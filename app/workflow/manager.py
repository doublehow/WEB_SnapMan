"""RunManager：啟動工作流（每日/掛載歷史副本）、持久化步驟、廣播即時進度、
副本記帳與保留策略、結果告警。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from types import SimpleNamespace

from ..config import settings
from ..database import SessionLocal
from ..models import AuditLog, CloneCopy, Profile, Run, StepLog
from ..notify import notify_run_result
from ..vsphere import get_client
from ..vsphere.base import VSphereError
from .steps import MOUNT_STEPS, STEPS, VMSYNC_STEPS, StepContext


from ..timeutil import now_hms as _now_hms  # WebSocket 事件時戳（依顯示時區）


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class _StopRequested(Exception):
    """使用者要求停止（於步驟邊界或 clone 取消時拋出）。"""


def _is_file_not_found(exc: Exception) -> bool:
    """vSphere 刪檔錯誤是否為「檔案不存在」（可安全移除紀錄）。
    比對訊息須含 .vmdk（避免把 datastore 不可見誤判成檔案不存在）。"""
    s = str(exc)
    return ".vmdk" in s and ("not found" in s.lower() or "找不到" in s)


def _vm_keys(snap: dict) -> list[tuple[str, str]]:
    """run 需要獨占的 VM 互斥 key：(vCenter host, VM 名)，不分大小寫。
    來源與目標各一（同一台則只有一個 key）。"""
    return list({
        (snap["src_vc"]["host"].strip().lower(), snap["source_vm"].strip().lower()),
        (snap["tgt_vc"]["host"].strip().lower(), snap["target_vm"].strip().lower()),
    })


class RunManager:
    """管理進行中的 run 與其進度訂閱者。"""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = {}
        # 進行中的 profile_id。start() 在同一個 event-loop tick 內「檢查＋登記」，
        # 中間沒有 await，因此不會有兩個請求同時通過檢查（先前用 lock.locked()
        # 檢查、稍後才在背景 task 取鎖的寫法存在 race，已以此取代）。
        self._running: set[int] = set()
        # VM 互斥鎖：(vCenter host, VM 名) → 持有的 profile_id。
        # 共用同一台來源/目標 VM 的任務不並行（掛碟 SCSI unit、guest 內
        # 磁碟 online/碟號腳本會互踩），全域上限可放大、安全由此結構保證
        self._vm_locks: dict[tuple[str, str], int] = {}
        # 使用者要求停止的 run_id（步驟邊界檢查；clone 中另會取消 vSphere 工作）
        self._cancel: set[int] = set()
        # 本程序內仍在執行的 run_id：request_stop 只接受這裡面的 id，
        # 避免對已結束 run 的停止請求在 _cancel 永久殘留
        self._active_runs: set[int] = set()

    def request_stop(self, run_id: int) -> None:
        if run_id in self._active_runs:
            self._cancel.add(run_id)

    def active_count(self) -> int:
        return len(self._running)

    # --- 訂閱（給 WebSocket 用）---
    def subscribe(self, run_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: int, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id)
        if subs:
            subs.discard(q)

    def is_running(self, profile_id: int) -> bool:
        return profile_id in self._running

    async def _emit(self, run_id: int, event: dict) -> None:
        for q in list(self._subscribers.get(run_id, ())):
            await q.put(event)

    # --- 啟動 ---
    async def start(self, profile_id: int) -> int:
        """每日完整工作流（①~⑩）。"""
        return await self._begin(profile_id, STEPS, "daily")

    async def start_mount(self, profile_id: int, copy_id: int, kind: str = "mount") -> int:
        """掛載指定的歷史副本（卸下目前的 → 掛上選定的 → ATTACH → 驗證）。"""
        return await self._begin(profile_id, MOUNT_STEPS, kind, copy_id=copy_id)

    async def _begin(self, profile_id: int, steps, kind: str, copy_id: int | None = None) -> int:
        if profile_id in self._running:
            raise VSphereError("此 Profile 已有一個執行中的任務，請稍後再試")
        if len(self._running) >= max(1, settings.max_concurrent_runs):
            raise VSphereError(
                f"已達全域同時執行上限（{settings.max_concurrent_runs}），請稍後再試"
            )
        self._running.add(profile_id)  # 同步佔位；_run 的 finally 釋放
        try:
            # 建 Run + StepLog 骨架
            with SessionLocal() as db:
                profile = db.get(Profile, profile_id)
                if profile is None:
                    raise VSphereError("找不到 Profile")
                if profile.vcenter is None or profile.target_vcenter is None:
                    raise VSphereError("此任務尚未綁定來源/目標 vCenter，請至任務編輯頁設定")
                # 整機同步任務：每日工作流換用 vmsync 步驟；副本掛載/驗證不適用
                if profile.job_type == "vmsync":
                    if copy_id is not None or kind != "daily":
                        raise VSphereError("整機同步任務不支援副本掛載/驗證")
                    steps, kind = VMSYNC_STEPS, "vmsync"
                snapshot = _profile_snapshot(profile)
                # 同 VM 互斥：來源/目標 VM 與任何執行中任務重疊即暫緩。
                # 從檢查到登記皆無 await（同一 event-loop tick），不會 race；
                # 排程器視同額滿，下一輪自動補跑
                conflict = next((k for k in _vm_keys(snapshot) if k in self._vm_locks), None)
                if conflict is not None:
                    raise VSphereError(
                        f"與執行中任務（Profile #{self._vm_locks[conflict]}）共用 VM"
                        f"「{conflict[1]}」，為避免互相干擾已暫緩；排程會自動補跑，"
                        f"手動執行請稍後再試"
                    )
                for k in _vm_keys(snapshot):
                    self._vm_locks[k] = profile_id
                if copy_id is not None:
                    copy = db.get(CloneCopy, copy_id)
                    if copy is None or copy.profile_id != profile_id:
                        raise VSphereError("找不到指定的副本")
                    if copy.status == "mounted":
                        raise VSphereError("該副本目前已掛載中")
                    snapshot["mount_path"] = copy.path
                    snapshot["mount_files"] = (
                        json.loads(copy.files_json) if copy.files_json else None
                    )
                run = Run(profile_id=profile_id, status="pending", kind=kind)
                db.add(run)
                db.flush()
                run_id = run.id
                # 先登記 active 再 commit：commit 後其他執行緒才查得到這筆 run，
                # 屆時 request_stop 一定已被接受
                self._active_runs.add(run_id)
                for seq, (key, title, _fn) in enumerate(steps):
                    db.add(StepLog(run_id=run_id, seq=seq, key=key, title=title, status="pending"))
                db.commit()
        except Exception:
            self._running.discard(profile_id)
            self._release_vm_locks(profile_id)
            if "run_id" in locals():
                self._active_runs.discard(run_id)
            raise

        asyncio.create_task(self._run(run_id, profile_id, snapshot, steps, kind))
        return run_id

    def _release_vm_locks(self, profile_id: int) -> None:
        for k in [k for k, pid in self._vm_locks.items() if pid == profile_id]:
            del self._vm_locks[k]

    async def _run(self, run_id: int, profile_id: int, snap: dict, steps, kind: str) -> None:
        t0 = time.monotonic()
        chain_next: int | None = None
        try:
            client = get_client(SimpleNamespace(**snap["src_vc"]))
            # 同一座 vCenter 共用連線；跨 VC 時各自建線
            tgt_client = (client if snap["same_vc"]
                          else get_client(SimpleNamespace(**snap["tgt_vc"])))
            # 進度回報：步驟（如④ clone）在 worker thread 內呼叫，
            # 用 run_coroutine_threadsafe 切回 event loop 廣播
            loop = asyncio.get_running_loop()
            current_seq = {"v": -1}

            def emit_progress(percent: int) -> None:
                asyncio.run_coroutine_threadsafe(
                    self._emit(run_id, {"type": "progress",
                                        "seq": current_seq["v"], "percent": percent}),
                    loop,
                )

            def emit_log(message: str) -> None:
                # 事件日誌 + 終端 log（OVF 串流每 3 分鐘的進度/速率報告等）
                asyncio.run_coroutine_threadsafe(
                    self._emit(run_id, {"type": "log", "message": message}), loop)
                logging.getLogger("snapman").info("Run #%d %s", run_id, message)

            ctx = StepContext(
                client=client,
                tgt_client=tgt_client,
                source_vm=snap["source_vm"],
                source_disk=snap["source_data_disk"],
                target_vm=snap["target_vm"],
                target_datastore=snap["target_datastore"],
                target_drive=snap["target_drive_letter"],
                databases=snap["database_list"],
                prev_clone_paths=snap["prev_clone_paths"],
                guest_user=settings.guest_user,
                guest_pass=settings.guest_password,
                source_sql_instance=snap["source_sql_instance"],
                target_sql_instance=snap["target_sql_instance"],
                progress=emit_progress,
                log=emit_log,
                transfer_mode=snap.get("transfer_mode", "shared"),
                staging_datastore=snap.get("staging_datastore", ""),
                tgt_vc_creds=snap["tgt_vc"],
                replica_name=snap.get("replica_name", ""),
                target_network=snap.get("target_network", ""),
                profile_id=snap.get("profile_id", 0),
            )
            ctx.should_cancel = lambda: run_id in self._cancel
            if kind in ("mount", "verify"):
                # 掛載歷史副本：目標碟預設為選定副本、檔案清單用副本記錄
                ctx.new_clone_path = snap["mount_path"]
                ctx.db_files_override = snap.get("mount_files")
            if kind == "verify":
                ctx.drill_query = snap.get("drill_query", "")
            self._set_run_status(run_id, "running")
            await self._emit(run_id, {"type": "run", "status": "running"})

            try:
                await asyncio.to_thread(client.connect)
                if tgt_client is not client:
                    await asyncio.to_thread(tgt_client.connect)
                for seq, (key, title, fn) in enumerate(steps):
                    if run_id in self._cancel:
                        for rest in range(seq, len(steps)):
                            await self._step_update(run_id, rest, "skipped", "使用者停止")
                        raise _StopRequested()
                    current_seq["v"] = seq
                    await self._step_update(run_id, seq, "running", "")
                    await self._emit(run_id, {"type": "step", "seq": seq, "key": key,
                                              "title": title, "status": "running", "message": "",
                                              "time": _now_hms()})
                    try:
                        msg = await fn(ctx)
                    except Exception as exc:  # 單步失敗
                        await self._step_update(run_id, seq, "failed", str(exc))
                        await self._emit(run_id, {"type": "step", "seq": seq, "key": key,
                                                  "title": title, "status": "failed", "message": str(exc),
                                                  "time": _now_hms()})
                        raise
                    await self._step_update(run_id, seq, "done", msg)
                    await self._emit(run_id, {"type": "step", "seq": seq, "key": key,
                                              "title": title, "status": "done", "message": msg,
                                              "time": _now_hms()})
                    # 記帳「即時」跟上實際狀態，而非成功時才更新：
                    # ⑥完成＝目標上已無舊碟 → 清空指標、舊副本標記 kept；
                    # ⑦完成＝新碟已掛 → 立即記錄指標、副本標記 mounted。
                    # 否則後續步驟失敗會留下過期指標，下一輪⑥會清錯對象
                    # （實測 Run #7 ⑨失敗 → Run #8 兩顆 clone 並存）。
                    if key == "clone":
                        self._copy_upsert(profile_id, ctx.new_clone_path,
                                          status="created", run_id=run_id)
                    elif key == "transfer" and kind == "daily":
                        # xvc 搬遷後路徑改變（暫存 → 目的端），更新副本紀錄
                        self._copy_repath(profile_id, run_id, ctx.new_clone_path)
                    elif key == "detach_old":
                        self._set_last_clone(profile_id, None)
                        remap = ctx.scratch.get("detach_remap", {})
                        for p in snap["prev_clone_paths"]:
                            actual = remap.get(p, p)
                            if actual != p:
                                self._copy_rename(profile_id, p, actual)
                            self._copy_upsert(profile_id, actual, status="kept")
                        for p in ctx.scratch.get("detach_extras", []):
                            self._copy_upsert(profile_id, p, status="kept")
                    elif key == "attach_new":
                        self._set_last_clone(profile_id, ctx.new_clone_path)
                        self._copy_upsert(profile_id, ctx.new_clone_path, status="mounted")
                    elif key == "attach_db" and ctx.scratch.get("db_files"):
                        self._copy_store_files(
                            profile_id, ctx.new_clone_path, ctx.scratch["db_files"]
                        )

                # 保留策略：只有每日工作流做清理（掛載歷史副本不刪任何東西）。
                # 副本檔案位在共享/目標側 datastore，用目標 client 刪除；
                # 跨 VC 時另傳來源 client（xvc 暫存殘留在來源側 datastore）
                if kind == "daily":
                    await self._retention_sweep(
                        run_id, profile_id, snap, tgt_client,
                        None if tgt_client is client else client)
                    await self._archive_copy(run_id, profile_id, snap, ctx, tgt_client)
                if kind in ("daily", "vmsync"):
                    chain_next = snap.get("chain_next_id")

                self._set_run_status(run_id, "success")
                await self._emit(run_id, {"type": "run", "status": "success"})
                await self._notify(run_id, snap, "success", None, t0)

            except _StopRequested:
                await self._rollback(run_id, ctx)
                self._set_run_status(run_id, "stopped", error="已由使用者停止")
                await self._emit(run_id, {"type": "run", "status": "stopped",
                                          "error": "已由使用者停止"})
                await self._notify(run_id, snap, "stopped", "已由使用者停止", t0)
            except Exception as exc:
                # clone 取消也會以 vSphere 工作失敗呈現 → 歸類為使用者停止
                if run_id in self._cancel:
                    await self._rollback(run_id, ctx)
                    self._set_run_status(run_id, "stopped", error="已由使用者停止")
                    await self._emit(run_id, {"type": "run", "status": "stopped",
                                              "error": "已由使用者停止"})
                    await self._notify(run_id, snap, "stopped", "已由使用者停止", t0)
                else:
                    await self._rollback(run_id, ctx)
                    self._set_run_status(run_id, "failed", error=str(exc))
                    await self._emit(run_id, {"type": "run", "status": "failed", "error": str(exc)})
                    await self._notify(run_id, snap, "failed", str(exc), t0)
            finally:
                for c in {id(client): client, id(tgt_client): tgt_client}.values():
                    try:
                        await asyncio.to_thread(c.disconnect)
                    except Exception:
                        pass
                await self._emit(run_id, {"type": "done"})
        finally:
            self._running.discard(profile_id)
            self._release_vm_locks(profile_id)
            self._cancel.discard(run_id)
            self._active_runs.discard(run_id)
            if chain_next:
                # 任務串接：等本 run 釋放名額後再觸發，避免被併發上限擋下
                asyncio.create_task(self._chain_start(chain_next))

    async def _chain_start(self, next_profile_id: int) -> None:
        try:
            rid = await self.start(next_profile_id)
            detail = f"串接觸發 Profile #{next_profile_id} → Run #{rid}"
        except Exception as exc:
            detail = f"串接觸發 Profile #{next_profile_id} 失敗：{exc}"
        try:
            with SessionLocal() as db:
                db.add(AuditLog(user="串接", action="run_start", detail=detail))
                db.commit()
        except Exception:
            pass

    async def _archive_copy(self, run_id: int, profile_id: int, snap: dict,
                            ctx: StepContext, client) -> None:
        """副本歸檔：把當日 clone 再複製一份到歸檔 datastore，並依 archive_keep 修剪。
        歸檔失敗不影響 run 結果，只記事件日誌。"""
        if not (snap.get("archive_enabled") and snap.get("archive_datastore")):
            return
        if not ctx.new_clone_path:
            return
        try:
            base = ctx.new_clone_path.rsplit("/", 1)[-1]
            await self._emit(run_id, {"type": "log",
                                      "message": f"歸檔：開始複製到 {snap['archive_datastore']}…"})
            dest = await asyncio.to_thread(
                client.copy_vmdk, ctx.new_clone_path, snap["archive_datastore"], base)
            self._copy_upsert(profile_id, dest, status="archived", run_id=run_id)
            await self._emit(run_id, {"type": "log", "message": f"歸檔完成：{dest}"})
            # 修剪歸檔份數
            keep = max(1, int(snap.get("archive_keep", 7)))
            with SessionLocal() as db:
                arch = (
                    db.query(CloneCopy)
                    .filter(CloneCopy.profile_id == profile_id,
                            CloneCopy.status == "archived")
                    .order_by(CloneCopy.created_at.desc())
                    .all()
                )
                victims = [(c.id, c.path) for c in arch[keep:]]
            # 與保留策略同語意：刪檔失敗「保留紀錄」下一輪重試，檔案確定不在才移除
            # 紀錄（孤兒檔掃描不涵蓋 snapman-archive/，紀錄一掉檔案就永遠無人管）
            for cid, path in victims:
                gone, note = await self._delete_copy_file(path, client)
                if gone:
                    with SessionLocal() as db:
                        row = db.get(CloneCopy, cid)
                        if row is not None:
                            db.delete(row)
                            db.commit()
                note = note.replace("保留策略：", "歸檔保留：", 1)
                await self._emit(run_id, {"type": "log", "message": note})
                self._sys_audit("retention", note)
        except Exception as exc:
            await self._emit(run_id, {"type": "log", "message": f"歸檔失敗：{exc}"})

    async def _retention_sweep(self, run_id: int, profile_id: int, snap: dict,
                               client, alt_client=None) -> None:
        """依 keep_copies 刪除多餘的 kept 副本、失敗殘留的 created 孤兒，
        並「對帳 datastore 實際檔案」刪除紀錄外的孤兒檔。清理失敗不影響 run 結果。

        - 刪檔失敗時「保留紀錄」下一輪重試（先前版本失敗即移除紀錄，
          造成檔案自此無人管、保留份數形同失效——JOB01 實測 datastore 上
          累積 9 份而紀錄只剩 4 份即此缺陷）；檔案已不存在才移除紀錄。
        - alt_client：跨 VC 時的另一座 client（xvc 暫存殘留在來源側 datastore，
          目標 client 刪不到）。"""
        keep_extra = max(0, int(snap.get("keep_copies", 1)) - 1)  # 扣掉掛載中那份
        try:
            with SessionLocal() as db:
                profile = db.get(Profile, profile_id)
                kept = (
                    db.query(CloneCopy)
                    .filter(CloneCopy.profile_id == profile_id, CloneCopy.status == "kept")
                    .order_by(CloneCopy.created_at.desc())
                    .all()
                )
                orphans = (
                    db.query(CloneCopy)
                    .filter(CloneCopy.profile_id == profile_id, CloneCopy.status == "created")
                    .filter(CloneCopy.path != (profile.last_clone_path or ""))
                    .all()
                )
                victims = [(c.id, c.path) for c in kept[keep_extra:]] + \
                          [(c.id, c.path) for c in orphans]
            for cid, path in victims:
                gone, note = await self._delete_copy_file(path, client, alt_client)
                if gone:
                    with SessionLocal() as db:
                        row = db.get(CloneCopy, cid)
                        if row is not None:
                            db.delete(row)
                            db.commit()
                await self._emit(run_id, {"type": "log", "message": note})
                self._sys_audit("retention", note)
            await self._orphan_file_sweep(run_id, profile_id, snap, client, alt_client)
        except Exception as exc:
            await self._emit(run_id, {"type": "log", "message": f"保留策略清理失敗：{exc}"})

    async def _delete_copy_file(self, path: str, client, alt_client=None) -> tuple[bool, str]:
        """刪副本 VMDK；跨 VC 時兩座都試。回傳 (檔案已不在, 訊息)——
        「檔案已不存在」視同刪除成功（可移除紀錄）；其他失敗保留紀錄下次重試。"""
        excs: list[Exception] = []
        for c in [client] + ([alt_client] if alt_client is not None else []):
            try:
                await asyncio.to_thread(c.delete_vmdk, path)
                return True, f"保留策略：已刪除舊副本 {path}"
            except Exception as exc:
                excs.append(exc)
        if any(_is_file_not_found(e) for e in excs):
            return True, f"保留策略：{path} 已不存在，移除紀錄"
        return False, f"保留策略：刪除 {path} 失敗（{excs[-1]}），保留紀錄下次重試"

    async def _orphan_file_sweep(self, run_id: int, profile_id: int, snap: dict,
                                 client, alt_client=None) -> None:
        """對帳 datastore 實際檔案：snapman-clones/ 內符合本任務前綴、
        但「任何紀錄都不認」的檔案 = 孤兒（早期刪檔失敗掉紀錄的殘留），刪除之。
        以全系統已知檔名保護（含其他任務的副本與掛載中檔案）。"""
        prefix = f"snapman-{snap['source_vm']}-data-"

        def _base(p: str) -> str:
            return p.rsplit("/", 1)[-1]

        with SessionLocal() as db:
            known = {_base(c.path) for c in db.query(CloneCopy).all()}
            known |= {_base(p.last_clone_path)
                      for p in db.query(Profile).all() if p.last_clone_path}
            mine = [c.path for c in db.query(CloneCopy)
                    .filter(CloneCopy.profile_id == profile_id).all()]
        # 掃描範圍：本任務副本所在過的 datastore ＋ 目的/暫存設定
        ds_names = {p.split("]")[0].lstrip("[").strip() for p in mine if p.startswith("[")}
        for key in ("target_datastore", "staging_datastore"):
            if snap.get(key):
                ds_names.add(snap[key])
        for ds in sorted(ds_names):
            for c in [client] + ([alt_client] if alt_client is not None else []):
                try:
                    files = await asyncio.to_thread(c.list_snapman_files, ds)
                except Exception:
                    continue  # 這座 vCenter 看不到此 datastore，換另一座
                for f in files:
                    name = f.get("name", "")
                    if not name.startswith("snapman-clones/"):
                        continue  # 歸檔資料夾由歸檔保留份數管理，不動
                    base = name.split("/", 1)[1]
                    # 前綴 + 純時間戳 + .vmdk 才是本任務的檔（startswith 會吃到
                    # 來源名互為前綴的別任務副本）；已知檔名與 -flat/-delta/-ctk 不動
                    if (re.fullmatch(re.escape(prefix) + r"\d+\.vmdk", base) is None
                            or base in known):
                        continue
                    path = f"[{ds}] {name}"
                    _gone, note = await self._delete_copy_file(path, c)
                    note = note.replace("保留策略：", "保留策略（孤兒檔）：", 1)
                    await self._emit(run_id, {"type": "log", "message": note})
                    self._sys_audit("retention", note)
                break  # 已由看得到的 client 處理完此 datastore

    def _sys_audit(self, action: str, detail: str) -> None:
        """保留策略等系統動作寫入稽核（先前只發 WebSocket 訊息，事後無跡可查）。"""
        try:
            with SessionLocal() as db:
                db.add(AuditLog(user="系統", action=action, detail=detail))
                db.commit()
        except Exception:
            pass

    async def _notify(self, run_id: int, snap: dict, status: str,
                      error: str | None, t0: float) -> None:
        """結果告警（失敗必發；成功依設定）。失敗不影響 run。"""
        if status == "success" and not settings.alert_on_success:
            return
        if not (settings.alert_webhook_url or settings.smtp_host):
            return
        try:
            results = await asyncio.to_thread(
                notify_run_result, snap.get("name", ""), run_id, status,
                error, int(time.monotonic() - t0),
            )
            for r in results:
                await self._emit(run_id, {"type": "log", "message": f"告警：{r}"})
        except Exception:
            pass

    async def _rollback(self, run_id: int, ctx: StepContext) -> None:
        """安全回滾：若還有殘留快照就移除；絕不動來源 DB 本體。
        整機同步另清暫存複本 VM（換手完成前失敗才清；殼名帶前綴，下一輪也會認領）。"""
        if ctx.snapshot_id:
            try:
                await asyncio.to_thread(ctx.client.remove_snapshot, ctx.source_vm, ctx.snapshot_id)
                await self._emit(run_id, {"type": "log", "message": f"回滾：已移除殘留快照 {ctx.snapshot_id}"})
            except Exception as exc:
                await self._emit(run_id, {"type": "log", "message": f"回滾移除快照失敗：{exc}"})
        temp = ctx.scratch.get("vs_temp")
        if temp and not ctx.scratch.get("vs_done"):
            for cl in {id(ctx.client): ctx.client, id(ctx.tgt_client): ctx.tgt_client}.values():
                try:
                    await asyncio.to_thread(cl.destroy_vm_if_exists, temp)
                except Exception:
                    pass
            await self._emit(run_id, {"type": "log",
                                      "message": f"回滾：已清除暫存複本 {temp}"})

    # --- DB 小工具 ---
    # 注意：以下皆為同步 SQLite 存取，直接在 event loop 上執行——SQLite 本機
    # 短查詢延遲可忽略，是刻意取捨；若改用網路型 DB（PostgreSQL 等）須改包
    # asyncio.to_thread，否則會卡住排程器與 WebSocket 廣播。
    def _set_run_status(self, run_id: int, status: str, error: str | None = None) -> None:
        with SessionLocal() as db:
            run = db.get(Run, run_id)
            run.status = status
            if status in ("success", "failed", "stopped"):
                run.finished_at = _now()
            if error:
                run.error = error
            db.commit()

    def _step_update(self, run_id: int, seq: int, status: str, message: str):
        async def _do():
            with SessionLocal() as db:
                step = (
                    db.query(StepLog).filter(StepLog.run_id == run_id, StepLog.seq == seq).one()
                )
                step.status = status
                step.message = message
                if status == "running" and step.started_at is None:
                    step.started_at = _now()
                if status in ("done", "failed", "skipped"):
                    step.finished_at = _now()
                db.commit()
        return _do()

    def _set_last_clone(self, profile_id: int, clone_path: str | None) -> None:
        with SessionLocal() as db:
            profile = db.get(Profile, profile_id)
            profile.last_clone_path = clone_path
            db.commit()

    def _copy_upsert(self, profile_id: int, path: str, status: str,
                     run_id: int | None = None) -> None:
        """依 path 更新副本狀態；沒有紀錄就補建（相容功能上線前的舊副本）。"""
        with SessionLocal() as db:
            row = (
                db.query(CloneCopy)
                .filter(CloneCopy.profile_id == profile_id, CloneCopy.path == path)
                .first()
            )
            if row is None:
                row = CloneCopy(profile_id=profile_id, path=path)
                db.add(row)
            row.status = status
            if run_id is not None:
                row.run_id = run_id
            db.commit()

    def _copy_rename(self, profile_id: int, old_path: str, new_path: str) -> None:
        """副本實體路徑被搬移（svMotion）後校正紀錄；新路徑已有紀錄則移除舊列。"""
        with SessionLocal() as db:
            old = (db.query(CloneCopy)
                   .filter(CloneCopy.profile_id == profile_id, CloneCopy.path == old_path)
                   .first())
            dup = (db.query(CloneCopy)
                   .filter(CloneCopy.profile_id == profile_id, CloneCopy.path == new_path)
                   .first())
            if old is not None and dup is None:
                old.path = new_path
            elif old is not None:
                db.delete(old)
            db.commit()

    def _copy_repath(self, profile_id: int, run_id: int, new_path: str) -> None:
        """xvc 搬遷後，把本輪 created 副本的路徑更新為目的端最終路徑。"""
        with SessionLocal() as db:
            row = (
                db.query(CloneCopy)
                .filter(CloneCopy.profile_id == profile_id,
                        CloneCopy.run_id == run_id,
                        CloneCopy.status == "created")
                .first()
            )
            if row is not None and row.path != new_path:
                row.path = new_path
                db.commit()

    def _copy_store_files(self, profile_id: int, path: str, db_files: dict) -> None:
        with SessionLocal() as db:
            row = (
                db.query(CloneCopy)
                .filter(CloneCopy.profile_id == profile_id, CloneCopy.path == path)
                .first()
            )
            if row is not None:
                row.files_json = json.dumps(db_files, ensure_ascii=False)
                db.commit()


def _profile_snapshot(profile: Profile) -> dict:
    # 待卸載清單不只信 last_clone_path：所有記錄為 mounted 的副本一併納入，
    # 指標與現實脫鉤（如手動清紀錄、先前失敗殘留）時可自動收斂回單一掛載
    prev = [c.path for c in profile.copies if c.status == "mounted"]
    if profile.last_clone_path and profile.last_clone_path not in prev:
        prev.append(profile.last_clone_path)
    def _vc_dict(v):
        return {"host": v.host, "user": v.user,
                "password": v.password, "insecure": v.insecure} if v else None

    # vmsync：目標端沒有既有 VM，互斥鎖 key（target_vm 欄位）改用複本最終名，
    # 防兩個任務同時寫同一個複本
    target_vm = profile.target_vm
    if profile.job_type == "vmsync":
        target_vm = profile.replica_name.strip() or f"{profile.source_vm}-replica"

    return {
        "profile_id": profile.id,
        "name": profile.name,
        "job_type": profile.job_type,
        "replica_name": profile.replica_name,
        "target_network": profile.target_network,
        "src_vc": _vc_dict(profile.vcenter),
        "tgt_vc": _vc_dict(profile.target_vcenter),
        "same_vc": profile.vcenter_id == profile.target_vcenter_id,
        "keep_copies": profile.keep_copies,
        "transfer_mode": profile.transfer_mode,
        "staging_datastore": profile.staging_datastore,
        "drill_query": profile.drill_query,
        "archive_enabled": profile.archive_enabled,
        "archive_datastore": profile.archive_datastore,
        "archive_keep": profile.archive_keep,
        "chain_next_id": profile.chain_next_id,
        "source_vm": profile.source_vm,
        "source_data_disk": profile.source_data_disk,
        "target_vm": target_vm,
        "target_datastore": profile.target_datastore,
        "target_drive_letter": profile.target_drive_letter,
        "database_list": profile.database_list,
        "prev_clone_paths": prev,
        "source_sql_instance": profile.source_sql_instance,
        "target_sql_instance": profile.target_sql_instance,
    }


run_manager = RunManager()
