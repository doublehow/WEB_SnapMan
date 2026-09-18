"""AD（NTLM）驗證。

移植自 VC_Dashboard，改為從 settings 動態讀取設定（而非全域變數）。
驗證流程：Service Account 搜尋使用者 → 使用者帳密 bind 驗密 → 群組授權。
"""
from __future__ import annotations

# ── MD4 相容性修補（Python 3.9+/OpenSSL 3.x 停用 MD4，NTLM 需要）──
import hashlib
import logging
import os
import secrets

try:
    hashlib.new("md4")
except ValueError:
    from Crypto.Hash import MD4 as _MD4_impl

    class _MD4Shim:
        name = "md4"
        digest_size = 16
        block_size = 64

        def __init__(self, d=b""):
            self._h = _MD4_impl.new(d)

        def update(self, d):
            self._h.update(d)
            return self

        def digest(self):
            return self._h.digest()

        def hexdigest(self):
            return self._h.hexdigest()

        def copy(self):
            c = _MD4Shim()
            c._h = self._h.copy()
            return c

    _orig_hashlib_new = hashlib.new

    def _patched_hashlib_new(name, *args, **kwargs):
        if name.lower() == "md4":
            return _MD4Shim(args[0] if args else b"")
        return _orig_hashlib_new(name, *args, **kwargs)

    hashlib.new = _patched_hashlib_new
# ── MD4 修補結束 ──

import ssl

from ldap3 import ALL, FIRST, NTLM, SUBTREE, Connection, Server, ServerPool, Tls
from ldap3.utils.conv import escape_filter_chars

from .config import INITIAL_ADMIN_PW_FILE, save_settings, settings

logger = logging.getLogger("snapman.auth")

# ---------- 本機 admin 密碼生命週期（參考 VCOD 的做法）----------
ADMIN_PASSWORD_MIN_LEN = 12


def admin_password_problem(pw: str) -> str:
    """本機 admin 新密碼規則（設定頁與 set_admin_password CLI 共用）；回錯誤訊息，合格回空字串。"""
    if len(pw) < ADMIN_PASSWORD_MIN_LEN:
        return f"本機 admin 密碼至少 {ADMIN_PASSWORD_MIN_LEN} 字元"
    if pw.strip().lower() == "admin":
        return "本機 admin 密碼不得為 admin"
    return ""


def local_admin_must_change(password_used: str) -> bool:
    """以此密碼登入後是否須強制變更：系統產生的初始密碼，或不符現行規則（如舊版預設 admin）。"""
    return bool(settings.local_admin_initial) or bool(admin_password_problem(password_used))


def set_local_admin_password(pw: str) -> None:
    """寫入新密碼、清初始旗標、刪初始密碼檔；設定頁與 CLI 共用。呼叫端先過 admin_password_problem。"""
    save_settings({"local_admin_password": pw, "local_admin_initial": False})
    try:
        INITIAL_ADMIN_PW_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("無法刪除初始密碼檔 %s：%s，請手動刪除", INITIAL_ADMIN_PW_FILE, exc)


def bootstrap_local_admin() -> None:
    """啟動時的本機 admin 密碼檢查：
    - 空且 AD 未啟用（否則無任何登入方式）→ 產生隨機初始密碼，存 config.json、明文寫
      data/initial_admin_password.txt（僅擁有者可讀）並標 local_admin_initial；首次登入強制變更後自動刪檔。
    - 仍為舊版預設 admin 或不符規則 → 不拒絕啟動（否則無法進 UI 改），改由登入後強制變更。"""
    if not settings.local_admin_password and not settings.ad_enabled:
        pw = secrets.token_urlsafe(12)
        save_settings({"local_admin_password": pw, "local_admin_initial": True})
        try:
            INITIAL_ADMIN_PW_FILE.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(INITIAL_ADMIN_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pw + "\n")
            logger.critical("本機 admin 尚未設定密碼，已產生初始密碼並寫入 %s；"
                            "請以 admin 登入後立即於設定頁變更（變更後此檔自動刪除）",
                            INITIAL_ADMIN_PW_FILE)
        except OSError as exc:
            logger.critical("本機 admin 初始密碼已產生但無法寫入 %s（%s）；"
                            "請在伺服器執行 python -m app.set_admin_password 重設",
                            INITIAL_ADMIN_PW_FILE, exc)
    elif settings.local_admin_password and admin_password_problem(settings.local_admin_password):
        logger.warning("本機 admin 密碼不符規則（%s）；下次以 admin 登入將被強制變更",
                       admin_password_problem(settings.local_admin_password))


_GENERIC_FAIL = "帳號或密碼錯誤"


def authenticate_ad(username: str, password: str) -> tuple[bool, object, str]:
    """回傳 (True, {'id','name'}, "") 或 (False, 對外訊息, 稽核細節)。
    對外訊息不區分「帳號不存在 / 密碼錯 / 不在群組 / LDAP 例外」（防帳號列舉、
    不洩漏 AD 內部錯誤）；真實原因放第三元素只進稽核紀錄。設定類錯誤
    （未設 AD 伺服器、Base DN 偵測失敗）屬管理者要看的，照實回。"""
    # 統一帳號格式為純 sAMAccountName
    if "\\" in username:
        username = username.split("\\", 1)[1]
    elif "/" in username:
        username = username.split("/", 1)[1]
    elif "@" in username:
        username = username.split("@", 1)[0]
    username = username.strip()

    domain = settings.ad_domain
    server_ips = settings.ad_servers
    svc_user = settings.ad_service_user
    svc_pass = settings.ad_service_password
    allowed_group = settings.ad_allowed_group
    base_dn = settings.ad_base_dn

    if not server_ips:
        return False, "尚未設定 AD 伺服器", "ad_servers 為空"

    # LDAPS（選項）：389 明文時 NTLM 交握與 memberOf 皆在網路上可見；開啟 ad_use_ssl
    # 走 636，並依 ad_ssl_verify 決定是否驗證 AD 憑證（自簽 CA 未匯入本機信任時可關）
    if settings.ad_use_ssl:
        tls = Tls(validate=ssl.CERT_REQUIRED if settings.ad_ssl_verify else ssl.CERT_NONE,
                  version=ssl.PROTOCOL_TLS_CLIENT)
        servers = [Server(ip, port=636, use_ssl=True, tls=tls, get_info=ALL) for ip in server_ips]
    else:
        servers = [Server(ip, get_info=ALL) for ip in server_ips]
    server_pool = ServerPool(servers, pool_strategy=FIRST)
    full_svc = f"{domain}\\{svc_user}" if "\\" not in svc_user else svc_user

    try:
        # Step 1：Service Account 連線搜尋使用者
        conn = Connection(
            server_pool, user=full_svc, password=svc_pass,
            authentication=NTLM, auto_bind=True,
        )
        if not base_dn:
            try:
                if conn.server and conn.server.info:
                    base_dn = conn.server.info.other.get("defaultNamingContext", [None])[0]
            except Exception:
                base_dn = None
            if not base_dn:
                return False, "無法自動偵測 Base DN，請於設定頁手動填寫", "defaultNamingContext 取得失敗"
            # 自動偵測成功則回存，之後免再偵測
            save_settings({"ad_base_dn": base_dn})

        conn.search(
            base_dn,
            # escape：使用者輸入不可直接組進 LDAP filter（注入可操縱找到哪個 entry）
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))",
            attributes=["distinguishedName", "memberOf", "displayName"],
            search_scope=SUBTREE,
        )
        if not conn.entries:
            return False, _GENERIC_FAIL, "找不到該使用者帳號"

        entry = conn.entries[0]
        display_name = entry.displayName.value if "displayName" in entry else username
        member_of = entry.memberOf.value if "memberOf" in entry else []
        conn.unbind()

        # Step 2：使用者帳密驗密
        user_conn = Connection(
            server_pool, user=f"{domain}\\{username}",
            password=password, authentication=NTLM,
        )
        if not user_conn.bind():
            return False, _GENERIC_FAIL, "密碼錯誤"
        user_conn.unbind()

        # Step 3：群組授權——精確比對群組 DN 的第一個 RDN（CN=xxx）。
        # 子字串比對會把「Admin」誤配到「NotAdmin」等群組；設定值若填
        # 完整 DN（含 =）則整串比對。
        def _group_match(gdn: str) -> bool:
            if "=" in allowed_group:
                return gdn.strip().lower() == allowed_group.strip().lower()
            first = gdn.split(",", 1)[0].strip()
            return ("=" in first
                    and first.split("=", 1)[1].strip().lower() == allowed_group.lower())

        if isinstance(member_of, str):
            member_of = [member_of]
        for gdn in member_of:
            if _group_match(gdn):
                return True, {"id": username, "name": display_name}, ""
        return False, _GENERIC_FAIL, f"驗證通過但不在授權群組（{allowed_group}）"

    except Exception as exc:
        logger.warning("AD 驗證例外（%s）：%s", username, exc)
        return False, "AD 驗證失敗，請稍後再試或聯絡管理員", f"LDAP 例外：{exc}"
