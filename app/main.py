"""SnapMan FastAPI 應用進入點。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import re
import urllib.parse

from .auth import (ADMIN_PASSWORD_MIN_LEN, admin_password_problem, authenticate_ad,
                   bootstrap_local_admin, local_admin_must_change, set_local_admin_password)
from .config import ensure_session_secret, save_settings, settings
from .timeutil import display_tz, to_display
from .database import SessionLocal, init_db
from .models import AccountRole, AuditLog, CloneCopy, Profile, Run, VCenter
from .vsphere import get_client
from .vsphere.base import VSphereError
from .workflow import run_manager

BASE_DIR = Path(__file__).resolve().parent

_local = to_display  # 顯示時間一律走 timeutil（依設定頁的顯示時區）

# ---------- 終端 log 格式 ----------
# 統一為「時間(秒級) 等級 訊息」；預設格式時戳帶 ,毫秒、等級混在訊息裡，
# 窄視窗下難讀。時戳用伺服器本地時間（console log 慣例；頁面顯示才走顯示時區）。
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s %(levelname)-7s %(message)s",
            "datefmt": _LOG_DATEFMT,
        },
        "access": {  # 來源IP "請求行" 狀態碼
            "()": "uvicorn.logging.AccessFormatter",
            "format": '%(asctime)s %(levelname)-7s %(client_addr)s "%(request_line)s" %(status_code)s',
            "datefmt": _LOG_DATEFMT,
        },
    },
    "handlers": {
        "default": {"class": "logging.StreamHandler", "formatter": "default",
                    "stream": "ext://sys.stderr"},
        "access": {"class": "logging.StreamHandler", "formatter": "access",
                   "stream": "ext://sys.stdout"},
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
        # 應用自身事件（OVF 串流進度報告等）
        "snapman": {"handlers": ["default"], "level": "INFO", "propagate": False},
    },
}


def _setup_logging() -> None:
    """把統一格式套到既有 handler（涵蓋 uvicorn CLI / --reload 等
    未帶 LOG_CONFIG 的啟動路徑；python -m app.main 已由 LOG_CONFIG 設定，重套無害）。"""
    default_fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", _LOG_DATEFMT)
    try:
        from uvicorn.logging import AccessFormatter
        access_fmt = AccessFormatter(
            '%(asctime)s %(levelname)-7s %(client_addr)s "%(request_line)s" %(status_code)s',
            _LOG_DATEFMT)
    except Exception:
        access_fmt = default_fmt
    for name, fmt in (("uvicorn", default_fmt), ("uvicorn.error", default_fmt),
                      ("uvicorn.access", access_fmt), ("", default_fmt)):
        for h in logging.getLogger(name).handlers:
            h.setFormatter(fmt)
    # snapman logger：uvicorn CLI 路徑（未帶 LOG_CONFIG）下補掛 handler
    snap = logging.getLogger("snapman")
    if not snap.handlers:
        h = logging.StreamHandler()
        h.setFormatter(default_fmt)
        snap.addHandler(h)
        snap.setLevel(logging.INFO)
        snap.propagate = False

# ---------- 帳號分權（RBAC）----------
# LDAP 群組只控制「誰能登入」；權限由 account_roles 對照表（帳號→角色）決定。
ROLE_LABELS = {
    "full_admin": "Full Admin",
    "admin_readonly": "Admin ReadOnly",
    "audit": "Audit",
}


def _resolve_role(username: str) -> str:
    """登入帳號 → 角色（查分權表；未指派套用 settings.default_role）。
    不對 admin 特判：本機 admin 由 _role_for_session 依 session 的 local 旗標放行，
    AD 上恰好名為 admin 的帳號一律查表，避免借名取得管理者。"""
    try:
        with SessionLocal() as db:
            rows = db.query(AccountRole).all()
        for r in rows:
            if r.username.strip().lower() == username.strip().lower():
                return r.role if r.role in ROLE_LABELS else "admin_readonly"
    except Exception:
        pass
    role = settings.default_role
    return role if role in ROLE_LABELS else "admin_readonly"


_ROLE_TTL = 10.0
_role_cache: dict[str, tuple[float, str]] = {}


def _role_for_session(user: dict | None) -> str:
    """session user → 角色，每請求重算（10 秒 TTL 快取）：分權表改動最晚 10 秒內
    對既有 session 生效，不再信任登入當下寫進 cookie 的 role（降權 / 移除帳號
    原本要等 cookie 過期 14 天才失效）。"""
    user = user or {}
    if user.get("local") is True and user.get("id") == "admin":
        return "full_admin"
    uid = str(user.get("id", ""))
    now = time.monotonic()
    hit = _role_cache.get(uid)
    if hit and now - hit[0] < _ROLE_TTL:
        return hit[1]
    role = _resolve_role(uid)
    _role_cache[uid] = (now, role)
    return role


# Audit 角色可存取的頁面（唯讀）：儀表板 / 工作階段（含 CSV、Run 詳情）/ 行事曆 / 報表 / 記錄
_AUDIT_PATHS = re.compile(
    r"^(/|/sessions(\.csv)?|/runs/\d+|/calendar|/reports|/logs|/logout)$"
)


def _audit(request: Request | None, action: str, detail: str, user: str = "") -> None:
    """寫入稽核紀錄（含來源 IP）；失敗不影響主流程但記 log（DB 鎖等問題要看得到）。"""
    try:
        if not user and request is not None:
            u = request.session.get("user") or {}
            user = u.get("name") or u.get("id") or ""
        ip = _client_ip(request) if request is not None else ""
        with SessionLocal() as db:
            db.add(AuditLog(user=user, action=action, detail=detail, ip=ip))
            db.commit()
    except Exception as exc:
        logging.getLogger("snapman").warning("稽核寫入失敗（%s %s）：%s", action, user, exc)


def _template_globals(request: Request) -> dict:
    """注入所有模板共用的變數（topbar 顯示 vCenter 概況、登入者角色）。"""
    try:
        with SessionLocal() as db:
            vcs = db.query(VCenter).order_by(VCenter.id).all()
            label = vcs[0].name if len(vcs) == 1 else f"{len(vcs)} 座 vCenter"
    except Exception:
        label = "—"
    try:
        u = request.session.get("user") or {}
    except Exception:
        u = {}
    role = getattr(request.state, "role", None) or _role_for_session(u)
    return {
        "vcenter_host": label,
        "user_role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "can_write": role == "full_admin",  # 模板隱藏寫入按鈕用；強制點在 middleware
    }


templates = Jinja2Templates(
    directory=str(BASE_DIR / "web" / "templates"),
    context_processors=[_template_globals],
)

# 步驟標題開頭的 ①~⑩ → 徽章數字（模板渲染成綠底方塊；相容既有 DB 紀錄）
_CIRCLED = {"①": "1", "②": "2", "③": "3", "④": "4", "⑤": "5",
            "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9", "⑩": "10"}


def _stepno(title: str) -> dict:
    t = (title or "").strip()
    if t and t[0] in _CIRCLED:
        no, rest = _CIRCLED[t[0]], t[1:]
        if rest.startswith("+"):        # ⑤+（xvc 跨 VC 搬遷）
            no, rest = no + "+", rest[1:]
        return {"no": no, "text": rest.strip()}
    return {"no": "", "text": t}


templates.env.filters["stepno"] = _stepno


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    init_db()
    bootstrap_local_admin()
    _recover_stale_runs()
    refresher = asyncio.create_task(_background_enum_refresh())
    scheduler = asyncio.create_task(_scheduler_loop())
    yield
    refresher.cancel()
    scheduler.cancel()


async def _wait_run_done(run_id: int) -> bool:
    """等待 run 結束；回傳是否成功。"""
    while True:
        await asyncio.sleep(10)
        with SessionLocal() as db:
            r = db.get(Run, run_id)
        if r is None:
            return False
        if r.status in ("success", "failed", "stopped"):
            return r.status == "success"


async def _verify_chain(profile_id: int, trigger: str = "排程器") -> bool:
    """副本驗證 / DR 演練：掛「最舊的保留副本」（DBCC + 自訂驗證 SQL）
    → 驗畢換回原掛載副本 → 發送演練報告。

    回傳「今日份驗證是否已定案」：False 表示暫時起不來（併發上限/同 VM 互斥），
    排程器不標記當日已觸發、下一輪自動補跑（與每日排程同語意）。"""
    from .notify import send_report
    try:
        with SessionLocal() as db:
            p = db.get(Profile, profile_id)
            if p is None:
                return True
            pname = p.name
            mounted = next((c for c in p.copies if c.status == "mounted"), None)
            kept = sorted((c for c in p.copies if c.status == "kept"),
                          key=lambda c: c.created_at)
            if mounted is None or not kept:
                _audit(None, "verify_skip",
                       f"{pname}：無保留副本可驗證（需至少 1 份 kept）", user=trigger)
                return True
            oldest_id, mounted_id = kept[0].id, mounted.id
            oldest_at = to_display(kept[0].created_at, "%Y-%m-%d %H:%M")
        try:
            rid = await run_manager.start_mount(profile_id, oldest_id, kind="verify")
        except VSphereError as exc:
            # 暫時性阻擋（名額/互斥）→ 不定案，排程器下一輪補跑；寫稽核留跡
            _audit(None, "verify_defer",
                   f"{pname}：驗證暫緩（{exc}），排程將自動補跑", user=trigger)
            return False
        _audit(None, "verify_start",
               f"{pname}：掛載最舊副本進行驗證（Run #{rid}）", user=trigger)
        ok = await _wait_run_done(rid)
        _audit(None, "verify_result",
               f"{pname}：最舊副本驗證{'通過' if ok else '失敗'}（Run #{rid}）", user=trigger)
        # 無論成敗都嘗試換回原掛載（若首段在換掛前就失敗，原副本仍是 mounted → 直接視為已還原）
        restored = True
        try:
            rid2 = await run_manager.start_mount(profile_id, mounted_id, kind="verify")
            restored = await _wait_run_done(rid2)
        except VSphereError:
            pass  # 原副本仍掛載中 = 已還原
        # 演練報告（不受 alert_on_success 限制）
        text = (
            f"{'✅' if ok else '❌'} SnapMan DR 演練報告 — [{pname}]\n"
            f"驗證副本：{oldest_at} 建立的最舊保留副本\n"
            f"DBCC/自訂驗證：{'通過' if ok else '失敗'}（Run #{rid}）\n"
            f"換回原掛載：{'完成' if restored else '失敗，請檢查'}"
        )
        try:
            await asyncio.to_thread(send_report, f"[SnapMan] DR 演練 {pname}"
                                    f" {'通過' if ok else '失敗'}", text)
        except Exception:
            pass
        return True
    except Exception as exc:
        # 非預期錯誤：寫稽核留跡（先前靜默吞掉，驗證消失無人知），
        # 視為已定案（不重試，避免持續性錯誤每 20 秒重跑）
        _audit(None, "verify_error", f"Profile #{profile_id} 驗證流程異常：{exc}",
               user=trigger)
        return True


def _recover_stale_runs() -> None:
    """服務啟動時，把上個程序遺留的 pending/running run 標記為 failed
    （工作流狀態只存在於程序記憶體，重啟即中斷；來源快照等由下一輪自行收斂）。"""
    with SessionLocal() as db:
        stale = db.query(Run).filter(Run.status.in_(("pending", "running"))).all()
        for r in stale:
            r.status = "failed"
            r.error = "服務重啟，執行中斷"
            r.finished_at = dt.datetime.now(dt.timezone.utc)
            for s in r.steps:
                if s.status in ("pending", "running"):
                    s.status = "skipped"
                    s.message = s.message or "服務重啟，執行中斷"
        if stale:
            db.add(AuditLog(user="系統", action="recover",
                            detail=f"啟動復原：{len(stale)} 筆中斷的 run 標記為 failed"))
        db.commit()


async def _scheduler_loop() -> None:
    """每 20 秒檢查：每日排程 / 副本驗證排程 / 每日清過期執行紀錄。

    每日排程採 catch-up 語意：now >= 排程時間即觸發（同日一次）；
    因併發上限暫時起不來時不標記，下一輪自動補跑。
    """
    fired: dict[int, str] = {}          # 每日排程：profile_id -> 已觸發日期
    fired_verify: dict[int, str] = {}   # 驗證排程（成功起跑才標記，起不來下輪補跑）
    verify_inflight: set[int] = set()   # 驗證鏈執行中（兩段 run 之間的空檔防重複觸發）
    purged = ""
    while True:
        try:
            now = dt.datetime.now(dt.timezone.utc).astimezone(display_tz())
            today, hhmm, weekday = now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), now.weekday()
            with SessionLocal() as db:
                plist = [
                    (p.id, p.schedule_enabled, p.schedule_mode, p.schedule_time,
                     p.schedule_date, p.verify_enabled, p.verify_weekday, p.verify_time)
                    for p in db.query(Profile).all()
                ]
            for pid, s_en, s_mode, s_t, s_date, v_en, v_wd, v_t in plist:
                # 工作流排程：
                # - daily：now >= 當日排程時間即觸發（同日一次）
                # - once：now >= 指定日期時間即觸發（過期日期也補跑，同 catch-up
                #   語意），成功起跑後自動停用排程
                due = False
                if s_en and s_t and fired.get(pid) != today \
                        and not run_manager.is_running(pid):
                    if s_mode == "once":
                        due = bool(s_date) and (
                            s_date < today or (s_date == today and s_t <= hhmm))
                    else:
                        due = s_t <= hhmm
                if due:
                    try:
                        rid = await run_manager.start(pid)
                        fired[pid] = today
                        _audit(None, "run_start",
                               f"排程執行 Profile #{pid} → Run #{rid}", user="排程器")
                        if s_mode == "once":
                            # 一次性：起跑成功即停用，不會再次觸發
                            with SessionLocal() as db:
                                prof = db.get(Profile, pid)
                                if prof is not None:
                                    prof.schedule_enabled = False
                                    db.commit()
                            _audit(None, "schedule_once_done",
                                   f"Profile #{pid} 一次性排程已執行，排程自動停用",
                                   user="排程器")
                    except VSphereError:
                        pass  # 併發上限/暫時性問題 → 下一輪補跑
                # 副本驗證（catch-up 語意同每日排程：起不來不標記、下一輪補跑）
                if (v_en and v_t and v_t <= hhmm and fired_verify.get(pid) != today
                        and (v_wd == 7 or v_wd == weekday)
                        and pid not in verify_inflight
                        and not run_manager.is_running(pid)):
                    verify_inflight.add(pid)

                    async def _kick(pid=pid, day=today):
                        try:
                            if await _verify_chain(pid):
                                fired_verify[pid] = day
                        finally:
                            verify_inflight.discard(pid)

                    asyncio.create_task(_kick())
            # 執行紀錄保留：每天清一次
            if settings.run_retention_days > 0 and purged != today:
                purged = today
                cutoff = (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(days=settings.run_retention_days)).replace(tzinfo=None)
                with SessionLocal() as db:
                    for r in db.query(Run).filter(Run.started_at < cutoff).all():
                        db.delete(r)  # cascade 一併刪 StepLog
                    db.commit()
        except Exception:
            pass
        await asyncio.sleep(20)


app = FastAPI(title="SnapMan — Snapshot Manager", lifespan=lifespan)
# 前端零外部依賴：htmx 等資源自帶（app/web/static），不引用 CDN
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")

# 免登入即可存取的路徑
PUBLIC_PATHS = {"/login"}


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # 不能用 no-referrer：Fetch 規範下 no-referrer 會讓同站表單 POST 的 Origin 序列化為
    # "null"，而純 HTTP（非 https / localhost）Chrome 不送 Sec-Fetch-Site，_cross_site 只剩
    # Origin 可比對 → 自己的登出 / 儲存全被當跨站擋掉（UAT 實測）。same-origin 對外站仍不洩漏。
    "Referrer-Policy": "same-origin",
    # 零外部依賴（htmx 自帶），inline script / style 為模板現況所需；
    # ws: 供 run 進度 WebSocket（同主機）
    "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                "connect-src 'self' ws: wss:; frame-ancestors 'none'; "
                                "base-uri 'self'; form-action 'self'"),
}


def _secured(resp):
    for k, v in _SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _cross_site(request: Request) -> bool:
    """CSRF 防線：非 GET 請求須來自同站。優先看 Sec-Fetch-Site（現代瀏覽器必帶），
    其次比對 Origin 與 Host；兩者皆無（非瀏覽器客戶端）不擋。"""
    sfs = request.headers.get("sec-fetch-site", "").lower()
    if sfs:
        return sfs not in ("same-origin", "same-site", "none")
    origin = request.headers.get("origin", "")
    if origin and origin != "null":
        return urllib.parse.urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower()
    # 無 Origin（非瀏覽器客戶端）或 "null"（sandbox iframe / 特定 referrer policy）：
    # 純 HTTP 環境沒有 Sec-Fetch-Site 可靠訊號，這裡不擋，靠 SameSite=Lax cookie 兜底
    return False


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and _cross_site(request):
        _audit(request, "csrf_blocked", f"拒絕跨站 {request.method} {path}")
        return _secured(Response("跨站請求被拒", status_code=403, media_type="text/plain"))
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return _secured(await call_next(request))
    user = request.session.get("user")
    if not user:
        if request.headers.get("HX-Request"):
            resp = Response(status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return _secured(resp)
        return _secured(RedirectResponse("/login", status_code=303))
    # 升級前的本機 admin session（cookie 內只有 role、無 local 旗標）：新版角色改每請求
    # 重算、本機 admin 靠 local 旗標辨識，舊 cookie 會被判成 default_role（唯讀）。
    # 直接清 session 要求重登一次，避免「本機管理員 Admin ReadOnly」的困惑狀態
    if user.get("id") == "admin" and "local" not in user and user.get("role"):
        request.session.clear()
        if request.headers.get("HX-Request"):
            resp = Response(status_code=401)
            resp.headers["HX-Redirect"] = "/login"
            return _secured(resp)
        return _secured(RedirectResponse("/login", status_code=303))
    # 本機 admin 以初始 / 不合規密碼登入 → 只能到設定頁改密碼或登出
    if user.get("must_change_pw") and path not in ("/settings", "/logout"):
        msg = "請先變更本機 admin 密碼，再使用其他功能"
        if request.headers.get("HX-Request"):
            return _secured(Response(msg, status_code=403, media_type="text/plain"))
        if request.method in ("GET", "HEAD"):
            return RedirectResponse("/settings?error=" + urllib.parse.quote(msg), status_code=303)
        return templates.TemplateResponse(
            request, "denied.html", {"message": msg}, status_code=403)
    # 角色授權（伺服器端強制點；模板隱藏按鈕只是 UI 禮貌）：
    # - full_admin：全功能
    # - admin_readonly：全頁面可看，非 GET（任何變更/執行）一律拒絕
    # - audit：唯讀且僅限 _AUDIT_PATHS 白名單頁面
    role = _role_for_session(user)
    request.state.role = role
    if role != "full_admin":
        denied = ""
        # /logout 為 POST（防跨站 GET 登出），任何角色皆可執行
        if request.method not in ("GET", "HEAD") and path != "/logout":
            denied = (f"此帳號為唯讀權限（{ROLE_LABELS.get(role, role)}），"
                      "不可執行變更或啟動操作")
        elif role == "audit" and not _AUDIT_PATHS.match(path):
            denied = "此帳號為稽核權限（Audit），僅可檢視儀表板、工作階段、行事曆、報表與記錄"
        if denied:
            if request.headers.get("HX-Request"):
                return _secured(Response(denied, status_code=403, media_type="text/plain"))
            return _secured(templates.TemplateResponse(
                request, "denied.html", {"message": denied}, status_code=403
            ))
    return _secured(await call_next(request))


# SessionMiddleware 必須「後加」→ 成為最外層 → 先執行，讓 require_login 內能讀 request.session
app.add_middleware(SessionMiddleware, secret_key=ensure_session_secret(), same_site="lax")


# ---------- 頁面 ----------
_KIND_LABELS = {"daily": "每日", "mount": "掛載副本", "verify": "副本驗證",
                "vmsync": "整機同步"}


def _run_row(r: Run) -> dict:
    return {
        "id": r.id, "profile": r.profile.name, "status": r.status, "kind": r.kind,
        "kind_label": _KIND_LABELS.get(r.kind, r.kind),
        "started_at": _local(r.started_at, "%Y-%m-%d %H:%M:%S"),
        "duration": _fmt_dur(
            (r.finished_at - r.started_at).total_seconds() if r.finished_at else None
        ),
        "error": r.error or "",
    }


def _trend_charts() -> dict:
    """近 30 天趨勢：每日成功/失敗堆疊長條 + 每日工作流耗時折線（預先算好 SVG 幾何）。"""
    tz = display_tz()
    now = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    days = [(now - dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).replace(tzinfo=None)
    ok: dict[str, int] = {d: 0 for d in days}
    bad: dict[str, int] = {d: 0 for d in days}
    dur: dict[str, list[float]] = {d: [] for d in days}
    with SessionLocal() as db:
        for r in db.query(Run).filter(Run.started_at >= cutoff).all():
            d = _local(r.started_at, "%Y-%m-%d")
            if d not in ok:
                continue
            if r.status == "success":
                ok[d] += 1
                if r.kind == "daily" and r.finished_at:
                    dur[d].append((r.finished_at - r.started_at).total_seconds())
            elif r.status in ("failed", "stopped"):
                bad[d] += 1
    # 幾何：viewBox 0 0 900 150，繪圖區 左 34 / 下 18 / 上 8
    W, H, L, B, T = 900, 150, 34, 18, 8
    plot_h, plot_w = H - B - T, W - L - 6
    slot = plot_w / 30
    bw = slot * 0.55
    max_c = max([ok[d] + bad[d] for d in days] + [1])
    bars, labels = [], []
    for i, d in enumerate(days):
        x = L + i * slot + (slot - bw) / 2
        h_ok = ok[d] / max_c * plot_h
        h_bad = bad[d] / max_c * plot_h
        y0 = T + plot_h
        if ok[d]:
            bars.append({"x": round(x, 1), "y": round(y0 - h_ok, 1), "w": round(bw, 1),
                         "h": round(h_ok, 1), "cls": "c-ok",
                         "tip": f"{d} 成功 {ok[d]}"})
        if bad[d]:
            gap = 2 if ok[d] else 0
            bars.append({"x": round(x, 1), "y": round(y0 - h_ok - gap - h_bad, 1),
                         "w": round(bw, 1), "h": round(h_bad, 1), "cls": "c-bad",
                         "tip": f"{d} 失敗/停止 {bad[d]}"})
        if i % 5 == 0:
            labels.append({"x": round(x + bw / 2, 1), "text": d[5:].replace("-", "/")})
    # 折線：每日平均耗時（分鐘）
    dd = {d: (sum(v) / len(v)) for d, v in dur.items() if v}
    max_m = max([s / 60 for s in dd.values()] + [1])
    pts, dots = [], []
    for i, d in enumerate(days):
        if d not in dd:
            continue
        x = L + i * slot + slot / 2
        m = dd[d] / 60
        y = T + plot_h - (m / max_m * plot_h)
        pts.append(f"{round(x, 1)},{round(y, 1)}")
        dots.append({"x": round(x, 1), "y": round(y, 1),
                     "tip": f"{d} 耗時 {m:.1f} 分"})
    return {
        "W": W, "H": H, "L": L, "baseline": T + plot_h,
        "bars": bars, "labels": labels, "max_c": max_c,
        "line_points": " ".join(pts), "dots": dots, "max_m": round(max_m, 1),
        "grid_ys": [round(T + plot_h * f, 1) for f in (0.0, 0.5)],
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    # 今日統計用日期範圍查詢（先前取「最近 50 筆」再篩日期，任務多時當天會少算）
    now_local = dt.datetime.now(dt.timezone.utc).astimezone(display_tz())
    day_start = (now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                 .astimezone(dt.timezone.utc).replace(tzinfo=None))
    with SessionLocal() as db:
        profiles = db.query(Profile).all()
        recent_runs = db.query(Run).order_by(Run.id.desc()).limit(50).all()
        today_runs = [r for r in db.query(Run).filter(Run.started_at >= day_start).all()
                      if r.status != "running"]
        copies_total = db.query(CloneCopy).count()
        mounted = db.query(CloneCopy).filter(CloneCopy.status == "mounted").count()
        stats = {
            "profiles": len(profiles),
            "scheduled": sum(1 for p in profiles if p.schedule_enabled),
            "today_success": sum(1 for r in today_runs if r.status == "success"),
            "today_failed": sum(1 for r in today_runs if r.status == "failed"),
            "copies": copies_total,
            "mounted": mounted,
            "last": _run_row(recent_runs[0]) if recent_runs else None,
        }
        recent = [_run_row(r) for r in recent_runs[:10]]
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"stats": stats, "recent": recent, "charts": _trend_charts()},
    )


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, error: str = ""):
    with SessionLocal() as db:
        def _vc_pair(p):
            """(來源 vCenter, 目的 vCenter)；同一座時目的回空字串（模板只印一行）。"""
            s = p.vcenter.name if p.vcenter else "（未綁定）"
            t = p.target_vcenter.name if p.target_vcenter else "（未綁定）"
            return s, ("" if p.vcenter_id == p.target_vcenter_id else t)

        def _target_label(p):
            if p.job_type == "vmsync":
                return p.replica_name.strip() or f"{p.source_vm}-replica"
            return p.target_vm

        profiles = [
            {"id": p.id, "name": p.name, "source_vm": p.source_vm,
             "target_vm": _target_label(p), "databases": p.databases,
             "job_type": p.job_type,
             "last_clone_path": p.last_clone_path,
             "vc_src": _vc_pair(p)[0], "vc_tgt": _vc_pair(p)[1],
             "schedule_enabled": p.schedule_enabled, "schedule_time": p.schedule_time,
             "schedule_mode": p.schedule_mode, "schedule_date": p.schedule_date}
            for p in db.query(Profile).order_by(Profile.id).all()
        ]
        vcenters = [{"id": v.id, "name": v.name}
                    for v in db.query(VCenter).order_by(VCenter.id).all()]
    return templates.TemplateResponse(
        request, "jobs.html",
        {"profiles": profiles, "vcenters": vcenters, "error": error})


@app.get("/sessions", response_class=HTMLResponse)
def sessions_page(request: Request):
    with SessionLocal() as db:
        runs = [_run_row(r) for r in db.query(Run).order_by(Run.id.desc()).limit(100).all()]
    return templates.TemplateResponse(request, "sessions.html", {"runs": runs})


@app.get("/sessions.csv")
def sessions_csv():
    import csv
    import io

    def _csv_safe(v: str) -> str:
        # Excel 公式注入防護：以 = + - @ 開頭的儲存格會被當公式執行，前綴 ' 中和
        return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["run_id", "profile", "kind", "status", "started_at", "duration", "error"])
    with SessionLocal() as db:
        for r in db.query(Run).order_by(Run.id.desc()).limit(1000).all():
            row = _run_row(r)
            w.writerow([row["id"], _csv_safe(row["profile"]), row["kind_label"],
                        row["status"], row["started_at"], row["duration"],
                        _csv_safe(row["error"])])
    return Response(
        content="﻿" + buf.getvalue(),  # BOM：Excel 直接開啟不亂碼
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=snapman-sessions.csv"},
    )


@app.post("/profiles/{profile_id}/drill")
async def drill_profile(request: Request, profile_id: int):
    with SessionLocal() as db:
        p = db.get(Profile, profile_id)
        if p is None:
            return HTMLResponse("找不到 Profile", status_code=404)
        has_kept = any(c.status == "kept" for c in p.copies)
        pname = p.name
    if not has_kept:
        return RedirectResponse(
            "/jobs?error=" + urllib.parse.quote(f"{pname} 無保留副本可演練（需至少 1 份）"),
            status_code=303)
    if run_manager.is_running(profile_id):
        return RedirectResponse("/jobs?error=" + urllib.parse.quote("任務執行中，請稍後"),
                                status_code=303)
    user = (request.session.get("user") or {}).get("name", "")
    _audit(request, "drill_start", f"手動觸發 DR 演練：{pname}")
    asyncio.create_task(_verify_chain(profile_id, trigger=user or "手動"))
    return RedirectResponse("/sessions", status_code=303)


# ---------- 行事曆 ----------
@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, ym: str = ""):
    """月曆：固定 6 週 42 格（週日起始，高度不隨月份跳動，參考 WEB_ALMS）。
    每筆 run 一個 chip（時間＋任務名，依狀態上色、點入詳情）；
    今天（含）以後的格子另以虛線 chip 呈現排程（每日排程逐日、一次性在其日期）。"""
    tz = display_tz()
    now = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    today = now.date()
    try:
        year, month = (int(x) for x in ym.split("-")) if ym else (now.year, now.month)
        first = dt.date(year, month, 1)
    except ValueError:
        first = dt.date(now.year, now.month, 1)
    prev_m = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    next_m = (first + dt.timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    # 固定 6 週網格（週日起始）；查詢範圍對齊網格邊界（顯示時區 → UTC）
    lead = (first.weekday() + 1) % 7
    grid_start = first - dt.timedelta(days=lead)
    tzinfo = tz or dt.datetime.now().astimezone().tzinfo
    lo = (dt.datetime.combine(grid_start, dt.time.min, tzinfo=tzinfo)
          .astimezone(dt.timezone.utc).replace(tzinfo=None))
    hi = (dt.datetime.combine(grid_start + dt.timedelta(days=42), dt.time.min,
                              tzinfo=tzinfo)
          .astimezone(dt.timezone.utc).replace(tzinfo=None))
    runs_by_day: dict[str, list[dict]] = {}
    with SessionLocal() as db:
        for r in (db.query(Run).filter(Run.started_at >= lo, Run.started_at < hi)
                  .order_by(Run.id).all()):
            runs_by_day.setdefault(_local(r.started_at, "%Y-%m-%d"), []).append({
                "id": r.id, "time": _local(r.started_at, "%H:%M"),
                "profile": r.profile.name, "status": r.status,
                "kind_label": _KIND_LABELS.get(r.kind, r.kind),
                "duration": _fmt_dur(
                    (r.finished_at - r.started_at).total_seconds()
                    if r.finished_at else None),
                "error": (r.error or "")[:160],
            })
        scheds = [
            {"name": p.name, "time": p.schedule_time,
             "mode": p.schedule_mode, "date": p.schedule_date}
            for p in db.query(Profile).filter(Profile.schedule_enabled.is_(True))
            .order_by(Profile.id).all()
        ]
    weeks = []
    d = grid_start
    for _ in range(6):
        row = []
        for _ in range(7):
            key = d.isoformat()
            day_scheds = [
                s for s in scheds
                if (s["date"] == key if s["mode"] == "once" else d >= today)
            ]
            row.append({"day": d.day, "in_month": d.month == first.month,
                        "is_today": d == today,
                        "runs": runs_by_day.get(key, []), "scheds": day_scheds})
            d += dt.timedelta(days=1)
        weeks.append(row)
    return templates.TemplateResponse(request, "calendar.html", {
        "title_ym": f"{first.year} 年 {first.month} 月",
        "prev_m": prev_m, "next_m": next_m,
        "is_current_month": (first.year, first.month) == (today.year, today.month),
        "weeks": weeks,
    })


@app.post("/profiles")
def create_profile(
    request: Request,
    name: str = Form(...),
    job_type: str = Form("disk"),
    replica_name: str = Form(""),
    target_network: str = Form(""),
    vmsync_transfer: str = Form("xvc"),
    source_vcenter_id: int = Form(0),
    target_vcenter_id: int = Form(0),
    transfer_mode: str = Form("shared"),
    staging_datastore: str = Form(""),
    source_vm: str = Form(...),
    source_data_disk: str = Form(""),
    target_vm: str = Form(""),
    target_drive_letter: str = Form("E"),
    target_datastore: str = Form(""),
    databases: str = Form(""),
    source_sql_instance: str = Form(""),
    target_sql_instance: str = Form(""),
    keep_copies: int = Form(1),
    schedule_enabled: str = Form(""),
    schedule_mode: str = Form("daily"),
    schedule_date: str = Form(""),
    schedule_hh: str = Form(""),
    schedule_mm: str = Form("00"),
    verify_enabled: str = Form(""),
    verify_weekday: int = Form(6),
    verify_hh: str = Form(""),
    verify_mm: str = Form("00"),
    drill_query: str = Form(""),
    archive_enabled: str = Form(""),
    archive_datastore: str = Form(""),
    archive_keep: int = Form(7),
    chain_next_id: int = Form(0),
):
    # 時間改由 24 小時制的時/分下拉組成（原生 time 選擇器彈窗會跳動且顯示 12 小時制）
    schedule_time = f"{schedule_hh}:{schedule_mm}" if schedule_hh else ""
    verify_time = f"{verify_hh}:{verify_mm}" if verify_hh else ""
    schedule_mode = schedule_mode if schedule_mode in ("daily", "once") else "daily"
    name = name.strip()
    job_type = job_type if job_type in ("disk", "vmsync") else "disk"
    # transfer_mode 依任務類型取值：disk = shared/xvc；vmsync = xvc/ovf（各自的下拉）
    if job_type == "vmsync":
        transfer_mode = vmsync_transfer if vmsync_transfer in ("xvc", "ovf") else "xvc"
    else:
        transfer_mode = transfer_mode if transfer_mode in ("shared", "xvc") else "shared"
    err = _validate_profile_input(
        job_type, databases, target_drive_letter.strip().upper(),
        source_data_disk, target_vm, target_datastore, target_network,
        source_vm=source_vm,
        same_vc=(not target_vcenter_id or target_vcenter_id == source_vcenter_id),
        source_sql_instance=source_sql_instance, target_sql_instance=target_sql_instance,
        replica_name=replica_name)
    if err:
        return RedirectResponse("/jobs?error=" + urllib.parse.quote(err), status_code=303)
    with SessionLocal() as db:
        if db.query(Profile).filter(Profile.name == name).first():
            return RedirectResponse(
                "/jobs?error=" + urllib.parse.quote(f"名稱「{name}」已被其他 Profile 使用"),
                status_code=303)
        first = db.query(VCenter).order_by(VCenter.id).first()
        default_id = first.id if first else None
        if not source_vcenter_id or db.get(VCenter, source_vcenter_id) is None:
            source_vcenter_id = default_id
        if not target_vcenter_id or db.get(VCenter, target_vcenter_id) is None:
            target_vcenter_id = source_vcenter_id
        # 串接對象須存在且不形成循環（既有資料已含循環時一併擋下）
        if chain_next_id and (db.get(Profile, chain_next_id) is None
                              or _chain_cycle(db, None, chain_next_id)):
            chain_next_id = 0
        db.add(
            Profile(
                name=name, vcenter_id=source_vcenter_id,
                job_type=job_type,
                replica_name=replica_name.strip(),
                target_network=target_network.strip(),
                target_vcenter_id=target_vcenter_id,
                transfer_mode=transfer_mode,
                staging_datastore=staging_datastore.strip(),
                source_vm=source_vm, source_data_disk=source_data_disk,
                target_vm=target_vm,
                target_drive_letter=target_drive_letter.strip().upper(),
                target_datastore=target_datastore, databases=databases,
                source_sql_instance=source_sql_instance.strip(),
                target_sql_instance=target_sql_instance.strip(),
                keep_copies=max(1, keep_copies),
                schedule_enabled=_valid_schedule(schedule_enabled == "on", schedule_time,
                                                 schedule_mode, schedule_date),
                schedule_mode=schedule_mode,
                schedule_date=schedule_date.strip(),
                schedule_time=schedule_time.strip(),
                verify_enabled=_valid_schedule(verify_enabled == "on", verify_time),
                verify_weekday=min(7, max(0, verify_weekday)),
                verify_time=verify_time.strip(),
                drill_query=drill_query.strip(),
                archive_enabled=archive_enabled == "on" and bool(archive_datastore.strip()),
                archive_datastore=archive_datastore.strip(),
                archive_keep=max(1, archive_keep),
                chain_next_id=chain_next_id or None,
            )
        )
        db.commit()
        warn = _xvm_warn(db.query(Profile).filter(Profile.name == name).first())
    _audit(request, "profile_create", f"新增任務：{name}")
    return RedirectResponse("/jobs" + warn, status_code=303)


def _valid_schedule(enabled: bool, schedule_time: str, mode: str = "daily",
                    schedule_date: str = "") -> bool:
    """排程需有合法 HH:MM 才視為啟用；一次性（once）另需合法日期 YYYY-MM-DD。"""
    if not (enabled
            and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", schedule_time.strip())):
        return False
    if mode == "once":
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", schedule_date.strip()))
    return True


_DB_NAME_RE = re.compile(r"^[\w\-. ]+$")  # \w 含中日韓字元；排除引號/括號/cmd 特殊字元


_SQL_INSTANCE_RE = re.compile(r"[A-Za-z0-9_$]{1,16}")   # 與 steps._INSTANCE_RE 同規則
_VM_NAME_RE = re.compile(r"[\w\-. ]{1,80}")            # VM / 複本名：拒絕引號與控制字元


def _validate_profile_input(job_type: str, databases: str, drive: str,
                            source_data_disk: str = "", target_vm: str = "",
                            target_datastore: str = "",
                            target_network: str = "",
                            source_vm: str = "", same_vc: bool = True,
                            source_sql_instance: str = "",
                            target_sql_instance: str = "",
                            replica_name: str = "") -> str | None:
    """任務表單驗證。disk：DB 名、碟號、SQL 執行個體名白名單（這些值會組進
    guest 內的 cmd / T-SQL / PowerShell 命令字串，在入口一次擋掉特殊字元），
    且同一座 vCenter 時目標不得是來源本身（⑥ DETACH 會打到 production）；
    vmsync：目的 datastore 與目標網路必填、複本名白名單。"""
    if not _VM_NAME_RE.fullmatch(source_vm.strip() or "x"):
        return "來源 VM 名稱含不允許的字元"
    if job_type == "vmsync":
        if not target_datastore.strip():
            return "整機同步任務須指定目的 datastore"
        if not target_network.strip():
            return "整機同步任務須指定目標網路（portgroup）"
        if replica_name.strip() and not _VM_NAME_RE.fullmatch(replica_name.strip()):
            return "複本名稱含不允許的字元（僅限中英數、底線、句點、連字號、空白）"
        return None
    if not source_data_disk.strip():
        return "請指定來源資料碟（VMDK）"
    if not target_vm.strip():
        return "請指定目標 VM"
    if not _VM_NAME_RE.fullmatch(target_vm.strip()):
        return "目標 VM 名稱含不允許的字元"
    if same_vc and target_vm.strip().lower() == source_vm.strip().lower():
        return "目標 VM 不可與來源 VM 相同（會對來源 production DB 執行 DETACH）"
    for label, inst in (("來源", source_sql_instance), ("目標", target_sql_instance)):
        if inst.strip() and not _SQL_INSTANCE_RE.fullmatch(inst.strip()):
            return (f"{label} SQL 執行個體名「{inst.strip()}」含不允許的字元"
                    "（僅限英數、底線、$，最長 16 字；不接受 HOST\\INST，一律指向該 VM 本機）")
    if not re.fullmatch(r"[A-Za-z]", drive):
        return "目標碟號須為單一英文字母（A–Z）"
    for d in (x.strip() for x in databases.split(",") if x.strip()):
        if not _DB_NAME_RE.fullmatch(d):
            return (f"資料庫名稱「{d}」含不允許的字元"
                    "（僅限中英數、底線、句點、連字號、空白）")
    return None


def _xvm_check(p: Profile) -> tuple[bool, str]:
    """XVM（Cross-vCenter vMotion）相容性預檢：編輯儲存與「檢視狀態」共用。
    比對兩端 vCenter 版本+build、來源側發起主機（暫存 datastore 可見者）、目的側
    落點主機（vmsync：目的 datastore 可見者；disk：目標 VM 所在主機）——任一側
    主機比對端 vCenter 新就會被拒（Run #49 / #119）。非 XVM 或同座 vCenter 回 (True, "")。"""
    if p.transfer_mode != "xvc" or p.vcenter_id == p.target_vcenter_id:
        return True, ""
    from .vsphere.real import _host_ok_for_vc
    src_vc, tgt_vc = p.vcenter, p.target_vcenter
    try:
        src_ver, src_build = _with_client(lambda c: c.vcenter_release(), src_vc)
        tgt_ver, tgt_build = _with_client(lambda c: c.vcenter_release(), tgt_vc)

        def _src(c):
            ds = (p.staging_datastore or "").strip()
            if not ds:
                disks = c.list_disks(p.source_vm)
                ds = disks[0].datastore if disks and disks[0].datastore else ""
            if not ds:
                raise VSphereError("無法判定來源側暫存 datastore，請在任務指定")
            return ds, c.pick_xvc_host(ds, tgt_ver, tgt_build)
        src_ds, src_host = _with_client(_src, src_vc)

        def _dst(c):
            if p.job_type == "vmsync":
                return c.pick_xvc_host(p.target_datastore, src_ver, src_build)
            name, ver, build = c.vm_host_release(p.target_vm)
            if not _host_ok_for_vc(ver, build, src_ver, src_build):
                raise VSphereError(
                    f"目標 VM 所在主機 {name}（ESXi {ver} build {build}）比來源 vCenter"
                    f"（{src_ver} build {src_build}）新，XVM 會被拒絕")
            return name
        dst_host = _with_client(_dst, tgt_vc)
        return True, (f"XVM 相容：來源 {src_host}（{src_ds}）發起 → 目的 {dst_host}；"
                      f"vCenter {src_ver} b{src_build} → {tgt_ver} b{tgt_build}")
    except Exception as exc:
        return False, _brief_err(exc)


def _xvm_warn(p: Profile | None) -> str:
    """儲存後的 XVM 預檢：不通過只警告（仍儲存），回 redirect 用的 query string。"""
    if p is None:
        return ""
    ok, msg = _xvm_check(p)
    if ok:
        return ""
    return "?error=" + urllib.parse.quote(f"已儲存，但 XVM 相容性預檢未通過：{msg}（執行時會在③/⑤失敗）")


def _chain_cycle(db, profile_id: int | None, next_id: int) -> bool:
    """檢查把 profile_id 的串接指向 next_id 後是否形成循環
    （A→B→A 會無限互相觸發、全天連跑）。profile_id=None 代表新任務。"""
    seen: set[int] = set()
    cur: int | None = next_id
    while cur:
        if cur == profile_id or cur in seen:
            return True
        seen.add(cur)
        nxt = db.get(Profile, cur)
        cur = nxt.chain_next_id if nxt else None
    return False


# ---------- Profile 編輯 ----------
def _profile_form_data(p: Profile) -> dict:
    return {
        "id": p.id, "name": p.name,
        "job_type": p.job_type, "replica_name": p.replica_name,
        "target_network": p.target_network,
        "source_vcenter_id": p.vcenter_id, "target_vcenter_id": p.target_vcenter_id,
        "transfer_mode": p.transfer_mode, "staging_datastore": p.staging_datastore,
        "source_vm": p.source_vm,
        "source_data_disk": p.source_data_disk, "target_vm": p.target_vm,
        "target_drive_letter": p.target_drive_letter, "target_datastore": p.target_datastore,
        "databases": p.databases, "source_sql_instance": p.source_sql_instance,
        "target_sql_instance": p.target_sql_instance, "last_clone_path": p.last_clone_path,
        "keep_copies": p.keep_copies, "schedule_enabled": p.schedule_enabled,
        "schedule_mode": p.schedule_mode, "schedule_date": p.schedule_date,
        "schedule_time": p.schedule_time,
        "verify_enabled": p.verify_enabled, "verify_weekday": p.verify_weekday,
        "verify_time": p.verify_time, "drill_query": p.drill_query,
        "archive_enabled": p.archive_enabled, "archive_datastore": p.archive_datastore,
        "archive_keep": p.archive_keep, "chain_next_id": p.chain_next_id,
    }


@app.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
def edit_profile_page(request: Request, profile_id: int, error: str = ""):
    with SessionLocal() as db:
        p = db.get(Profile, profile_id)
        if p is None:
            return HTMLResponse("找不到 Profile", status_code=404)
        data = _profile_form_data(p)
        vcenters = [{"id": v.id, "name": v.name}
                    for v in db.query(VCenter).order_by(VCenter.id).all()]
        others = [{"id": o.id, "name": o.name}
                  for o in db.query(Profile).filter(Profile.id != profile_id)
                  .order_by(Profile.id).all()]
    return templates.TemplateResponse(
        request, "profile_edit.html",
        {"p": data, "vcenters": vcenters, "others": others, "error": error})


@app.post("/profiles/{profile_id}/edit")
def edit_profile_submit(
    request: Request,
    profile_id: int,
    name: str = Form(...),
    job_type: str = Form("disk"),
    replica_name: str = Form(""),
    target_network: str = Form(""),
    vmsync_transfer: str = Form("xvc"),
    source_vcenter_id: int = Form(0),
    target_vcenter_id: int = Form(0),
    transfer_mode: str = Form("shared"),
    staging_datastore: str = Form(""),
    source_vm: str = Form(...),
    source_data_disk: str = Form(""),
    target_vm: str = Form(""),
    target_drive_letter: str = Form("E"),
    target_datastore: str = Form(""),
    databases: str = Form(""),
    source_sql_instance: str = Form(""),
    target_sql_instance: str = Form(""),
    keep_copies: int = Form(1),
    schedule_enabled: str = Form(""),
    schedule_mode: str = Form("daily"),
    schedule_date: str = Form(""),
    schedule_hh: str = Form(""),
    schedule_mm: str = Form("00"),
    verify_enabled: str = Form(""),
    verify_weekday: int = Form(6),
    verify_hh: str = Form(""),
    verify_mm: str = Form("00"),
    drill_query: str = Form(""),
    archive_enabled: str = Form(""),
    archive_datastore: str = Form(""),
    archive_keep: int = Form(7),
    chain_next_id: int = Form(0),
):
    schedule_time = f"{schedule_hh}:{schedule_mm}" if schedule_hh else ""
    verify_time = f"{verify_hh}:{verify_mm}" if verify_hh else ""
    schedule_mode = schedule_mode if schedule_mode in ("daily", "once") else "daily"
    with SessionLocal() as db:
        p = db.get(Profile, profile_id)
        if p is None:
            return HTMLResponse("找不到 Profile", status_code=404)
        dup = (
            db.query(Profile)
            .filter(Profile.name == name.strip(), Profile.id != profile_id)
            .first()
        )
        if dup:
            data = _profile_form_data(p)
            return templates.TemplateResponse(
                request, "profile_edit.html",
                {"p": data, "error": f"名稱「{name.strip()}」已被其他 Profile 使用"},
                status_code=400,
            )
        job_type = job_type if job_type in ("disk", "vmsync") else "disk"
        _src_vc = source_vcenter_id or p.vcenter_id
        _tgt_vc = target_vcenter_id or p.target_vcenter_id
        err = _validate_profile_input(
            job_type, databases, target_drive_letter.strip().upper(),
            source_data_disk, target_vm, target_datastore, target_network,
            source_vm=source_vm, same_vc=(_tgt_vc == _src_vc),
            source_sql_instance=source_sql_instance, target_sql_instance=target_sql_instance,
            replica_name=replica_name)
        if err is None and chain_next_id and chain_next_id != profile_id \
                and db.get(Profile, chain_next_id) is not None \
                and _chain_cycle(db, profile_id, chain_next_id):
            err = "任務串接形成循環（會無限互相觸發），請改選其他任務"
        if err:
            data = _profile_form_data(p)
            return templates.TemplateResponse(
                request, "profile_edit.html", {"p": data, "error": err},
                status_code=400,
            )
        p.name = name.strip()
        p.job_type = job_type
        p.replica_name = replica_name.strip()
        p.target_network = target_network.strip()
        if source_vcenter_id and db.get(VCenter, source_vcenter_id) is not None:
            p.vcenter_id = source_vcenter_id
        if target_vcenter_id and db.get(VCenter, target_vcenter_id) is not None:
            p.target_vcenter_id = target_vcenter_id
        # transfer_mode 依任務類型取值：disk = shared/xvc；vmsync = xvc/ovf
        if job_type == "vmsync":
            p.transfer_mode = (vmsync_transfer
                               if vmsync_transfer in ("xvc", "ovf") else "xvc")
        elif transfer_mode in ("shared", "xvc"):
            p.transfer_mode = transfer_mode
        p.staging_datastore = staging_datastore.strip()
        p.source_vm = source_vm.strip()
        p.source_data_disk = source_data_disk.strip()
        p.target_vm = target_vm.strip()
        p.target_drive_letter = target_drive_letter.strip().upper()
        p.target_datastore = target_datastore.strip()
        p.databases = databases.strip()
        p.source_sql_instance = source_sql_instance.strip()
        p.target_sql_instance = target_sql_instance.strip()
        p.keep_copies = max(1, keep_copies)
        p.schedule_enabled = _valid_schedule(schedule_enabled == "on", schedule_time,
                                             schedule_mode, schedule_date)
        p.schedule_mode = schedule_mode
        p.schedule_date = schedule_date.strip()
        p.schedule_time = schedule_time.strip()
        p.verify_enabled = _valid_schedule(verify_enabled == "on", verify_time)
        p.verify_weekday = min(7, max(0, verify_weekday))
        p.verify_time = verify_time.strip()
        p.drill_query = drill_query.strip()
        p.archive_enabled = archive_enabled == "on" and bool(archive_datastore.strip())
        p.archive_datastore = archive_datastore.strip()
        p.archive_keep = max(1, archive_keep)
        p.chain_next_id = chain_next_id if (
            chain_next_id and chain_next_id != profile_id
            and db.get(Profile, chain_next_id) is not None) else None
        db.commit()
        warn = _xvm_warn(p)
    _audit(request, "profile_update", f"編輯任務：{name.strip()}（#{profile_id}）")
    return RedirectResponse("/jobs" + warn, status_code=303)


# ---------- 副本（point-in-time copies）----------
@app.get("/profiles/{profile_id}/copies", response_class=HTMLResponse)
def copies_page(request: Request, profile_id: int, error: str = ""):
    with SessionLocal() as db:
        p = db.get(Profile, profile_id)
        if p is None:
            return HTMLResponse("找不到 Profile", status_code=404)
        copies = [
            {"id": c.id, "path": c.path, "status": c.status,
             "created_at": to_display(c.created_at, "%Y-%m-%d %H:%M:%S"),
             "has_files": bool(c.files_json)}
            for c in p.copies
        ]
        ctx = {"profile_id": p.id, "profile": p.name, "keep_copies": p.keep_copies,
               "copies": copies, "error": error}
    return templates.TemplateResponse(request, "copies.html", ctx)


@app.post("/profiles/{profile_id}/copies/{copy_id}/mount")
async def mount_copy(request: Request, profile_id: int, copy_id: int):
    try:
        run_id = await run_manager.start_mount(profile_id, copy_id)
    except VSphereError as exc:
        return RedirectResponse(
            f"/profiles/{profile_id}/copies?error={urllib.parse.quote(str(exc))}",
            status_code=303,
        )
    _audit(request, "copy_mount", f"掛載副本 #{copy_id}（Profile #{profile_id}）→ Run #{run_id}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/profiles/{profile_id}/copies/{copy_id}/delete")
def delete_copy(request: Request, profile_id: int, copy_id: int):
    with SessionLocal() as db:
        c = db.get(CloneCopy, copy_id)
        if c is None or c.profile_id != profile_id:
            return RedirectResponse(f"/profiles/{profile_id}/copies?error=找不到副本",
                                    status_code=303)
        if c.status == "mounted":
            return RedirectResponse(
                f"/profiles/{profile_id}/copies?error=掛載中的副本不可刪除",
                status_code=303,
            )
        path = c.path
        prof = db.get(Profile, profile_id)
        tvc = (prof.target_vcenter or prof.vcenter) if prof else None
        if tvc is not None:
            db.expunge(tvc)
    err = ""
    try:
        _with_client(lambda cl: cl.delete_vmdk(path), tvc)
    except Exception as exc:
        err = f"檔案刪除失敗（{exc}），已移除紀錄"
    with SessionLocal() as db:
        c = db.get(CloneCopy, copy_id)
        if c is not None:
            db.delete(c)
            db.commit()
    _audit(request, "copy_delete", f"刪除副本：{path}")
    url = f"/profiles/{profile_id}/copies"
    return RedirectResponse(
        url + (f"?error={urllib.parse.quote(err)}" if err else ""), status_code=303)


# ---------- 報表 ----------
def _fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    out = []
    with SessionLocal() as db:
        for p in db.query(Profile).order_by(Profile.id).all():
            runs = (
                db.query(Run).filter(Run.profile_id == p.id)
                .order_by(Run.id.desc()).limit(200).all()
            )
            total = len(runs)
            succ = sum(1 for r in runs if r.status == "success")
            durs = [
                (r.finished_at - r.started_at).total_seconds()
                for r in runs if r.finished_at and r.status == "success"
            ]
            # 每步平均耗時（取近 50 次成功的每日工作流）
            step_stats: dict[str, list] = {}
            picked = [r for r in runs if r.status == "success" and r.kind == "daily"][:50]
            for r in picked:
                for s in r.steps:
                    if s.started_at and s.finished_at:
                        d = (s.finished_at - s.started_at).total_seconds()
                        t = step_stats.setdefault(s.key, [s.title, 0, 0.0])
                        t[1] += 1
                        t[2] += d
            steps = [
                {"title": v[0], "avg": _fmt_dur(v[2] / v[1])}
                for v in step_stats.values() if v[1]
            ]
            recent = [
                {"id": r.id, "status": r.status, "kind": r.kind,
                 "started_at": to_display(r.started_at, "%Y-%m-%d %H:%M:%S"),
                 "duration": _fmt_dur(
                     (r.finished_at - r.started_at).total_seconds()
                     if r.finished_at else None)}
                for r in runs[:20]
            ]
            out.append({
                "name": p.name, "total": total, "success": succ,
                "rate": f"{succ * 100 // total}%" if total else "—",
                "avg_duration": _fmt_dur(sum(durs) / len(durs) if durs else None),
                "steps": steps, "recent": recent,
            })
    return templates.TemplateResponse(request, "reports.html", {"profiles": out})


# ---------- 登入 / 登出 ----------
# 登入失敗速率限制（防暴力嘗試；程序內記憶體即可，重啟歸零無妨）
_LOGIN_MAX_FAILS = 5           # 同 IP+帳號：視窗內失敗達此數即暫時封鎖
_LOGIN_MAX_FAILS_IP = 20       # 同 IP（不分帳號）：防密碼噴灑 / 帳號列舉
_LOGIN_WINDOW_SECS = 300.0     # 觀察視窗 5 分鐘
_LOGIN_TABLE_MAX = 5000        # 封鎖表上限：超過即全表修剪（隨機帳號名灌爆記憶體）
_login_fails: dict[str, list[float]] = {}   # "u|IP|帳號" / "ip|IP" → 失敗時刻


def _client_ip(request: Request | None) -> str:
    # 不信任 X-Forwarded-For（無反向代理；偽造 header 可繞過限流）
    return request.client.host if request is not None and request.client else "?"


def _login_keys(request: Request, username: str) -> tuple[str, str]:
    ip = _client_ip(request)
    return f"u|{ip}|{username.lower()}", f"ip|{ip}"


def _recent(key: str, now: float) -> list[float]:
    fails = [t for t in _login_fails.get(key, []) if now - t < _LOGIN_WINDOW_SECS]
    if fails:
        _login_fails[key] = fails
    else:
        _login_fails.pop(key, None)
    return fails


def _login_blocked(request: Request, username: str) -> bool:
    now = time.monotonic()
    k_user, k_ip = _login_keys(request, username)
    return (len(_recent(k_user, now)) >= _LOGIN_MAX_FAILS
            or len(_recent(k_ip, now)) >= _LOGIN_MAX_FAILS_IP)


def _login_failed(request: Request, username: str) -> None:
    now = time.monotonic()
    if len(_login_fails) > _LOGIN_TABLE_MAX:
        for k in list(_login_fails):
            _recent(k, now)
    for k in _login_keys(request, username):
        _login_fails.setdefault(k, []).append(now)


def _login_reset(request: Request, username: str) -> None:
    _login_fails.pop(_login_keys(request, username)[0], None)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "ad_enabled": settings.ad_enabled}
    )


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    uname = username.strip()
    if _login_blocked(request, uname):
        _audit(request, "login_failed", f"登入嘗試過於頻繁，暫時封鎖：{uname}", user=uname)
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote("嘗試次數過多，請 5 分鐘後再試"),
            status_code=303,
        )
    # 本機管理帳號（緊急備援，不經過 AD；密碼可於設定頁變更；恆為 Full Admin）。
    # 空值防護：密碼未設定（AD 模式停用本機登入）時不得 fail-open。
    # compare_digest 的 str 版只接受 ASCII，含中文密碼會 TypeError → 一律以 bytes 比對。
    if (uname == "admin" and settings.local_admin_password
            and secrets.compare_digest(password.encode("utf-8"),
                                       settings.local_admin_password.encode("utf-8"))):
        _login_reset(request, uname)
        must_change = local_admin_must_change(password)
        request.session["user"] = {"id": "admin", "name": "本機管理員", "local": True,
                                   "must_change_pw": must_change}
        _audit(request, "login", "本機管理員登入（Full Admin）"
               + ("（初始 / 不合規密碼，強制變更）" if must_change else ""))
        if must_change:
            return RedirectResponse(
                "/settings?error=" + urllib.parse.quote("請先變更本機 admin 密碼（初始或不符規則）"),
                status_code=303)
        return RedirectResponse("/", status_code=303)

    if settings.ad_enabled:
        ok, result, detail = authenticate_ad(uname, password)
        if ok:
            # LDAP 只把關「能否登入」；權限由帳號分權對照表決定（每請求重查）
            _login_reset(request, uname)
            role = _resolve_role(result.get("id", uname))
            request.session["user"] = result
            _audit(request, "login",
                   f"AD 登入：{uname}（{ROLE_LABELS.get(role, role)}）")
            return RedirectResponse("/", status_code=303)
        _login_failed(request, uname)
        # 對外只給一般化訊息（帳號不存在 / 密碼錯 / 不在群組 / LDAP 例外一律同一句），
        # 真實原因寫稽核，避免帳號列舉與內部資訊外洩
        _audit(request, "login_failed", f"登入失敗：{uname}（{detail}）", user=uname)
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote(str(result)), status_code=303)
    _login_failed(request, uname)
    _audit(request, "login_failed", f"登入失敗：{uname}（AD 未啟用）", user=uname)

    return RedirectResponse(
        "/login?error=帳號或密碼錯誤（AD 未啟用，請用本機管理員帳號或至設定頁啟用 AD）",
        status_code=303,
    )


@app.post("/logout")
def logout(request: Request):
    # POST：避免跨站 GET（圖片/連結）觸發登出
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------- vSphere 列舉（多 vCenter；同步 def → FastAPI 自動走 threadpool）----------
def _get_vc(vc_id: int):
    """讀取 VCenter 紀錄；vc_id=0 時退回第一筆（單 vCenter 環境免選）。"""
    with SessionLocal() as db:
        if vc_id:
            return db.get(VCenter, vc_id)
        return db.query(VCenter).order_by(VCenter.id).first()


def _with_client(fn, vc=None):
    """連線 → 執行 → 斷線。每次請求獨立連線（自然執行緒安全）。"""
    client = get_client(vc)
    client.connect()
    try:
        return fn(client)
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


# 列舉結果快取（key 帶 vCenter id）：
# - vms / datastores：背景每 30 分鐘自動刷新（頁面直接吃快取，開頁即載入）
# - 單一 VM 的磁碟清單：選取當下即時查（30 秒短快取），確保準確
_ENUM_TTL = 35 * 60.0
_DISK_TTL = 30.0
_ENUM_REFRESH_SECS = 30 * 60
_enum_cache: dict[str, tuple[float, object]] = {}


def _cached_enum(vc, key: str, producer, ttl: float = _ENUM_TTL):
    if vc is None:
        raise VSphereError("尚未設定任何 vCenter，請至設定頁新增")
    ck = f"{vc.id}:{key}"
    hit = _enum_cache.get(ck)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = _with_client(producer, vc)
    _enum_cache[ck] = (time.monotonic(), value)
    return value


async def _background_enum_refresh() -> None:
    """啟動即抓各 vCenter 的 VM / datastore 清單，之後每 30 分鐘刷新。"""
    while True:
        try:
            with SessionLocal() as db:
                vcs = db.query(VCenter).all()
                db.expunge_all()
            for vc in vcs:
                if not vc.password:
                    continue
                try:
                    vms = await asyncio.to_thread(_with_client, lambda c: c.list_vms(), vc)
                    _enum_cache[f"{vc.id}:vms"] = (time.monotonic(), vms)
                    ds = await asyncio.to_thread(
                        _with_client, lambda c: c.list_datastores(), vc)
                    _enum_cache[f"{vc.id}:datastores"] = (time.monotonic(), ds)
                except Exception:
                    pass  # 這座暫時連不上就等下一輪；頁面 miss 時仍會即時查
        except Exception:
            pass
        await asyncio.sleep(_ENUM_REFRESH_SECS)


@app.get("/api/vms")
def api_vms(vc: int = 0):
    try:
        vms = _cached_enum(_get_vc(vc), "vms", lambda c: c.list_vms())
        return {
            "vms": [
                {"name": v.name, "power_state": v.power_state,
                 "tools_running": v.tools_running, "is_template": v.is_template}
                for v in vms
            ]
        }
    except Exception as exc:
        return JSONResponse({"error": _brief_err(exc)}, status_code=502)


@app.get("/api/vms/{vm}/disks")
def api_vm_disks(vm: str, vc: int = 0):
    try:
        disks = _cached_enum(_get_vc(vc), f"disks:{vm}",
                             lambda c: c.list_disks(vm), ttl=_DISK_TTL)
        return {
            "disks": [
                {"label": d.label, "file_name": d.file_name,
                 "capacity_gb": d.capacity_gb, "datastore": d.datastore}
                for d in disks
            ]
        }
    except Exception as exc:
        return JSONResponse({"error": _brief_err(exc)}, status_code=502)


@app.get("/api/datastores")
def api_datastores(vc: int = 0):
    try:
        return {"datastores": _cached_enum(_get_vc(vc), "datastores",
                                           lambda c: c.list_datastores())}
    except Exception as exc:
        return JSONResponse({"error": _brief_err(exc)}, status_code=502)


# ---------- HTMX datalist 選項片段 ----------
def _power_zh(state: str, is_template: bool = False) -> str:
    if is_template:
        return "範本"
    return "開機" if state == "poweredOn" else "關機"


@app.get("/partials/options/vms", response_class=HTMLResponse)
def partial_vm_options(request: Request, vcenter_id: int = 0,
                       source_vcenter_id: int = 0, target_vcenter_id: int = 0):
    try:
        vc_id = source_vcenter_id or target_vcenter_id or vcenter_id
        vms = _cached_enum(_get_vc(vc_id), "vms", lambda c: c.list_vms())
        opts = [{"value": v.name,
                 "label": f"{v.name} · {_power_zh(v.power_state, v.is_template)}"}
                for v in vms]
    except Exception:
        opts = []
    return templates.TemplateResponse(request, "_options.html", {"options": opts})


@app.get("/partials/options/datastores", response_class=HTMLResponse)
def partial_datastore_options(request: Request, vcenter_id: int = 0,
                              source_vcenter_id: int = 0, target_vcenter_id: int = 0):
    try:
        vc_id = target_vcenter_id or source_vcenter_id or vcenter_id
        opts = [{"value": ds, "label": ds}
                for ds in _cached_enum(_get_vc(vc_id), "datastores",
                                       lambda c: c.list_datastores())]
    except Exception:
        opts = []
    return templates.TemplateResponse(request, "_options.html", {"options": opts})


@app.get("/partials/options/networks", response_class=HTMLResponse)
def partial_network_options(request: Request, vcenter_id: int = 0,
                            source_vcenter_id: int = 0, target_vcenter_id: int = 0):
    """目標 vCenter 的網路（portgroup）清單——vmsync 網卡對應下拉用。"""
    try:
        vc_id = target_vcenter_id or source_vcenter_id or vcenter_id
        nets = _cached_enum(_get_vc(vc_id), "networks", lambda c: c.list_networks())
        opts = [{"value": n, "label": n} for n in nets]
    except Exception:
        opts = []
    return templates.TemplateResponse(request, "_options.html", {"options": opts})


@app.get("/partials/options/disks", response_class=HTMLResponse)
def partial_disk_options(request: Request, source_vm: str = "", vcenter_id: int = 0,
                         source_vcenter_id: int = 0):
    opts = []
    if source_vm.strip():
        vm = source_vm.strip()
        try:
            disks = _cached_enum(_get_vc(source_vcenter_id or vcenter_id), f"disks:{vm}",
                                 lambda c: c.list_disks(vm), ttl=_DISK_TTL)
            opts = [
                {"value": d.label, "label": f"{d.label} · {d.capacity_gb}GB · {d.file_name}"}
                for d in disks
            ]
        except Exception:
            opts = []
    return templates.TemplateResponse(request, "_options.html", {"options": opts})


# ---------- Profile VM 即時狀態（HTMX 載入）----------
@app.get("/partials/profile-status/{profile_id}", response_class=HTMLResponse)
def partial_profile_status(request: Request, profile_id: int):
    with SessionLocal() as db:
        p = db.get(Profile, profile_id)
        if p is None:
            return HTMLResponse("找不到 Profile", status_code=404)
        source_vm, target_vm = p.source_vm, p.target_vm
        vmsync = p.job_type == "vmsync"
        if vmsync:
            target_vm = p.replica_name.strip() or f"{p.source_vm}-replica"
        src_vc, tgt_vc = p.vcenter, p.target_vcenter
        same = p.vcenter_id == p.target_vcenter_id
        db.expunge_all()
    xvm_ok, xvm_msg = _xvm_check(p)   # 非 XVM / 同座 → (True, "")

    def gather_src(c):
        src = c.get_vm(source_vm)
        writer = None
        if not vmsync:
            try:
                writer = c.check_sql_writer(source_vm, settings.guest_user,
                                            settings.guest_password)
            except Exception:
                writer = None
        tgt = c.get_vm(target_vm) if same else None
        return src, writer, tgt

    try:
        # vmsync：複本第一次同步前不存在，目標查不到不當錯誤（顯示「尚未建立」）
        if vmsync:
            src = _with_client(lambda c: c.get_vm(source_vm), src_vc)
            writer, tgt = None, None
            try:
                tgt = _with_client(lambda c: c.get_vm(target_vm), tgt_vc)
            except Exception:
                tgt = None
        else:
            src, writer, tgt = _with_client(gather_src, src_vc)
            if tgt is None:
                tgt = _with_client(lambda c: c.get_vm(target_vm), tgt_vc)
        ctx = {
            "ok": True,
            "vmsync": vmsync,
            "xvm": {"ok": xvm_ok, "msg": xvm_msg} if xvm_msg else None,
            "source": {"name": src.name, "power_state": src.power_state,
                       "tools_running": src.tools_running, "writer": writer,
                       "is_template": src.is_template},
            "target": ({"name": tgt.name, "power_state": tgt.power_state,
                        "tools_running": tgt.tools_running, "missing": False,
                        "is_template": tgt.is_template}
                       if tgt is not None
                       else {"name": target_vm, "power_state": "",
                             "tools_running": False, "missing": True,
                             "is_template": False}),
        }
    except Exception as exc:
        ctx = {"ok": False, "error": _brief_err(exc)}
    return templates.TemplateResponse(request, "_profile_status.html", ctx)


# ---------- vCenter 管理 ----------
@app.post("/vcenters")
def create_vcenter(
    request: Request,
    vc_name: str = Form(...),
    vc_host: str = Form(...),
    vc_user: str = Form(...),
    vc_password: str = Form(""),
    vc_insecure: str = Form(""),
):
    with SessionLocal() as db:
        db.add(VCenter(name=vc_name.strip(), host=vc_host.strip(), user=vc_user.strip(),
                       password=vc_password, insecure=vc_insecure == "on"))
        db.commit()
    _audit(request, "vcenter_create", f"新增 vCenter：{vc_name.strip()}（{vc_host.strip()}）")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/vcenters/{vc_id}/edit")
def edit_vcenter(
    request: Request,
    vc_id: int,
    vc_name: str = Form(...),
    vc_host: str = Form(...),
    vc_user: str = Form(...),
    vc_password: str = Form(""),
    vc_insecure: str = Form(""),
):
    with SessionLocal() as db:
        v = db.get(VCenter, vc_id)
        if v is None:
            return HTMLResponse("找不到 vCenter", status_code=404)
        v.name, v.host, v.user = vc_name.strip(), vc_host.strip(), vc_user.strip()
        v.insecure = vc_insecure == "on"
        if vc_password:  # 留空不變更
            v.password = vc_password
        db.commit()
    _enum_cache.clear()
    _audit(request, "vcenter_update", f"編輯 vCenter：{vc_name.strip()}（#{vc_id}）")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/vcenters/{vc_id}/delete")
def delete_vcenter(request: Request, vc_id: int):
    with SessionLocal() as db:
        v = db.get(VCenter, vc_id)
        if v is None:
            return HTMLResponse("找不到 vCenter", status_code=404)
        in_use = db.query(Profile).filter(
            (Profile.vcenter_id == vc_id) | (Profile.target_vcenter_id == vc_id)
        ).count()
        if in_use:
            return RedirectResponse(
                "/settings?tested=" + urllib.parse.quote(
                    f"vCenter「{v.name}」仍被 {in_use} 個任務使用，不可刪除"),
                status_code=303,
            )
        name = v.name
        db.delete(v)
        db.commit()
    _enum_cache.clear()
    _audit(request, "vcenter_delete", f"刪除 vCenter：{name}")
    return RedirectResponse("/settings?saved=1", status_code=303)


# ---------- 帳號分權（帳號→角色對照表）----------
@app.post("/roles")
def create_role(request: Request, role_username: str = Form(...),
                role_value: str = Form("full_admin")):
    uname = role_username.strip()
    if not uname or role_value not in ROLE_LABELS:
        return RedirectResponse("/settings", status_code=303)
    with SessionLocal() as db:
        dup = [r for r in db.query(AccountRole).all()
               if r.username.strip().lower() == uname.lower()]
        if dup:
            dup[0].role = role_value  # 重複新增視為更新
        else:
            db.add(AccountRole(username=uname, role=role_value))
        db.commit()
    _audit(request, "role_set", f"帳號分權：{uname} → {ROLE_LABELS[role_value]}")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/roles/{role_id}/edit")
def edit_role(request: Request, role_id: int, role_value: str = Form("full_admin")):
    if role_value not in ROLE_LABELS:
        return RedirectResponse("/settings", status_code=303)
    with SessionLocal() as db:
        row = db.get(AccountRole, role_id)
        if row is not None:
            row.role = role_value
            db.commit()
            _audit(request, "role_set",
                   f"帳號分權：{row.username} → {ROLE_LABELS[role_value]}")
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/roles/{role_id}/delete")
def delete_role(request: Request, role_id: int):
    with SessionLocal() as db:
        row = db.get(AccountRole, role_id)
        if row is not None:
            uname = row.username
            db.delete(row)
            db.commit()
            _audit(request, "role_delete",
                   f"帳號分權：移除 {uname}（回歸預設角色）")
    return RedirectResponse("/settings?saved=1", status_code=303)


def _mask_url(url: str) -> str:
    if not url:
        return ""
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}/…" if u.scheme and u.netloc else "…"


def _brief_err(exc: BaseException) -> str:
    """回前端的例外摘要：首行、≤160 字（pyVmomi/SOAP 例外常含內部路徑與多行細節），
    全文進 log。"""
    logging.getLogger("snapman").warning("%s", exc, exc_info=False)
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return first[:160]


# ---------- 設定頁（對應 config.json）----------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0, tested: str = "", error: str = ""):
    with SessionLocal() as db:
        vcenters = [
            {"id": v.id, "name": v.name, "host": v.host, "user": v.user,
             "insecure": v.insecure, "has_password": bool(v.password),
             "in_use": db.query(Profile).filter(
                 (Profile.vcenter_id == v.id) | (Profile.target_vcenter_id == v.id)
             ).count()}
            for v in db.query(VCenter).order_by(VCenter.id).all()
        ]
        account_roles = [
            {"id": r.id, "username": r.username, "role": r.role}
            for r in db.query(AccountRole).order_by(AccountRole.username).all()
        ]
    user = request.session.get("user") or {}
    admin_pw_warn = ""
    if settings.local_admin_initial:
        admin_pw_warn = "目前為系統產生的初始密碼，請立即變更"
    elif not settings.local_admin_password:
        admin_pw_warn = "本機 admin 登入已停用（未設密碼）" if settings.ad_enabled else "尚未設定"
    elif admin_password_problem(settings.local_admin_password):
        admin_pw_warn = admin_password_problem(settings.local_admin_password) + "，請立即變更"
    ctx = {
        "saved": bool(saved),
        "tested": tested,
        "error": error,
        "must_change_pw": bool(user.get("must_change_pw")),
        "admin_pw_min": ADMIN_PASSWORD_MIN_LEN,
        "vcenters": vcenters,
        "account_roles": account_roles,
        "role_options": ROLE_LABELS,
        "cfg": {
            "guest_user": settings.guest_user,
            "has_guest_password": bool(settings.guest_password),
            "admin_pw_warn": admin_pw_warn,
            # AD
            "ad_enabled": settings.ad_enabled,
            "ad_domain": settings.ad_domain,
            "ad_servers": ", ".join(settings.ad_servers),
            "ad_service_user": settings.ad_service_user,
            "ad_allowed_group": settings.ad_allowed_group,
            "ad_base_dn": settings.ad_base_dn,
            "ad_use_ssl": settings.ad_use_ssl,
            "ad_ssl_verify": settings.ad_ssl_verify,
            "has_ad_service_password": bool(settings.ad_service_password),
            # 帳號分權
            "default_role": settings.default_role,
            # 顯示
            "timezone": settings.timezone,
            # 告警
            # webhook URL 本身即 bearer secret：唯讀角色只看到 scheme+host
            "alert_webhook_url": (settings.alert_webhook_url
                                  if getattr(request.state, "role", "") == "full_admin"
                                  else _mask_url(settings.alert_webhook_url)),
            "alert_on_success": settings.alert_on_success,
            "smtp_host": settings.smtp_host,
            "smtp_port": settings.smtp_port,
            "smtp_tls": settings.smtp_tls,
            "smtp_tls_verify": settings.smtp_tls_verify,
            "smtp_user": settings.smtp_user,
            "has_smtp_password": bool(settings.smtp_password),
            "smtp_from": settings.smtp_from,
            "smtp_to": settings.smtp_to,
            # 保留
            "run_retention_days": settings.run_retention_days,
            # 併發
            "max_concurrent_runs": settings.max_concurrent_runs,
        },
    }
    return templates.TemplateResponse(request, "settings.html", ctx)


@app.post("/settings")
def save_settings_endpoint(
    request: Request,
    guest_user: str = Form(""),
    guest_password: str = Form(""),
    local_admin_password: str = Form(""),
    ad_enabled: str = Form(""),
    ad_domain: str = Form(""),
    ad_servers: str = Form(""),
    ad_service_user: str = Form(""),
    ad_service_password: str = Form(""),
    ad_allowed_group: str = Form(""),
    ad_base_dn: str = Form(""),
    ad_use_ssl: str = Form(""),
    ad_ssl_verify: str = Form(""),
    default_role: str = Form("admin_readonly"),
    timezone: str = Form(""),
    alert_webhook_url: str = Form(""),
    alert_on_success: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(25),
    smtp_tls: str = Form(""),
    smtp_tls_verify: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_to: str = Form(""),
    run_retention_days: int = Form(90),
    max_concurrent_runs: int = Form(1),
):
    updates = {
        "guest_user": guest_user.strip(),
        "ad_enabled": ad_enabled == "on",
        "ad_domain": ad_domain.strip(),
        "ad_servers": [s.strip() for s in ad_servers.split(",") if s.strip()],
        "ad_service_user": ad_service_user.strip(),
        "ad_allowed_group": ad_allowed_group.strip(),
        "ad_base_dn": ad_base_dn.strip(),
        "ad_use_ssl": ad_use_ssl == "on",
        "ad_ssl_verify": ad_ssl_verify == "on",
        "default_role": default_role if default_role in ROLE_LABELS else "admin_readonly",
        "timezone": timezone.strip(),
        "alert_webhook_url": alert_webhook_url.strip(),
        "alert_on_success": alert_on_success == "on",
        "smtp_host": smtp_host.strip(),
        "smtp_port": smtp_port,
        "smtp_tls": smtp_tls == "on",
        "smtp_tls_verify": smtp_tls_verify == "on",
        "smtp_user": smtp_user.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_to": smtp_to.strip(),
        "run_retention_days": max(0, run_retention_days),
        "max_concurrent_runs": max(1, max_concurrent_runs),
    }
    # webhook URL 先驗（scheme / 主機；防 file:// 與 loopback 探測）
    from .notify import validate_webhook_url
    if (wp := validate_webhook_url(alert_webhook_url.strip())):
        return RedirectResponse("/settings?error=" + urllib.parse.quote(wp), status_code=303)
    # 本機 admin 密碼先驗規則（不合格整筆不存，避免其他欄位半套用讓人誤以為成功）
    if local_admin_password and (problem := admin_password_problem(local_admin_password)):
        return RedirectResponse("/settings?error=" + urllib.parse.quote(problem), status_code=303)
    # 密碼欄位留空 = 不變更（避免把畫面上的遮罩存成真正密碼）
    if guest_password:
        updates["guest_password"] = guest_password
    if ad_service_password:
        updates["ad_service_password"] = ad_service_password
    if smtp_password:
        updates["smtp_password"] = smtp_password
    save_settings(updates)
    if local_admin_password:
        set_local_admin_password(local_admin_password)   # 落地、清初始旗標、刪初始密碼檔
        u = request.session.get("user") or {}
        if u.get("must_change_pw"):
            request.session["user"] = {**u, "must_change_pw": False}
    _enum_cache.clear()  # vCenter 連線資訊可能變了，快取作廢
    _audit(request, "settings_update",
           "儲存系統設定" + ("（含本機 admin 密碼）" if local_admin_password else ""))
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/test-alert")
async def test_alert(request: Request):
    from .notify import send_test
    results = await asyncio.to_thread(send_test)
    _audit(request, "alert_test", "；".join(results))
    return RedirectResponse(
        "/settings?tested=" + urllib.parse.quote("；".join(results)), status_code=303
    )


# ---------- 容量 ----------
def _fmt_gb(nbytes: int) -> str:
    return f"{nbytes / (1024 ** 3):,.1f} GB"


@app.get("/capacity", response_class=HTMLResponse)
def capacity_page(request: Request):
    # 依 vCenter 分組收集各任務的目標 datastore（同時記下引用它的任務名，錯誤時好找源頭）
    groups: dict[int, list] = {}
    with SessionLocal() as db:
        for p in db.query(Profile).all():
            vc = p.target_vcenter or p.vcenter  # 副本位在目標側 datastore
            if p.target_datastore and vc is not None:
                ent = groups.setdefault(vc.id, [vc, {}])
                ent[1].setdefault(p.target_datastore, []).append(p.name)
        for vc, _ in groups.values():
            db.expunge(vc)
    items: list[dict] = []

    def make_gather(names):
        # 逐 datastore 容錯：某一個查不到（改名/卸載/任務設定過期）只影響那一列，
        # 其餘 datastore 照常呈現；整頁只在 vCenter 連線本身失敗時才整組報錯
        def gather(c):
            out = []
            for n in names:
                try:
                    out.append((n, c.datastore_usage(n), c.list_snapman_files(n), ""))
                except Exception as exc:
                    out.append((n, None, [], str(exc)))
            return out
        return gather

    for vc, ds_map in groups.values():
        names = sorted(ds_map)
        try:
            rows = _with_client(make_gather(names), vc)
        except Exception as exc:
            rows = [(n, None, [], f"vCenter 連線失敗：{exc}") for n in names]
        for name, usage, files, err in rows:
            label = f"{name}（{vc.name}）"
            jobs = "、".join(sorted(ds_map.get(name, [])))
            if err:
                items.append({"name": label, "error": err, "jobs": jobs})
                continue
            used = usage["capacity"] - usage["free"]
            logical = sum(f["size"] for f in files)
            items.append({
                "name": label,
                "jobs": jobs,
                "capacity": _fmt_gb(usage["capacity"]),
                "free": _fmt_gb(usage["free"]),
                "used_pct": round(used * 100 / usage["capacity"]) if usage["capacity"] else 0,
                "clone_count": len(files),
                "clone_logical": _fmt_gb(logical),
                "files": [{**f, "size_h": _fmt_gb(f["size"])} for f in files],
            })
    return templates.TemplateResponse(request, "capacity.html", {"items": items})


# ---------- 稽核記錄 ----------
# 系統性動作的記錄者（保留策略、排程觸發、串接、啟動復原等）；
# 其餘視為人為操作。頁面依此分成「操作稽核 / 系統事件」兩個頁籤（參考 WEB_ERS）
_SYS_AUDIT_USERS = {"系統", "排程器", "串接"}


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    def _fmt(a: AuditLog) -> dict:
        return {"ts": to_display(a.ts, "%Y-%m-%d %H:%M:%S"), "user": a.user,
                "action": a.action, "detail": a.detail, "ip": a.ip}

    with SessionLocal() as db:
        rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(600).all()
    ops = [_fmt(a) for a in rows if a.user not in _SYS_AUDIT_USERS][:300]
    sys_events = [_fmt(a) for a in rows if a.user in _SYS_AUDIT_USERS][:300]
    return templates.TemplateResponse(
        request, "logs.html", {"ops": ops, "sys_events": sys_events})


@app.post("/profiles/{profile_id}/run")
async def run_profile(request: Request, profile_id: int):
    try:
        run_id = await run_manager.start(profile_id)
    except VSphereError as exc:
        return RedirectResponse(
            "/jobs?error=" + urllib.parse.quote(str(exc)), status_code=303)
    _audit(request, "run_start", f"手動執行 Profile #{profile_id} → Run #{run_id}")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/stop")
def stop_run(request: Request, run_id: int):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            return HTMLResponse("找不到 Run", status_code=404)
        if run.status in ("pending", "running"):
            run_manager.request_stop(run_id)
            _audit(request, "run_stop", f"要求停止 Run #{run_id}（{run.profile.name}）")
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int):
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            return HTMLResponse("找不到 Run", status_code=404)
        steps = [
            {"seq": s.seq, "key": s.key, "title": s.title, "status": s.status,
             "message": s.message,
             "started_at": _local(s.started_at), "finished_at": _local(s.finished_at)}
            for s in run.steps
        ]
        done = sum(1 for s in steps if s["status"] == "done")
        # 事件日誌歷史重建：即時串流不持久化，事後開頁原本一片空白——
        # 以 StepLog 起訖/訊息 + 執行期間的保留策略稽核還原時間軸。
        # 條目結構化（no/text 拆開），步驟編號渲染成與步驟表一致的綠底徽章
        def _hist(t, status, title, message=""):
            st = _stepno(title)
            return {"time": _local(t), "status": status,
                    "no": st["no"], "text": st["text"], "message": message or ""}

        events: list[tuple] = []
        for s in run.steps:
            if s.started_at:
                events.append((s.started_at, _hist(s.started_at, "running", s.title)))
            if s.finished_at:
                events.append((s.finished_at,
                               _hist(s.finished_at, s.status, s.title, s.message)))
        if run.started_at:
            aud = db.query(AuditLog).filter(
                AuditLog.action == "retention", AuditLog.ts >= run.started_at)
            if run.finished_at:
                aud = aud.filter(AuditLog.ts <= run.finished_at)
            for a in aud.all():
                events.append((a.ts, _hist(a.ts, "retention", a.detail)))
        events.sort(key=lambda e: str(e[0]))
        ctx = {
            "run_id": run.id, "profile": run.profile.name, "status": run.status,
            "error": run.error, "steps": steps,
            "done_count": done,
            "percent": round(done * 100 / len(steps)) if steps else 0,
            "history": [e[1] for e in events],
        }
    return templates.TemplateResponse(request, "run.html", ctx)


# ---------- WebSocket 即時進度 ----------
@app.websocket("/ws/runs/{run_id}")
async def ws_run(websocket: WebSocket, run_id: int):
    # http middleware 攔不到 WebSocket，這裡自行驗證 session
    if not websocket.session.get("user"):
        await websocket.close(code=1008)  # policy violation
        return
    await websocket.accept()
    # 先訂閱再查狀態，避免「查完發現在跑、訂閱前剛好結束」的縫隙
    q = run_manager.subscribe(run_id)
    try:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            status = run.status if run else None
            error = run.error if run else None
        if status is None:
            await websocket.send_json({"type": "done"})
            return
        if status in ("success", "failed", "stopped"):
            # run 已結束：直接回放最終狀態並收線，不留永久掛著的連線
            event: dict = {"type": "run", "status": status}
            if error:
                event["error"] = error
            await websocket.send_json(event)
            await websocket.send_json({"type": "done"})
            return
        while True:
            event = await q.get()
            await websocket.send_json(event)
            if event.get("type") == "done":
                break
    except WebSocketDisconnect:
        pass
    finally:
        run_manager.unsubscribe(run_id, q)


# ---------- 進入點：python -m app.main（綁定 settings.web_host / web_port）----------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.web_host, port=settings.web_port,
                log_config=LOG_CONFIG)
