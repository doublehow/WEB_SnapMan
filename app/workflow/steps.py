"""工作流步驟定義（①~⑩）。

每個步驟是一個 async 函式，接收 StepContext，回傳訊息字串（或拋出例外代表失敗）。
真正的阻塞操作透過 asyncio.to_thread 呼叫 vSphere 客戶端。

Guest 內操作注意事項：
- Guest Operations 不回傳 stdout，成敗一律以 exit code 判斷。
- sqlcmd 一律帶 -b：SQL 錯誤時以非零 exit code 結束（否則永遠回 0，錯誤會被吞掉）。
- 程式路徑使用絕對路徑（StartProgramInGuest 不保證解析 PATH）。
"""
from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field

from ..vsphere.base import VSphereClient, VSphereError

# guest 內程式絕對路徑
_PS = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_CMD = r"C:\Windows\System32\cmd.exe"

# guest 內操作逾時（秒）。預設 600 給輕量腳本；SQL 操作依性質放寬——
# 大型 DB 的 CHECKPOINT / ATTACH（含 crash recovery）可達數十分鐘、
# DBCC CHECKDB 可達小時級，硬限 600 秒會誤殺（run 失敗但 guest 內其實還在跑）
_T_SQL = 3600      # CHECKPOINT / DETACH / ATTACH / 演練自訂查詢
_T_DBCC = 21600    # DBCC CHECKDB（6 小時）


# SQL 執行個體名白名單：這個值會落在 cmd.exe 引號外與 PowerShell 裸字串中，
# 任何 & | ; ' $ 空白都等於在來源 production VM 執行任意指令；main.py 表單入口
# 已用同一規則擋下，這裡是第二道（既有 DB 紀錄 / 直接改 DB 亦不能繞過）。
_INSTANCE_RE = re.compile(r"[A-Za-z0-9_$]{1,16}")


def _check_instance(instance: str) -> None:
    if instance and not _INSTANCE_RE.fullmatch(instance):
        raise VSphereError(
            f"SQL 執行個體名「{instance}」含不允許的字元（僅限英數、底線、$，最長 16 字）")


def _server(instance: str) -> str:
    """執行個體名 → sqlcmd -S 參數值，一律指向 guest 本機（.\\INST）。
    不接受 HOST\\INST 形式：目標端的 sqlcmd 若能指向來源主機，⑥ DETACH /
    drill_query 就會打到 production——「絕不動來源」不變量不能靠設定正確。"""
    _check_instance(instance)
    return f".\\{instance}"


def _ps_q(s: str) -> str:
    """PowerShell 單引號字面值（'' 逃逸）。"""
    return "'" + s.replace("'", "''") + "'"


def _sqlcmd(query: str, instance: str = "", rows: bool = False) -> str:
    """組出 cmd.exe /c sqlcmd 參數；-b 讓 SQL 錯誤反映到 exit code。

    rows=True：查詢取資料列用（-h -1 無標題、-W 去尾端空白），配合 stdout 擷取解析。
    """
    s = f" -S {_server(instance)}" if instance else ""
    r = " -h -1 -W" if rows else ""
    return f'/c sqlcmd -b{s}{r} -Q "{query}"'


def _ps_encoded(script: str) -> str:
    """PowerShell 腳本 → -EncodedCommand 參數，避免多層引號逃逸問題。"""
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return f"-NoProfile -NonInteractive -EncodedCommand {b64}"


def _clean_output(text: str) -> str:
    """去除 PowerShell 重導 stderr 時的 CLIXML 序列化雜訊（進度/模組載入訊息）。
    CLIXML 標頭可能出現在輸出「開頭」（stderr 先寫入），不能從標頭處截斷，
    改為逐行過濾雜訊行、保留真正的 stdout。"""
    lines = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith("#< CLIXML"):
            continue
        if s.startswith("<Objs ") and "schemas.microsoft.com/powershell" in s:
            continue
        lines.append(ln)
    return "\n".join(lines).strip()


def _out(res) -> str:
    """guest 輸出尾段，附加到錯誤訊息供診斷。"""
    text = _clean_output(res.stdout or "")
    return f"；輸出：{text[-400:]}" if text else ""


@dataclass
class StepContext:
    # 來源 / 目標 vCenter 的 client（同一座時為同一個實例）
    client: VSphereClient            # 來源端（快照/clone/來源 guest）
    tgt_client: VSphereClient        # 目標端（掛卸碟/目標 guest）
    source_vm: str
    source_disk: str
    target_vm: str
    target_datastore: str
    target_drive: str
    databases: list[str]
    # 待卸載的掛載中副本（可能不只一顆：紀錄脫鉤/先前失敗殘留時自動收斂）
    prev_clone_paths: list[str]
    guest_user: str
    guest_pass: str
    source_sql_instance: str = ""
    target_sql_instance: str = ""
    # 進度回報 hook（manager 注入；於 worker thread 呼叫，接收 0~100）
    progress: object = None
    # 事件日誌 hook（manager 注入；於 worker thread 呼叫，接收訊息字串，
    # 發到 run 事件日誌與終端 log——OVF 串流每 3 分鐘的進度/速率報告用）
    log: object = None
    # 停止檢查 hook（manager 注入；回傳 True 表示使用者要求停止）
    should_cancel: object = None
    # 掛載歷史副本時使用：副本建立當下記錄的檔案清單 {db: [目標端路徑]}，免查來源
    db_files_override: dict | None = None
    # DR 演練：DBCC 通過後額外執行的自訂驗證 T-SQL（僅 verify 類 run 由 manager 注入）
    drill_query: str = ""
    # 跨 VC 傳輸模式：shared（共享 NFS）/ xvc（殼 VM + Cross-vCenter Relocate）
    transfer_mode: str = "shared"
    staging_datastore: str = ""              # xvc/vmsync：clone 先落的來源側 datastore
    tgt_vc_creds: dict | None = None         # xvc：目的端 vCenter 連線資訊（ServiceLocator 用）
    # vmsync（整機同步）：目標端複本名（留空 = {source_vm}-replica）與目標 portgroup
    replica_name: str = ""
    target_network: str = ""
    profile_id: int = 0                      # 複本所有權標記（vmsync ⑥ 覆蓋判定）
    # 執行中累積的狀態
    snapshot_id: str | None = None
    new_clone_path: str | None = None
    scratch: dict = field(default_factory=dict)


def _claimed(prefix: str, name: str, suffix: str = "") -> bool:
    """SnapMan 產物認領：前綴 + 純時間戳（+ 副檔名）才算本任務的。
    只用 startswith 時，來源 VM「APP」的前綴會吃到「APP-1」任務的資源。"""
    return re.fullmatch(re.escape(prefix) + r"\d+" + suffix, name) is not None


def _client_for(ctx: StepContext, vm: str) -> VSphereClient:
    """依 VM 屬於來源或目標，選用對應 vCenter 的 client。"""
    return ctx.client if vm == ctx.source_vm else ctx.tgt_client


async def _guest(ctx: StepContext, vm: str, program: str, args: str,
                 timeout_s: int = 600):
    return await asyncio.to_thread(
        _client_for(ctx, vm).guest_run, vm, program, args,
        ctx.guest_user, ctx.guest_pass, timeout_s,
    )


async def step_precheck(ctx: StepContext) -> str:
    """前置檢查。DB 清單留空 = 檔案磁碟分享模式：跳過所有 SQL 相關檢查/步驟，
    快照仍走 VSS 靜默（檔案系統一致），目標端只做掛碟 + 碟號指派。"""
    info = await asyncio.to_thread(ctx.client.get_vm, ctx.source_vm)
    if info.power_state != "poweredOn":
        raise VSphereError(f"來源 {ctx.source_vm} 非開機狀態（{info.power_state}）")
    if not info.tools_running:
        raise VSphereError(f"來源 {ctx.source_vm} 的 VMware Tools 未運作，無法靜默/Guest Ops")
    tgt = await asyncio.to_thread(ctx.tgt_client.get_vm, ctx.target_vm)
    if tgt.power_state != "poweredOn":
        raise VSphereError(f"目標 {ctx.target_vm} 非開機狀態（{tgt.power_state}）")
    if not tgt.tools_running:
        raise VSphereError(f"目標 {ctx.target_vm} 的 VMware Tools 未運作，無法執行 guest 內操作")
    # datastore 可見性檢查（依傳輸模式）：
    # - shared：目的 datastore 須「來源 VC」可見（④由來源 VC 執行寫入）；
    #           跨 VC 時目標 VC 也要看得到同名 datastore（⑦掛載用）
    # - xvc：暫存 datastore 須來源 VC 可見；目的 datastore 只需目標 VC 可見
    if ctx.transfer_mode == "xvc":
        # 暫存 datastore 留空 = 執行時自動使用「來源碟當下所在的 datastore」
        # （每輪動態掃描，來源 VM svMotion 後自動跟隨，且為同陣列最快路徑）
        if ctx.staging_datastore:
            src_ds = await asyncio.to_thread(ctx.client.list_datastores)
            if ctx.staging_datastore not in src_ds:
                raise VSphereError(
                    f"暫存 datastore「{ctx.staging_datastore}」在來源 vCenter 上不可見")
        if ctx.target_datastore:
            tgt_ds = await asyncio.to_thread(ctx.tgt_client.list_datastores)
            if ctx.target_datastore not in tgt_ds:
                raise VSphereError(
                    f"目的 datastore「{ctx.target_datastore}」在目標 vCenter 上不可見")
    elif ctx.target_datastore:
        src_ds = await asyncio.to_thread(ctx.client.list_datastores)
        if ctx.target_datastore not in src_ds:
            raise VSphereError(
                f"目的 datastore「{ctx.target_datastore}」在來源 vCenter 上不可見——"
                f"共享 NFS 須同名掛給兩座 vCenter 的主機（含來源側），"
                f"或改用「跨 VC 搬遷」傳輸模式"
            )
        if ctx.tgt_client is not ctx.client:
            tgt_ds = await asyncio.to_thread(ctx.tgt_client.list_datastores)
            if ctx.target_datastore not in tgt_ds:
                raise VSphereError(
                    f"目的 datastore「{ctx.target_datastore}」在目標 vCenter 上不可見——"
                    f"共享 NFS 須同名掛給兩座 vCenter 的主機（含目標側）"
                )
            # 同名 ≠ 同一儲存：volume URL 相同才是真共享（防兩地同名 NAS）
            src_url = await asyncio.to_thread(ctx.client.datastore_url,
                                              ctx.target_datastore)
            tgt_url = await asyncio.to_thread(ctx.tgt_client.datastore_url,
                                              ctx.target_datastore)
            if not src_url or src_url != tgt_url:
                raise VSphereError(
                    f"「{ctx.target_datastore}」在兩座 vCenter 上同名，但 volume URL"
                    f"不同＝實為不同儲存（如兩地各一台同名 NAS），clone 寫入來源側"
                    f"後目標側掛不到；請確認共享 NFS 設定或改用 xvc 傳輸模式"
                )
    if not ctx.databases:
        return "來源/目標皆開機、Tools 正常；檔案磁碟分享模式（無 DB，略過 SQL 相關步驟）"
    ok = await asyncio.to_thread(
        ctx.client.check_sql_writer, ctx.source_vm, ctx.guest_user, ctx.guest_pass
    )
    if not ok:
        raise VSphereError(f"{ctx.source_vm} 的 SQL Server VSS Writer 未就緒")
    return (
        f"來源/目標皆開機、Tools 正常、SQL VSS Writer 就緒；"
        f"DB：{', '.join(ctx.databases)}"
    )


async def step_checkpoint(ctx: StepContext) -> str:
    if not ctx.databases:
        return "檔案磁碟分享模式：略過 CHECKPOINT"
    for db in ctx.databases:
        dbq = db.replace("]", "]]")  # bracket 逃逸（與 detach/verify 一致）
        res = await _guest(
            ctx, ctx.source_vm, _CMD,
            _sqlcmd(f"USE [{dbq}]; CHECKPOINT;", ctx.source_sql_instance),
            timeout_s=_T_SQL,
        )
        if not res.ok:
            raise VSphereError(f"{db} CHECKPOINT 失敗（exit={res.exit_code}）{_out(res)}")
    return f"已對 {len(ctx.databases)} 個 DB 下 CHECKPOINT"


async def step_snapshot(ctx: StepContext) -> str:
    snap = await asyncio.to_thread(
        ctx.client.create_quiesced_snapshot, ctx.source_vm, "snapman-quiesced"
    )
    ctx.snapshot_id = snap
    return f"靜默快照建立完成（{snap}）— 應用一致點（VMware Tools → SQL VSS Writer）"


async def step_clone(ctx: StepContext) -> str:
    import time
    # snapman- 前綴：檔名本身即可識別為 SnapMan 產物（不只靠目錄）
    name = f"snapman-{ctx.source_vm}-data-{int(time.time())}"
    # xvc 模式：clone 先落來源側暫存 datastore（留空 = clone_data_disk 會
    # 自動退回「來源碟當下所在的 datastore」，svMotion 後自動跟隨）
    dest_ds = ctx.staging_datastore if ctx.transfer_mode == "xvc" else ctx.target_datastore
    path = await asyncio.to_thread(
        ctx.client.clone_data_disk,
        ctx.source_vm,
        ctx.source_disk,
        dest_ds,
        name,
        ctx.snapshot_id,  # 以快照當下的 backing 為 copy 來源（現行 delta 鎖定中）
        ctx.progress,     # CopyVirtualDisk 進度 → 前端進度條
        ctx.should_cancel,  # 停止請求 → 取消 vSphere 複製工作（免等 15 分鐘）
    )
    ctx.new_clone_path = path
    note = "（暫存，待跨 VC 搬遷）" if ctx.transfer_mode == "xvc" else ""
    return f"已從一致點 clone 出獨立資料碟：{path}{note}"


async def step_remove_snapshot(ctx: StepContext) -> str:
    if not ctx.snapshot_id:
        return "無快照可移除"
    await asyncio.to_thread(ctx.client.remove_snapshot, ctx.source_vm, ctx.snapshot_id)
    ctx.snapshot_id = None
    return "已立即移除來源快照（縮短 production stun 風險）"


async def step_xvc_transfer(ctx: StepContext) -> str:
    """跨 VC 搬遷：來源側建殼 VM 掛上暫存 clone → Cross-vCenter Relocate 到目的端
    → 目的端拆碟移入 snapman-clones/、銷毀殼 VM。免共享儲存。"""
    if ctx.transfer_mode != "xvc":
        return "共享 datastore 模式：略過跨 VC 搬遷"
    if not ctx.new_clone_path:
        raise VSphereError("無暫存 clone 可搬遷")
    import time
    # 殼 VM 名稱帶來源 VM 識別：上一輪 unwrap 失敗的殘留（如 session 逾時）
    # 才能在下一輪安全認領清除（只認本任務前綴，不動其他任務的殼 VM）
    shell_prefix = f"snapman-xfer-{ctx.source_vm}-"
    cleaned: list[str] = []
    for cl in {id(ctx.client): ctx.client, id(ctx.tgt_client): ctx.tgt_client}.values():
        try:
            stale = [v.name for v in await asyncio.to_thread(cl.list_vms)
                     if _claimed(shell_prefix, v.name)]
            for name in stale:
                await asyncio.to_thread(cl.destroy_vm_if_exists, name)
                cleaned.append(name)
        except Exception:
            pass  # 清殘留失敗不擋主流程，殼 VM 留待下一輪再清
    ctx.scratch["xfer_cleaned"] = cleaned
    shell = f"{shell_prefix}{int(time.time())}"
    base = ctx.new_clone_path.rsplit("/", 1)[-1]
    if base.endswith(".vmdk"):
        base = base[:-5]
    # 1) 先取目的端資訊（落點 = 目標 VM 所在主機；含目的 vCenter 版本），
    #    再挑來源側承載殼 VM 的主機：看得到暫存碟、版本「不高於目的 vCenter」
    #    且其中最高者（XVM 由殼 VM 所在主機發起，兩個方向的版本不相容都會被拒）
    src_ver, src_build = await asyncio.to_thread(ctx.client.vcenter_release)
    dest_info = await asyncio.to_thread(
        ctx.tgt_client.get_vm_moids, ctx.target_vm, ctx.target_datastore, src_ver, src_build)
    staging = ctx.staging_datastore or ctx.new_clone_path.split("]")[0].lstrip("[").strip()
    xvc_host = await asyncio.to_thread(
        ctx.client.pick_xvc_host, staging,
        dest_info.get("vc_version", ""), dest_info.get("vc_build", ""))
    await asyncio.to_thread(
        ctx.client.create_shell_vm, shell,
        staging, ctx.new_clone_path, xvc_host,
    )
    try:
        # 2) Relocate 到目的端
        await asyncio.to_thread(
            ctx.client.xvc_relocate_vm, shell, dest_info, ctx.tgt_vc_creds, ctx.progress)
    except Exception:
        # 回滾：銷毀殼 VM（連同暫存碟一併刪除，不留孤兒）
        try:
            await asyncio.to_thread(ctx.client.destroy_vm_if_exists, shell)
        except Exception:
            pass
        raise
    # 3) 目的端：拆碟 → snapman-clones/ → 銷毀殼 VM
    final = await asyncio.to_thread(
        ctx.tgt_client.shell_unwrap, shell, ctx.target_datastore, base)
    ctx.new_clone_path = final
    note = f"；已清除上一輪殘留殼 VM：{', '.join(cleaned)}" if cleaned else ""
    return f"已跨 VC 搬遷到目的端（經 {xvc_host}）：{final}{note}"


async def step_detach_old(ctx: StepContext) -> str:
    # 與現實對帳：以目標 VM「實際掛載」的本任務 snapman 碟為準。
    # svMotion 可能把掛載中的 clone 連同 VM 搬走（路徑改變、落入 VM 資料夾），
    # 只信紀錄會拔不到又留孤兒；改以檔名前綴 snapman-{來源VM}-data- 盤點實際碟，
    # 僅認本任務的前綴，絕不動同目標 VM 上其他任務的碟。
    def _base(p: str) -> str:
        return p.rsplit("/", 1)[-1]

    prefix = f"snapman-{ctx.source_vm}-data-"
    disks = await asyncio.to_thread(ctx.tgt_client.list_disks, ctx.target_vm)
    actual_by_base = {
        _base(d.file_name): d.file_name
        for d in disks if _claimed(prefix, _base(d.file_name), r"\.vmdk")
    }
    if not ctx.prev_clone_paths and not actual_by_base:
        return "無上一輪 clone，略過卸載"
    # 對帳：紀錄路徑 → 實際路徑（basename 對應）；紀錄外的實際碟一併納入卸載
    detach_list: list[str] = []
    remap: dict[str, str] = {}
    seen: set[str] = set()
    for rp in ctx.prev_clone_paths:
        ap = actual_by_base.get(_base(rp))
        if ap is None:
            detach_list.append(rp)          # 已不在 VM 上，走容錯
        else:
            if ap != rp:
                remap[rp] = ap              # 路徑被搬移過（svMotion）
            detach_list.append(ap)
            seen.add(_base(rp))
    extras = [ap for b, ap in actual_by_base.items() if b not in seen]
    detach_list.extend(extras)
    ctx.scratch["detach_remap"] = remap
    ctx.scratch["detach_extras"] = extras
    # 1) 對目標 SQL DETACH 舊 DB —— 失敗即中止，絕不在 DB 掛載中的情況下拔碟
    for db in ctx.databases:
        # DB 不存在（例如上一輪在 ATTACH 前失敗）視為已卸載，不擋流程。
        # DETACH 前先 SINGLE_USER 強制斷開殘留連線（忘了關的 SSMS、offload 程式），
        # 並與 DETACH 同批次執行——我方連線佔住唯一名額，不給其他連線搶進的縫隙。
        dbq = db.replace("]", "]]")
        dbs = db.replace("'", "''")
        res = await _guest(
            ctx, ctx.target_vm, _CMD,
            _sqlcmd(
                f"IF DB_ID(N'{dbs}') IS NOT NULL BEGIN "
                f"ALTER DATABASE [{dbq}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"EXEC sp_detach_db N'{dbs}'; END",
                ctx.target_sql_instance,
            ),
            timeout_s=_T_SQL,
        )
        if not res.ok:
            raise VSphereError(
                f"{db} DETACH 失敗（exit={res.exit_code}）；"
                f"為避免資料損毀已中止，請至目標 VM 確認 DB 狀態後再重試{_out(res)}"
            )
    # 2) 依碟號將上一輪資料碟 offline（避免 Windows 對熱移除的碟殘留 handle）。
    #    碟號找不到（Profile 碟號在兩輪之間被改過、或已被手動卸載）→ exit 100，
    #    略過 offline 直接拔碟：DB 已 DETACH（或不存在），surprise removal 無害。
    offline_script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"$part = Get-Partition -DriveLetter '{ctx.target_drive}' -ErrorAction SilentlyContinue\n"
        "if (-not $part) { exit 100 }\n"
        "try { $part | Get-Disk | Set-Disk -IsOffline $true } catch { exit 2 }\n"
        "exit 0\n"
    )
    res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(offline_script))
    skipped_offline = res.exit_code == 100
    if not res.ok and not skipped_offline:
        raise VSphereError(
            f"目標磁碟（{ctx.target_drive}:）offline 失敗（exit={res.exit_code}），"
            f"中止拔碟{_out(res)}"
        )
    # 3) 從 VM 逐一移除掛載中的舊碟——「不」刪除 backing 檔案：副本留在
    #    datastore 上，由保留策略（keep_copies）統一決定去留。
    #    碟已不在 VM 上（過期指標）→ 視為已卸載，不擋流程
    removed: list[str] = []
    missing: list[str] = []
    for path in detach_list:
        try:
            await asyncio.to_thread(ctx.tgt_client.detach_disk, ctx.target_vm, path, False)
            removed.append(path)
        except VSphereError as exc:
            if "找不到資料碟" not in str(exc):
                raise
            missing.append(path)
    parts = []
    if removed:
        parts.append(f"已卸載（檔案保留）：{'; '.join(removed)}")
    if missing:
        parts.append(f"已不在 VM 上：{'; '.join(missing)}")
    if remap:
        parts.append(
            "路徑曾被搬移（svMotion），已依實際路徑處理："
            + "; ".join(f"{k} → {v}" for k, v in remap.items())
        )
    if extras:
        parts.append(f"發現紀錄外的本任務碟，一併卸載：{'; '.join(extras)}")
    if skipped_offline:
        parts.append(f"（碟號 {ctx.target_drive}: 不存在，略過 offline）")
    return "；".join(parts)


async def step_attach_new(ctx: StepContext) -> str:
    if not ctx.new_clone_path:
        raise VSphereError("無 clone 可掛載")
    unit = await asyncio.to_thread(
        ctx.tgt_client.attach_disk, ctx.target_vm, ctx.new_clone_path)
    ctx.scratch["attach_unit"] = unit  # guest 據 SCSI unit 精確定位剛掛的碟
    return f"已熱掛新資料碟到 {ctx.target_vm}：{ctx.new_clone_path}"


# 目標磁碟 online + 資料分割區識別 + 碟號指派（在目標 guest 內執行）。
# 實測：online（含簽章重寫）後 Windows mountmgr 會「自動」依序配發碟號給
# clone 碟的所有分割區——例如 E: 給系統保留（548MB）、F: 給真正的資料分割區，
# 所以「碟號存在」推不出「指對分割區」。分割區的識別也不用大小猜（啟發式會錯），
# 而是拿來源 sys.master_files 的 DB 檔案「相對路徑」逐一探測各分割區內容：
# 誰的檔案系統裡真的有這個路徑，誰就是資料分割區（演繹，非猜測）。
# - 探測經 volume GUID 路徑（\\?\Volume{..}\），分割區沒碟號也能驗
# - 檔案本身可能因 ACL 探不到（SQL DATA 目錄只授權服務 SID），
#   依序退探其目錄、上層目錄
# - Profile 碟號被 clone 碟其他分割區占走 → 釋放重指派；被其他磁碟占用 → exit 7
_ONLINE_SCRIPT = """\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
$drive = '@DRIVE@'
$probes = @(@PROBES@)
$offline = @(Get-Disk | Where-Object IsOffline)
try {
  $offline | Set-Disk -IsOffline $false
  Get-Disk | Where-Object IsReadOnly | Set-Disk -IsReadOnly $false
} catch { exit 2 }
Start-Sleep -Seconds 3
$ErrorActionPreference = 'SilentlyContinue'
$diskNos = @()
if ($offline) { $diskNos = @($offline | ForEach-Object { $_.Number }) }
if (-not $diskNos) {
  $diskNos = @(Get-Disk | Where-Object { -not $_.IsBoot } | ForEach-Object { $_.Number })
}
if (-not $diskNos) { exit 3 }
$found = $null
foreach ($p in @(Get-Partition -DiskNumber $diskNos)) {
  $vol = $p | Get-Volume
  if (-not $vol -or -not $vol.Path) { continue }
  foreach ($rp in $probes) {
    $full = $vol.Path + $rp.TrimStart('\\')
    $dir = Split-Path $full
    if ([System.IO.File]::Exists($full) -or
        [System.IO.Directory]::Exists($dir) -or
        [System.IO.Directory]::Exists((Split-Path $dir))) { $found = $p; break }
  }
  if ($found) { break }
}
if (-not $found) { exit 5 }
# 清掉 clone 碟上「其他」分割區被自動配發的碟號（如系統保留搶到 E:）——
# 只動 $found 所在那顆碟，不碰其他磁碟（含別任務的 clone 碟）
foreach ($p in @(Get-Partition -DiskNumber $found.DiskNumber -ErrorAction SilentlyContinue)) {
  if ($p.DriveLetter -and $p.PartitionNumber -ne $found.PartitionNumber) {
    Remove-PartitionAccessPath -DiskNumber $p.DiskNumber `
      -PartitionNumber $p.PartitionNumber -AccessPath ("$($p.DriveLetter)" + ':\\') `
      -ErrorAction SilentlyContinue
  }
}
if ("$($found.DriveLetter)" -eq $drive) { exit 0 }
$holder = Get-Partition -DriveLetter $drive -ErrorAction SilentlyContinue
if ($holder) {
  if ($diskNos -notcontains $holder.DiskNumber) { exit 7 }
  Remove-PartitionAccessPath -DiskNumber $holder.DiskNumber `
    -PartitionNumber $holder.PartitionNumber -AccessPath ($drive + ':\\')
}
$ErrorActionPreference = 'Stop'
try { $found | Set-Partition -NewDriveLetter $drive } catch { exit 3 }
exit 0
"""

_ONLINE_ERRORS = {
    2: "磁碟 online / 解除唯讀失敗",
    3: "找不到候選磁碟或碟號指派失敗",
    5: "clone 碟各分割區皆不含來源 DB 路徑對應的內容（clone 對象磁碟選錯？）",
    7: "指定碟號已被其他磁碟（開機碟等）占用，請先釋放該碟號",
}

# 檔案磁碟分享模式的 online + 碟號指派：以⑦掛載回傳的 SCSI unit number
# 精確鎖定「剛熱掛的那顆碟」（不靠 offline 偵測——跨 VC 的檔案碟無簽章衝突，
# 掛上即被 Windows 自動 online；也不猜其他碟，絕不動同目標 VM 上別任務的碟）。
_ONLINE_SCRIPT_FILE = """\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$drive = '@DRIVE@'
$unit = @UNIT@
Get-Disk | Where-Object IsOffline | Set-Disk -IsOffline $false
Get-Disk | Where-Object IsReadOnly | Set-Disk -IsReadOnly $false
Start-Sleep -Seconds 3
# vSphere unit number 對應 Windows 的「SCSITargetId」（LUN 恆為 0）——
# 實測（目標 VM）：unit 0/1/2 的碟 target=0/1/2、lun 全為 0，
# 舊版比對 SCSILogicalUnit 永遠落空（exit 3 的根因）。
# 剛熱掛的碟可能仍在列舉中，最多重試 5 次
$dd = $null
for ($i = 0; $i -lt 5 -and -not $dd; $i++) {
  $dd = Get-CimInstance Win32_DiskDrive |
    Where-Object { $_.SCSITargetId -eq $unit -and $_.SCSILogicalUnit -eq 0 -and
                   $_.InterfaceType -ne 'USB' }
  if (-not $dd) { Start-Sleep -Seconds 2 }
}
if (-not $dd) {
  Get-CimInstance Win32_DiskDrive | Sort-Object Index | ForEach-Object {
    'disk idx=' + $_.Index + ' target=' + $_.SCSITargetId + ' lun=' + $_.SCSILogicalUnit +
    ' if=' + $_.InterfaceType }
  exit 3
}
$diskNos = @($dd | ForEach-Object { $_.Index })
$found = @(Get-Partition -DiskNumber $diskNos -ErrorAction SilentlyContinue) |
  Where-Object { -not $_.IsSystem -and -not $_.IsHidden } |
  Sort-Object Size -Descending | Select-Object -First 1
if (-not $found) { exit 3 }
if ("$($found.DriveLetter)" -eq $drive) { exit 0 }
$holder = Get-Partition -DriveLetter $drive -ErrorAction SilentlyContinue
if ($holder) {
  if ($diskNos -notcontains $holder.DiskNumber) { exit 7 }
  Remove-PartitionAccessPath -DiskNumber $holder.DiskNumber `
    -PartitionNumber $holder.PartitionNumber -AccessPath ($drive + ':\\')
}
# 清掉這顆碟上其他分割區被自動配發、卻占用非目標碟號的情況（如系統保留搶到碟號）
foreach ($p in @(Get-Partition -DiskNumber $diskNos -ErrorAction SilentlyContinue)) {
  if ($p.DriveLetter -and "$($p.DriveLetter)" -ne $drive -and
      $p.PartitionNumber -ne $found.PartitionNumber) {
    Remove-PartitionAccessPath -DiskNumber $p.DiskNumber `
      -PartitionNumber $p.PartitionNumber -AccessPath ("$($p.DriveLetter)" + ':\\') -ErrorAction SilentlyContinue
  }
}
try { $found | Set-Partition -NewDriveLetter $drive } catch { exit 3 }
exit 0
"""

# 單一 DB 的 ATTACH（在目標 guest 內執行）。
# 檔案清單由來源 sys.master_files 查得（權威路徑，免搜尋、免檔名約定），
# 換成目標碟號後傳入。SQL 的 DATA 目錄預設 ACL 只授權來源的服務 SID，
# 目標端先 takeown + icacls 補權限（clone 是可拋棄副本，放寬無妨），再 ATTACH。
_ATTACH_SCRIPT = """\
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'SilentlyContinue'
$dirs = @(@DIRS@)
foreach ($d in $dirs) {
  takeown /F $d 2>$null | Out-Null
  icacls $d /grant '*S-1-5-32-544:F' 2>$null | Out-Null
  icacls $d /grant '@SVC@:F' 2>$null | Out-Null
}
$files = @(@FILES@)
foreach ($f in $files) {
  takeown /F $f 2>$null | Out-Null
  icacls $f /grant '*S-1-5-32-544:F' 2>$null | Out-Null
  icacls $f /grant '@SVC@:F' 2>$null | Out-Null
  if (-not (Test-Path -LiteralPath $f)) { exit 4 }
}
if (-not (Get-Command sqlcmd -ErrorAction SilentlyContinue)) { exit 8 }
$on = ($files | ForEach-Object { "(FILENAME=N'" + ($_ -replace "'", "''") + "')" }) -join ','
& sqlcmd -b @SARG@ -Q ('CREATE DATABASE [@DBQ@] ON ' + $on + ' FOR ATTACH')
if ($LASTEXITCODE -ne 0) { exit 6 }
exit 0
"""

_ATTACH_ERRORS = {
    4: "來源路徑對應的檔案不存在於掛載碟（碟號指派或 clone 內容有誤）",
    6: "sqlcmd ATTACH 失敗（DB 已存在？SQL 版本低於來源？服務帳號無檔案權限？）",
    8: "目標 VM 找不到 sqlcmd——目標須安裝 SQL Server（版本 ≥ 來源）與其工具",
}


async def _query_db_files(ctx: StepContext, db: str) -> list[str]:
    """向來源 SQL 查該 DB 的實體檔案路徑（sys.master_files，權威資料）。"""
    q = (
        "SET NOCOUNT ON; SELECT physical_name FROM sys.master_files "
        f"WHERE database_id = DB_ID(N'{db.replace(chr(39), chr(39) * 2)}')"
    )
    res = await _guest(
        ctx, ctx.source_vm, _CMD, _sqlcmd(q, ctx.source_sql_instance, rows=True)
    )
    if not res.ok:
        raise VSphereError(
            f"{db} 向來源查詢實體檔案路徑失敗（exit={res.exit_code}）{_out(res)}"
        )
    paths = [
        ln.strip() for ln in _clean_output(res.stdout).splitlines()
        if re.match(r"^[A-Za-z]:\\", ln.strip())
    ]
    if not paths:
        raise VSphereError(f"{db} 在來源查無實體檔案（sys.master_files）{_out(res)}")
    return paths


async def step_online_and_attach_db(ctx: StepContext) -> str:
    # 檔案磁碟分享模式：只做 online + 碟號指派（依⑦的 SCSI unit 定位剛掛的碟），無 ATTACH
    if not ctx.databases:
        unit = ctx.scratch.get("attach_unit")
        if unit is None:
            raise VSphereError("缺少掛載磁碟的 SCSI unit（⑦未提供）")
        script = (_ONLINE_SCRIPT_FILE
                  .replace("@DRIVE@", ctx.target_drive)
                  .replace("@UNIT@", str(unit)))
        res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(script))
        if not res.ok:
            reason = _ONLINE_ERRORS.get(res.exit_code, "未知錯誤")
            raise VSphereError(
                f"目標磁碟上線/碟號指派失敗（exit={res.exit_code}：{reason}）{_out(res)}"
            )
        return f"檔案模式：磁碟上線並指派碟號 {ctx.target_drive}:（無 SQL ATTACH）"

    # 1) 取得各 DB 的檔案清單（目標端路徑）：
    #    - 掛載歷史副本：用副本建立當下記錄的清單（碟號依現行設定重對映）
    #    - 每日工作流：向來源查 sys.master_files 權威路徑，
    #      並確認全部落在同一顆來源磁碟（Profile 只 clone 一顆碟）
    mapped_files: dict[str, list[str]] = {}
    if ctx.db_files_override:
        for db, paths in ctx.db_files_override.items():
            mapped_files[db] = [f"{ctx.target_drive}{p[1:]}" for p in paths]
    else:
        for db in ctx.databases:
            paths = await _query_db_files(ctx, db)
            drives = {p[0].upper() for p in paths}
            if len(drives) > 1:
                raise VSphereError(
                    f"{db} 檔案分散在多顆來源磁碟（{', '.join(sorted(drives))}:），"
                    f"無法以單一 clone 搬運"
                )
            mapped_files[db] = [f"{ctx.target_drive}{p[1:]}" for p in paths]
    all_drives = {p[0].upper() for paths in mapped_files.values() for p in paths}
    if len(all_drives) > 1:
        raise VSphereError(
            f"DB 檔案分散在多顆來源磁碟（{', '.join(sorted(all_drives))}:），"
            f"無法以單一 clone 搬運"
        )
    ctx.scratch["db_files"] = mapped_files  # manager 記到 CloneCopy，供日後掛載歷史副本
    # 2) 磁碟 online + 依路徑「內容」識別資料分割區 + 指派碟號
    #    探測樣本：各 DB 第一個檔案（mdf）的相對路徑（去碟號）
    probes = [paths[0][2:] for paths in mapped_files.values()]
    probes_ps = ",".join("'" + p.replace("'", "''") + "'" for p in probes)
    script = (
        _ONLINE_SCRIPT
        .replace("@DRIVE@", ctx.target_drive)
        .replace("@PROBES@", probes_ps)
    )
    res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(script))
    if not res.ok:
        reason = _ONLINE_ERRORS.get(res.exit_code, "未知錯誤")
        raise VSphereError(
            f"目標磁碟上線/碟號指派失敗（exit={res.exit_code}：{reason}）{_out(res)}"
        )
    # 3) 逐一 DB：目標補權限 + ATTACH
    for db, mapped in mapped_files.items():
        _check_instance(ctx.target_sql_instance)
        svc = (
            f"NT SERVICE\\MSSQL${ctx.target_sql_instance}"
            if ctx.target_sql_instance else "NT SERVICE\\MSSQLSERVER"
        )
        sarg = (f"-S {_ps_q(_server(ctx.target_sql_instance))}"
                if ctx.target_sql_instance else "")
        files_ps = ",".join("'" + m.replace("'", "''") + "'" for m in mapped)
        # 父目錄也要補權限：DBCC 線上檢查需在資料檔旁建內部快照檔（目錄層級的建檔權）
        dirs = sorted({m.rsplit("\\", 1)[0] for m in mapped})
        dirs_ps = ",".join("'" + d.replace("'", "''") + "'" for d in dirs)
        script = (
            _ATTACH_SCRIPT
            .replace("@DIRS@", dirs_ps)
            .replace("@FILES@", files_ps)
            .replace("@SVC@", svc.replace("'", "''"))
            .replace("@DBQ@", db.replace("]", "]]"))      # T-SQL bracket 逃逸
            .replace("@SARG@", sarg)
        )
        res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(script),
                           timeout_s=_T_SQL)
        if not res.ok:
            reason = _ATTACH_ERRORS.get(res.exit_code, "未知錯誤")
            raise VSphereError(
                f"{db} ATTACH 失敗（exit={res.exit_code}：{reason}）；"
                f"預期檔案：{'; '.join(mapped)}{_out(res)}"
            )
    return (
        f"目標磁碟上線（碟號 {ctx.target_drive}: 依來源 DB 路徑識別資料分割區）"
        f"並 ATTACH {len(ctx.databases)} 個 DB"
    )


async def step_verify(ctx: StepContext) -> str:
    # 檔案磁碟分享模式：驗證掛載碟可存取即可（無 DBCC / 自訂 SQL）
    if not ctx.databases:
        script = (
            "$ProgressPreference = 'SilentlyContinue'\n"
            f"if (Test-Path '{ctx.target_drive}:\\') {{ exit 0 }} else {{ exit 1 }}\n"
        )
        res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(script))
        if not res.ok:
            raise VSphereError(
                f"掛載碟 {ctx.target_drive}: 無法存取（exit={res.exit_code}）{_out(res)}"
            )
        return f"檔案模式：掛載碟 {ctx.target_drive}: 可存取"
    for db in ctx.databases:
        dbq = db.replace("]", "]]")
        # TABLOCK 需要獨占鎖，⑧到⑨之間可能有使用者重連搶占（實測 Run #7 的 SSMS
        # 重連導致 5030）→ 先 SINGLE_USER 踢人再驗，驗完恢復 MULTI_USER，同批次執行
        res = await _guest(
            ctx, ctx.target_vm, _CMD,
            _sqlcmd(
                f"ALTER DATABASE [{dbq}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
                f"DBCC CHECKDB([{dbq}]) WITH NO_INFOMSGS, PHYSICAL_ONLY, TABLOCK; "
                f"ALTER DATABASE [{dbq}] SET MULTI_USER;",
                ctx.target_sql_instance,
            ),
            timeout_s=_T_DBCC,
        )
        if not res.ok:
            raise VSphereError(
                f"{db} 驗證失敗（DBCC exit={res.exit_code}）"
                f"；注意：DB 可能停留在 SINGLE_USER 模式{_out(res)}"
            )
    # DR 演練：自訂驗證查詢（sqlcmd -b，非零 exit = 失敗；經 PS 編碼避免引號問題）
    if ctx.drill_query.strip():
        sarg = (f"-S {_ps_q(_server(ctx.target_sql_instance))}"
                if ctx.target_sql_instance else "")
        q = ctx.drill_query.strip().replace("'", "''")
        script = (
            "$ProgressPreference = 'SilentlyContinue'\n"
            f"& sqlcmd -b {sarg} -Q '{q}'\n"
            "if ($LASTEXITCODE -ne 0) { exit 6 }\nexit 0\n"
        )
        res = await _guest(ctx, ctx.target_vm, _PS, _ps_encoded(script),
                           timeout_s=_T_SQL)
        if not res.ok:
            # 不附帶 stdout：自訂 SQL 的查詢結果可能是業務資料，StepLog / WS 對唯讀角色可見
            raise VSphereError(f"演練自訂驗證查詢失敗（exit={res.exit_code}；輸出不入紀錄，請於目標 SQL 手動執行檢視）")
        return "目標 DB 驗證通過（DBCC + 自訂驗證查詢），已恢復 MULTI_USER"
    return "目標 DB 驗證通過（DBCC CHECKDB），已恢復 MULTI_USER"


async def step_finalize(ctx: StepContext) -> str:
    return f"完成。目標 {ctx.target_vm} 已掛上當日一致副本；本輪 clone：{ctx.new_clone_path}"


# ---------- 整機同步（vmsync）----------
def _replica_final(ctx: StepContext) -> str:
    """目標端複本的最終名稱。"""
    return ctx.replica_name.strip() or f"{ctx.source_vm}-replica"


async def _vm_exists(client, name: str) -> bool:
    try:
        await asyncio.to_thread(client.get_vm, name)
        return True
    except VSphereError:
        return False


async def step_vs_precheck(ctx: StepContext) -> str:
    """整機同步前置檢查：來源可拍快照、目的 datastore/網路存在、複本名不衝突。"""
    info = await asyncio.to_thread(ctx.client.get_vm, ctx.source_vm)
    # 範本：vSphere 不允許對範本拍快照（NotSupported），且範本恆關機、本身即靜態
    # 一致點 → ②略過快照、③直接 clone、⑥定名後再把複本標記為範本（範本對範本）
    ctx.scratch["vs_src_template"] = bool(info.is_template)
    if not info.is_template and info.power_state == "poweredOn" and not info.tools_running:
        raise VSphereError(
            f"來源 {ctx.source_vm} 開機中但 VMware Tools 未運作，無法 VSS 靜默；"
            f"（關機中的 VM 可直接同步，不需 Tools）"
        )
    same_vc = ctx.tgt_client is ctx.client
    final = _replica_final(ctx)
    if same_vc and final == ctx.source_vm:
        raise VSphereError("複本名稱與來源 VM 相同且在同一座 vCenter，請改複本名稱")
    if not ctx.target_datastore:
        raise VSphereError("整機同步須指定目的 datastore")
    tgt_ds = await asyncio.to_thread(ctx.tgt_client.list_datastores)
    if ctx.target_datastore not in tgt_ds:
        raise VSphereError(
            f"目的 datastore「{ctx.target_datastore}」在目標 vCenter 上不可見")
    if ctx.staging_datastore and not same_vc:
        src_ds = await asyncio.to_thread(ctx.client.list_datastores)
        if ctx.staging_datastore not in src_ds:
            raise VSphereError(
                f"暫存 datastore「{ctx.staging_datastore}」在來源 vCenter 上不可見")
    if not same_vc and ctx.transfer_mode == "ovf":
        # OVF 串流無法匯出 vTPM/加密 VM，先擋在前置檢查（訊息比匯出失敗清楚）
        if await asyncio.to_thread(ctx.client.vm_has_vtpm, ctx.source_vm):
            raise VSphereError(
                f"來源 {ctx.source_vm} 帶 vTPM，OVF 串流無法匯出；"
                f"請改用 XVM 傳輸（需兩端主機與 vCenter 版本 / build 相容）")
    if not ctx.target_network:
        raise VSphereError("整機同步須指定目標網路（portgroup）")
    nets = await asyncio.to_thread(ctx.tgt_client.list_networks)
    if ctx.target_network not in nets:
        raise VSphereError(
            f"目標網路（portgroup）「{ctx.target_network}」在目標 vCenter 上不存在")
    if info.is_template:
        state = "為範本（靜態，略過快照；複本亦將標記為範本）"
    else:
        state = "開機中（VSS 靜默）" if info.power_state == "poweredOn" else "關機中"
    return (f"來源 {ctx.source_vm} {state}；目的 datastore/網路就緒；"
            f"複本將定名 {final}")


async def step_vs_snapshot(ctx: StepContext) -> str:
    """vmsync 快照：範本不可拍快照（本身即靜態一致點），略過；其餘沿用 disk 的靜默快照。"""
    if ctx.scratch.get("vs_src_template"):
        return "來源為範本，本身即靜態一致點，略過快照"
    return await step_snapshot(ctx)


async def step_vs_clone_vm(ctx: StepContext) -> str:
    """從快照一致點 clone 出完整暫存 VM（來源側；同 VC 時直接落目的 datastore）。"""
    import time
    # 上一輪失敗殘留的暫存複本：只認本任務前綴，兩側都清（同 xvc 殼 VM 慣例）
    prefix = f"snapman-vsync-{ctx.source_vm}-"
    cleaned: list[str] = []
    for cl in {id(ctx.client): ctx.client, id(ctx.tgt_client): ctx.tgt_client}.values():
        try:
            stale = [v.name for v in await asyncio.to_thread(cl.list_vms)
                     if _claimed(prefix, v.name)]
            for name in stale:
                await asyncio.to_thread(cl.destroy_vm_if_exists, name)
                cleaned.append(name)
        except Exception:
            pass  # 清殘留失敗不擋主流程，留待下一輪
    same_vc = ctx.tgt_client is ctx.client
    max_ver = max_build = ""
    if same_vc:
        dest_ds = ctx.target_datastore
    else:
        dest_ds = ctx.staging_datastore
        if not dest_ds:
            disks = await asyncio.to_thread(ctx.client.list_disks, ctx.source_vm)
            dest_ds = disks[0].datastore if disks and disks[0].datastore else ""
        if not dest_ds:
            raise VSphereError("無法判定暫存 datastore，請在任務設定指定")
        if ctx.transfer_mode != "ovf":
            # xvc：clone 落點主機即後續搬遷發起者，版本不得高於目的端 vCenter；
            # ovf 串流無此限制（匯出是來源側本地操作），任何主機皆可
            max_ver, max_build = await asyncio.to_thread(ctx.tgt_client.vcenter_release)
    from_template = bool(ctx.scratch.get("vs_src_template"))
    if not from_template and not ctx.snapshot_id:
        raise VSphereError("無快照一致點可供 clone（②未建立快照）")
    host = await asyncio.to_thread(ctx.client.pick_xvc_host, dest_ds, max_ver, max_build)
    temp = f"{prefix}{int(time.time())}"
    await asyncio.to_thread(
        ctx.client.clone_vm_from_snapshot,
        ctx.source_vm, None if from_template else ctx.snapshot_id, temp, dest_ds, host,
        ctx.progress, ctx.should_cancel,
    )
    ctx.scratch["vs_temp"] = temp
    note = f"；已清除上一輪殘留：{', '.join(cleaned)}" if cleaned else ""
    src = "範本現行狀態" if from_template else "一致點"
    return f"已從{src} clone 完整暫存複本 {temp}（{dest_ds}，經 {host}）{note}"


async def step_vs_transfer(ctx: StepContext) -> str:
    """把暫存複本傳到目的端。同 VC：只重接網卡。跨 VC 依傳輸方式：
    - xvc：XVM（Cross-vCenter vMotion / Relocate，由③選定的相容主機發起）
    - ovf：OVF/HTTP 串流（經本程式記憶體中轉，不落地；免主機版本相容）"""
    temp = ctx.scratch.get("vs_temp")
    if not temp:
        raise VSphereError("無暫存複本可搬遷")
    if ctx.tgt_client is ctx.client:
        await asyncio.to_thread(ctx.client.remap_vm_network, temp, ctx.target_network)
        return f"同一座 vCenter：網卡已接 {ctx.target_network}，略過跨 VC 搬遷"
    if ctx.transfer_mode == "ovf":
        await asyncio.to_thread(
            ctx.client.ovf_stream_vm, temp, ctx.tgt_client,
            ctx.target_datastore, ctx.target_network,
            ctx.progress, ctx.should_cancel, ctx.log,
            ctx.source_vm,  # NVRAM 後備來源（暫存複本無 .nvram 時退回原 VM）
        )
        # 匯入成功（目的端已有完整複本＋NVRAM）→ 銷毀來源側暫存複本；
        # 串流中失敗 ovf_stream_vm 已 abort 兩端 lease（目的端半成品自動清除）、
        # NVRAM 補複製失敗則目的端暫存複本留存——兩者皆由下一輪前綴認領清理
        await asyncio.to_thread(ctx.client.destroy_vm_if_exists, temp)
        return (f"已以 OVF 串流傳輸到目的端 {ctx.target_datastore}"
                f"（網卡接 {ctx.target_network}，thin 佈建，含 NVRAM/EFI 開機項目）")
    # 目的端落點主機也要讓「發起端（來源）vCenter」認得：同版本但 build 較新的主機
    # 會被拒（Run #119：DR 的 8.0.3 build 25595708 > HQ vCenter build 24674346）
    src_ver, src_build = await asyncio.to_thread(ctx.client.vcenter_release)
    dest_info = await asyncio.to_thread(
        ctx.tgt_client.get_vmsync_dest_info, ctx.target_datastore, ctx.target_network,
        src_ver, src_build)
    try:
        await asyncio.to_thread(
            ctx.client.xvc_relocate_vm, temp, dest_info, ctx.tgt_vc_creds, ctx.progress)
    except Exception:
        # 回滾：銷毀暫存複本（連碟），不留孤兒
        try:
            await asyncio.to_thread(ctx.client.destroy_vm_if_exists, temp)
            ctx.scratch.pop("vs_temp", None)
        except Exception:
            pass
        raise
    return (f"已以 XVM 搬遷複本到 {ctx.target_datastore}，"
            f"網卡已重接 {ctx.target_network}")


async def step_vs_swap(ctx: StepContext) -> str:
    """目標端換手：刪除上一份複本 → 暫存複本定名。任何一步失敗，
    上一份或暫存複本至少會留下一個可用版本。"""
    temp = ctx.scratch.get("vs_temp")
    if not temp:
        raise VSphereError("無暫存複本可定名")
    final = _replica_final(ctx)
    owner = f"profile:{ctx.profile_id}"
    # 先在暫存複本寫入所有權標記（extraConfig）：OVF 匯入不會帶 extraConfig、
    # XVM 會保留，統一在目的端補寫；日後 ⑥ 只覆蓋帶本任務標記的 VM
    await asyncio.to_thread(ctx.tgt_client.set_vm_owner_tag, temp, owner, ctx.source_vm)
    try:
        old = await asyncio.to_thread(ctx.tgt_client.get_vm, final)
    except VSphereError:
        old = None
    replaced = False
    if old is not None:
        # 安全鎖 1：複本被開機（可能正被 DR 使用）絕不覆蓋
        if old.power_state == "poweredOn":
            raise VSphereError(
                f"目標端複本「{final}」目前為開機狀態（可能使用中），不覆蓋；"
                f"新複本保留為 {temp}，請確認後手動處理"
            )
        # 安全鎖 2：只銷毀本任務先前建立的複本——同名的一般 VM / 範本（撞名、誤填
        # replica_name）沒有標記，一律不動，新複本保留為 temp 讓人工判斷
        old_owner = await asyncio.to_thread(ctx.tgt_client.get_vm_owner_tag, final)
        if old_owner != owner:
            raise VSphereError(
                f"目標端已有同名 VM「{final}」但非本任務建立"
                f"（標記：{old_owner or '無'}），不覆蓋；新複本保留為 {temp}。"
                f"若確認該 VM 是舊版 SnapMan 產生的複本，請在 vCenter 手動刪除，"
                f"或為其加上 extraConfig snapman.owner={owner} 後重跑"
            )
        await asyncio.to_thread(ctx.tgt_client.destroy_vm_if_exists, final)
        if await _vm_exists(ctx.tgt_client, final):
            raise VSphereError(
                f"無法刪除舊複本「{final}」；新複本保留為 {temp}，請手動處理")
        replaced = True
    await asyncio.to_thread(ctx.tgt_client.rename_vm, temp, final)
    ctx.scratch["vs_done"] = True  # 定名即到位，之後失敗回滾不得銷毀複本
    if ctx.scratch.get("vs_src_template"):
        # 範本對範本：複本先以一般 VM 傳到目的端，此處補標記；失敗時複本已完整可用、
        # 只差型別，明確報錯讓人工 MarkAsTemplate（下一輪會視為既有複本正常覆蓋）
        try:
            await asyncio.to_thread(ctx.tgt_client.mark_as_template, final)
        except Exception as exc:
            raise VSphereError(
                f"複本 {final} 已到位但標記為範本失敗：{exc}；請手動在目的端 vCenter 標記")
        return (f"複本已{'更新' if replaced else '建立'}為範本 {final}"
                f"（網卡接 {ctx.target_network}）")
    return (f"複本已{'更新' if replaced else '建立'}為 {final}"
            f"（保持關機，網卡接 {ctx.target_network}）")


async def step_vs_finalize(ctx: StepContext) -> str:
    kind = "範本複本" if ctx.scratch.get("vs_src_template") else "複本"
    return (f"完成。{ctx.source_vm} 已同步為目標端{kind} {_replica_final(ctx)}"
            f"（僅保留最新一份，關機 standby）")


async def step_precheck_target(ctx: StepContext) -> str:
    """掛載歷史副本用的輕量前置檢查：只驗目標 VM（不動來源）。"""
    tgt = await asyncio.to_thread(ctx.tgt_client.get_vm, ctx.target_vm)
    if tgt.power_state != "poweredOn":
        raise VSphereError(f"目標 {ctx.target_vm} 非開機狀態（{tgt.power_state}）")
    if not tgt.tools_running:
        raise VSphereError(f"目標 {ctx.target_vm} 的 VMware Tools 未運作")
    return f"目標 {ctx.target_vm} 開機、Tools 正常"


# 掛載歷史副本的工作流：卸下目前掛載 → 掛上選定副本 → 上線 + ATTACH → 驗證。
# key 沿用每日工作流，manager 的記帳 hook（detach_old/attach_new/attach_db）共用。
MOUNT_STEPS = [
    ("precheck", "① 前置檢查（目標 VM）", step_precheck_target),
    ("detach_old", "② 卸載目前掛載的副本", step_detach_old),
    ("attach_new", "③ 熱掛選定的歷史副本", step_attach_new),
    ("attach_db", "④ 磁碟上線 + SQL ATTACH", step_online_and_attach_db),
    ("verify", "⑤ 驗證（DBCC CHECKDB）", step_verify),
]

# 整機同步（vmsync）工作流：快照 → clone 完整 VM → 跨 VC 搬遷 → 目標端換手。
# 目標端只保留最新一份複本（換手成功才刪舊份），複本保持關機作為 standby。
# key 與資料碟工作流不重疊（clone_vm/vs_*），manager 的副本記帳 hook 不會誤觸發。
VMSYNC_STEPS = [
    ("precheck", "① 前置檢查（來源/目的 datastore/網路）", step_vs_precheck),
    ("snapshot", "② 靜默快照（application-consistent；範本略過）", step_vs_snapshot),
    ("clone_vm", "③ 從一致點 Clone 完整 VM（暫存）", step_vs_clone_vm),
    ("remove_snapshot", "④ 立即移除來源快照", step_remove_snapshot),
    ("vs_transfer", "⑤ 傳輸複本到目的端（XVM 搬遷 / OVF 串流）", step_vs_transfer),
    ("vs_swap", "⑥ 目標端換手（刪舊複本、定名）", step_vs_swap),
    ("finalize", "⑦ 收尾", step_vs_finalize),
]

# 步驟清單：(key, 標題, 函式)
STEPS = [
    ("precheck", "① 前置檢查（開機/Tools/SQL VSS Writer）", step_precheck),
    ("checkpoint", "② SQL CHECKPOINT", step_checkpoint),
    ("snapshot", "③ 靜默快照（application-consistent）", step_snapshot),
    ("clone", "④ Clone 資料 VMDK（獨立碟）", step_clone),
    ("remove_snapshot", "⑤ 立即移除來源快照", step_remove_snapshot),
    ("transfer", "⑤+ 跨 VC 搬遷（僅 xvc 傳輸模式）", step_xvc_transfer),
    ("detach_old", "⑥ 目標卸載上一輪 clone", step_detach_old),
    ("attach_new", "⑦ 熱掛新 clone 到目標", step_attach_new),
    ("attach_db", "⑧ 目標磁碟上線 + SQL ATTACH", step_online_and_attach_db),
    ("verify", "⑨ 驗證（DBCC CHECKDB）", step_verify),
    ("finalize", "⑩ 收尾", step_finalize),
]
