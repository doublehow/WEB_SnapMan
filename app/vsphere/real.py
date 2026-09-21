"""RealVSphere：pyVmomi 實作。

Guest Operations 的 StartProgramInGuest 不會直接回傳 stdout，
guest_run 以 cmd 包一層把輸出重導到 guest 暫存檔，結束後經
InitiateFileTransferFromGuest 取回，供錯誤診斷；成敗仍以 exit code 判斷。
clone / 掛碟等操作牽涉真實環境細節（datastore 路徑、SCSI 控制器、獨立碟模式），
對 production 使用前務必在測試環境完整驗證一輪。
"""
from __future__ import annotations

import queue
import logging
import ssl
import threading
import time
import urllib.request
import uuid

from ..config import settings
from .base import DiskInfo, GuestResult, VMInfo, VSphereClient, VSphereError

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim
except Exception as exc:  # pragma: no cover - 只有真實模式需要
    raise VSphereError(
        "未安裝 pyvmomi 或匯入失敗；真實模式需要 `pip install pyvmomi`。"
    ) from exc


logger = logging.getLogger("snapman.vsphere")


def _ver_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in str(v or "0").split("."))


def _host_ok_for_vc(host_version: str, host_build, vc_version: str, vc_build) -> bool:
    """ESXi 主機是否會被指定 vCenter 認得（XVM 相容判定）：
    主機版本較低 → 可；同版本 → 主機 build 不得高於 vCenter build（未知 build 視為可）。"""
    hv, cv = _ver_tuple(host_version), _ver_tuple(vc_version)
    if hv[:3] < cv[:3]:
        return True
    if hv[:3] > cv[:3]:
        return False
    try:
        return int(host_build or 0) <= int(vc_build or 0) if vc_build else True
    except (TypeError, ValueError):
        return True


def _ssl_thumbprint(host: str, port: int = 443, verify: bool = True) -> str:
    """目的端 vCenter 憑證的 SHA1 指紋（XVC ServiceLocator 需要）。
    verify=True 時經已驗證的 TLS 連線取得（否則是每次連線的 TOFU，中間人可換指紋）；
    目的端 vCenter 設為略過驗證時才退回不驗證取得。"""
    import hashlib
    import socket
    if verify:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=30) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    else:
        pem = ssl.get_server_certificate((host, port))
        der = ssl.PEM_cert_to_DER_cert(pem)
    return ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest())


class RealVSphere(VSphereClient):
    def __init__(self, host: str | None = None, user: str | None = None,
                 password: str | None = None, insecure: bool | None = None):
        # 未指定時退回全域設定（相容單一 vCenter 的舊用法 / preflight）
        self.host = host if host is not None else settings.vcenter_host
        self.user = user if user is not None else settings.vcenter_user
        self.password = password if password is not None else settings.vcenter_password
        self.insecure = insecure if insecure is not None else settings.vcenter_insecure
        self._si = None

    # --- 連線 ---
    def connect(self) -> None:
        if not self.password:
            raise VSphereError(f"vCenter「{self.host}」未設定密碼，請至設定頁填寫")
        ctx = None
        if self.insecure:
            # 執行期留下痕跡：略過驗證代表 vCenter 帳密 / guest 帳密 / OVF 資料流皆可被中間人攔截
            logger.warning("vCenter「%s」連線略過 TLS 憑證驗證（設定頁可改為驗證）", self.host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._si = SmartConnect(
            host=self.host,
            user=self.user,
            pwd=self.password,
            sslContext=ctx,
        )
        if self._si is None:
            raise VSphereError(f"連線 vCenter「{self.host}」失敗")

    def disconnect(self) -> None:
        if self._si is not None:
            Disconnect(self._si)
            self._si = None

    # --- 內部工具 ---
    def _content(self):
        if self._si is None:
            raise VSphereError("尚未連線 vCenter")
        # session 逾時自動重連：xvc 搬遷可長達 30 分鐘以上，期間「另一座」
        # vCenter 的 client 閒置會被回收 session，後續呼叫拋 NotAuthenticated
        # （Run #34 實測：搬遷完成後目的端 unwrap 即中招）。每次取 content 前
        # 驗一次 session，死了就重新登入。
        try:
            if self._si.content.sessionManager.currentSession is None:
                raise VSphereError("session 已逾時")
        except Exception:
            try:
                self.connect()
            except VSphereError:
                raise
            except Exception as exc:
                raise VSphereError(f"vCenter「{self.host}」session 逾時後重連失敗：{exc}")
        return self._si.RetrieveContent()

    def _find_vm(self, name: str):
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            hits = [vm for vm in view.view if vm.name == name]
        finally:
            view.Destroy()
        if not hits:
            raise VSphereError(f"找不到 VM：{name}")
        if len(hits) > 1:
            # 多 DC / 多資料夾允許同名：取第一筆會對錯誤對象快照、clone 甚至銷毀
            raise VSphereError(
                f"vCenter 內有 {len(hits)} 台同名 VM「{name}」，無法唯一定位，"
                f"請先改名或移除多餘者")
        return hits[0]

    @staticmethod
    def _wait(task, on_progress=None, should_cancel=None):
        last = None
        cancel_sent = False
        while task.info.state in (vim.TaskInfo.State.queued, vim.TaskInfo.State.running):
            if on_progress is not None:
                p = task.info.progress  # vSphere 工作進度 0~100（可能為 None）
                if isinstance(p, int) and p != last:
                    last = p
                    try:
                        on_progress(p)
                    except Exception:
                        pass
            if should_cancel is not None and not cancel_sent:
                try:
                    if should_cancel():
                        task.CancelTask()  # 取消後工作轉 error 態，由下方統一拋錯
                        cancel_sent = True
                except Exception:
                    cancel_sent = True  # 取消失敗就等工作自然結束
            time.sleep(0.5)
        if task.info.state != vim.TaskInfo.State.success:
            msg = getattr(task.info.error, "msg", "unknown error")
            raise VSphereError(f"vSphere 工作失敗：{msg}")
        return task.info.result

    def _find_disk(self, vm, source_disk: str):
        """依 label（如 'Hard disk 2'）或 backing 檔名片段找 VirtualDisk。"""
        dev = self._find_disk_in_devices(vm.config.hardware.device, source_disk)
        if dev is None:
            raise VSphereError(f"VM {vm.name} 上找不到資料碟：{source_disk}")
        return dev

    @staticmethod
    def _find_disk_in_devices(devices, source_disk: str):
        for dev in devices:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                if dev.deviceInfo.label == source_disk or source_disk in dev.backing.fileName:
                    return dev
        return None

    def _find_snapshot(self, vm_obj, snapshot_id: str):
        """依 moId 在快照樹中找 snapshot 物件；找不到回 None。"""
        def walk(nodes):
            for n in nodes:
                if str(n.snapshot._moId) == snapshot_id:
                    return n.snapshot
                found = walk(n.childSnapshotList)
                if found:
                    return found
            return None

        if not vm_obj.snapshot:
            return None
        return walk(vm_obj.snapshot.rootSnapshotList)

    # --- 查詢 ---
    def get_vm(self, name: str) -> VMInfo:
        vm = self._find_vm(name)
        tools = vm.guest.toolsRunningStatus == "guestToolsRunning"
        return VMInfo(name=name, power_state=str(vm.runtime.powerState),
                      tools_running=tools, is_template=self._is_template(vm))

    @staticmethod
    def _is_template(vm) -> bool:
        """VM 是否為範本（config 於 inaccessible/孤兒 VM 可能為 None → 視為非範本）。"""
        try:
            return bool(vm.summary.config.template)
        except Exception:
            return False

    def list_vms(self) -> list[VMInfo]:
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            out = []
            for vm in view.view:
                try:
                    tools = vm.guest.toolsRunningStatus == "guestToolsRunning"
                    out.append(
                        VMInfo(
                            name=vm.name,
                            power_state=str(vm.runtime.powerState),
                            tools_running=tools,
                            is_template=self._is_template(vm),
                        )
                    )
                except Exception:
                    continue
        finally:
            view.Destroy()
        return sorted(out, key=lambda v: v.name.lower())

    def list_disks(self, vm: str) -> list[DiskInfo]:
        vm_obj = self._find_vm(vm)
        out = []
        for dev in vm_obj.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                backing = dev.backing
                file_name = getattr(backing, "fileName", "") or ""
                ds = ""
                try:
                    if getattr(backing, "datastore", None):
                        ds = backing.datastore.name
                    elif "]" in file_name:
                        ds = file_name.split("]")[0].replace("[", "").strip()
                except Exception:
                    ds = ""
                out.append(
                    DiskInfo(
                        label=dev.deviceInfo.label if dev.deviceInfo else "",
                        file_name=file_name,
                        capacity_gb=round((dev.capacityInKB or 0) / (1024 * 1024), 1),
                        datastore=ds,
                    )
                )
        return out

    def _find_datastore(self, name: str):
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True
        )
        try:
            for ds in view.view:
                if ds.name == name:
                    return ds
        finally:
            view.Destroy()
        raise VSphereError(f"找不到 datastore：{name}")

    def datastore_usage(self, name: str) -> dict:
        """datastore 總容量 / 剩餘空間（bytes）。"""
        s = self._find_datastore(name).summary
        return {"capacity": s.capacity or 0, "free": s.freeSpace or 0}

    def datastore_url(self, name: str) -> str:
        """datastore 的 volume URL（ds:///vmfs/volumes/<uuid>/）。
        NFS 的 uuid 由 server+path 導出：同一個 export 掛給不同 vCenter 的主機
        URL 相同；「同名但不同台 NAS」URL 不同——共享模式以此驗證真共享，
        防兩地各一台同名儲存的假共享（兩地各一台同型號 NAS、datastore 同名）。"""
        return str(self._find_datastore(name).summary.url or "")

    def _find_dc_of_datastore(self, ds_name: str):
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datacenter], True
        )
        try:
            for dc in view.view:
                for ds in dc.datastore:
                    if ds.name == ds_name:
                        return dc
        finally:
            view.Destroy()
        raise VSphereError(f"找不到 datastore：{ds_name}")

    def copy_vmdk(self, src_path: str, dest_datastore: str, dest_name: str,
                  folder: str = "snapman-archive") -> str:
        """把既有 VMDK 複製到指定 datastore 的資料夾（副本歸檔用）。"""
        content = self._content()
        src_ds = src_path.split("]")[0].lstrip("[").strip()
        src_dc = self._find_dc_of_datastore(src_ds)
        dst_dc = self._find_dc_of_datastore(dest_datastore)
        try:
            content.fileManager.MakeDirectory(
                name=f"[{dest_datastore}] {folder}", datacenter=dst_dc,
                createParentDirectories=True,
            )
        except vim.fault.FileAlreadyExists:
            pass
        dest = f"[{dest_datastore}] {folder}/{dest_name}"
        task = content.virtualDiskManager.CopyVirtualDisk_Task(
            sourceName=src_path, sourceDatacenter=src_dc,
            destName=dest, destDatacenter=dst_dc, force=True,
        )
        self._wait(task)
        return dest

    def list_snapman_files(self, datastore: str) -> list[dict]:
        """snapman-clones/ 與 snapman-archive/ 內的 VMDK 清單（名稱、邏輯大小、修改時間）。"""
        ds = self._find_datastore(datastore)
        details = vim.host.DatastoreBrowser.FileInfo.Details(
            fileSize=True, fileType=True, modification=True
        )
        spec = vim.host.DatastoreBrowser.SearchSpec(
            matchPattern=["snapman-*.vmdk"], details=details
        )
        out = []
        for folder in ("snapman-clones", "snapman-archive"):
            try:
                task = ds.browser.SearchDatastoreSubFolders_Task(
                    datastorePath=f"[{datastore}] {folder}", searchSpec=spec
                )
                self._wait(task)
            except VSphereError:
                continue  # 目錄尚未建立
            for res in task.info.result or []:
                for f in res.file or []:
                    out.append({
                        "name": f"{folder}/{f.path}",
                        "size": int(getattr(f, "fileSize", 0) or 0),
                        "modified": str(getattr(f, "modification", "") or "")[:19],
                    })
        return sorted(out, key=lambda x: x["name"], reverse=True)

    def list_datastores(self) -> list[str]:
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datastore], True
        )
        try:
            names = [ds.name for ds in view.view]
        finally:
            view.Destroy()
        return sorted(names, key=str.lower)

    def check_sql_writer(self, vm: str, guest_user: str, guest_password: str) -> bool:
        # Guest Ops 不回傳 stdout，改以 exit code 判斷（Running → exit 0）
        res = self.guest_run(
            vm,
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-Command \"if ((Get-Service SQLWriter -ErrorAction SilentlyContinue).Status -eq 'Running') { exit 0 } else { exit 1 }\"",
            guest_user,
            guest_password,
        )
        return res.ok

    # --- Guest Operations（透過 VMware Tools）---
    def guest_run(self, vm, program, args, guest_user, guest_password,
                  timeout_s: int = 600) -> GuestResult:
        vm_obj = self._find_vm(vm)
        content = self._content()
        gom = content.guestOperationsManager
        creds = vim.vm.guest.NamePasswordAuthentication(
            username=guest_user, password=guest_password
        )
        # 以 cmd /S /c 包一層，把 stdout+stderr 重導到 guest 暫存檔，結束後取回。
        # /S：整段（第一個到最後一個引號間）視為命令，內層引號原樣保留。
        out_path = rf"C:\Windows\Temp\snapman-{uuid.uuid4().hex}.out"
        wrapped = f'/S /c ""{program}" {args} > "{out_path}" 2>&1"'
        spec = vim.vm.guest.ProcessManager.ProgramSpec(
            programPath=r"C:\Windows\System32\cmd.exe", arguments=wrapped
        )
        pid = gom.processManager.StartProgramInGuest(vm_obj, creds, spec)
        # 等待程式結束並取回 exit code（cmd /c 會回傳子程式的 exit code）。
        # timeout_s 由呼叫端依步驟性質指定：DBCC/CHECKPOINT 對大 DB 可達小時級，
        # 硬限 600 秒會誤殺（run 失敗但 guest 內其實還在跑）
        for _ in range(max(1, int(timeout_s))):
            procs = gom.processManager.ListProcessesInGuest(vm_obj, creds, [pid])
            if procs and procs[0].endTime is not None:
                code = procs[0].exitCode or 0
                out = self._fetch_guest_file(vm_obj, creds, out_path)
                return GuestResult(exit_code=code, stdout=out)
            time.sleep(1)
        raise VSphereError(f"Guest 程式逾時未結束（超過 {timeout_s} 秒）：{program}")

    def _fetch_guest_file(self, vm_obj, creds, path: str) -> str:
        """取回 guest 內的輸出重導檔（讀完即刪）；任何失敗都回空字串，不影響主流程。"""
        fm = self._content().guestOperationsManager.fileManager
        data = b""
        try:
            info = fm.InitiateFileTransferFromGuest(vm_obj, creds, path)
            url = info.url.replace("*", self.host)
            ctx = None
            if self.insecure:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(url, context=ctx, timeout=30) as resp:
                data = resp.read(65536)
        except Exception:
            return ""
        finally:
            try:
                fm.DeleteFileInGuest(vm_obj, creds, path)
            except Exception as exc:
                # 輸出檔可能含查詢結果，留在 guest Temp 要看得到
                logger.warning("guest 暫存輸出檔 %s 刪除失敗：%s", path, exc)
        # sqlcmd / PowerShell 在中文 Windows 常輸出 cp950；先試 utf-8 再退回
        for enc in ("utf-8", "cp950"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", "replace")

    # --- 快照 ---
    def create_quiesced_snapshot(self, vm: str, name: str) -> str:
        vm_obj = self._find_vm(vm)
        task = vm_obj.CreateSnapshot_Task(
            name=name, description="SnapMan quiesced", memory=False, quiesce=True
        )
        self._wait(task)
        snap = vm_obj.snapshot.currentSnapshot
        return str(snap._moId)

    def remove_snapshot(self, vm: str, snapshot_id: str) -> None:
        vm_obj = self._find_vm(vm)
        target = self._find_snapshot(vm_obj, snapshot_id)
        if target is None:
            return
        self._wait(target.RemoveSnapshot_Task(removeChildren=False))

    # --- 磁碟 ---
    def clone_data_disk(
        self, source_vm, source_disk, target_datastore, target_name,
        snapshot_id=None, on_progress=None, should_cancel=None,
    ) -> str:
        vm_obj = self._find_vm(source_vm)
        # 拍完快照後，VM 現行 backing 是快照後新開的 delta（-0000NN.vmdk），
        # 被執行中的 VM 寫入鎖定，CopyVirtualDisk 直接 copy 會失敗。
        # 必須取「快照當下」config 記錄的 backing（已凍結成唯讀的 parent），
        # CopyVirtualDisk 會把該檔連同其 parent 鏈攤平成一顆獨立碟＝一致點副本。
        disk = None
        if snapshot_id:
            snap = self._find_snapshot(vm_obj, snapshot_id)
            if snap is not None:
                disk = self._find_disk_in_devices(snap.config.hardware.device, source_disk)
        if disk is None:
            disk = self._find_disk(vm_obj, source_disk)
        content = self._content()
        vdm = content.virtualDiskManager
        dc = self._get_datacenter(vm_obj)
        ds = target_datastore or self._primary_datastore_name(disk)
        # CopyVirtualDisk 不會自動建目的資料夾，先確保存在
        try:
            content.fileManager.MakeDirectory(
                name=f"[{ds}] snapman-clones", datacenter=dc, createParentDirectories=True
            )
        except vim.fault.FileAlreadyExists:
            pass
        dest = f"[{ds}] snapman-clones/{target_name}.vmdk"
        # 磁碟格式沿用來源（預設 copy spec）；如需 thin/eagerzeroed 再指定 destSpec
        task = vdm.CopyVirtualDisk_Task(
            sourceName=disk.backing.fileName,
            sourceDatacenter=dc,
            destName=dest,
            destDatacenter=dc,
            force=True,
        )
        self._wait(task, on_progress, should_cancel)
        return dest

    def attach_disk(self, vm: str, vmdk_path: str) -> int:
        """熱掛 VMDK，回傳其 SCSI unit number（guest 可據此精確定位該碟）。"""
        vm_obj = self._find_vm(vm)
        controller = self._find_scsi_controller(vm_obj)
        unit = self._next_unit_number(vm_obj, controller)
        backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
            fileName=vmdk_path, diskMode="independent_persistent"
        )
        disk = vim.vm.device.VirtualDisk(
            backing=backing, controllerKey=controller.key, unitNumber=unit, key=-1
        )
        spec = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=disk
        )
        cfg = vim.vm.ConfigSpec(deviceChange=[spec])
        self._wait(vm_obj.ReconfigVM_Task(spec=cfg))
        return unit

    def detach_disk(self, vm: str, vmdk_path: str, delete_backing: bool = False) -> None:
        vm_obj = self._find_vm(vm)
        disk = self._find_disk(vm_obj, vmdk_path)
        op = vim.vm.device.VirtualDeviceSpec.Operation.remove
        spec = vim.vm.device.VirtualDeviceSpec(operation=op, device=disk)
        if delete_backing:
            spec.fileOperation = vim.vm.device.VirtualDeviceSpec.FileOperation.destroy
        cfg = vim.vm.ConfigSpec(deviceChange=[spec])
        self._wait(vm_obj.ReconfigVM_Task(spec=cfg))

    def delete_vmdk(self, vmdk_path: str) -> None:
        # datacenter 必填：多 datacenter 的 vCenter 上省略會一律失敗
        # （A specified parameter was not correct: dc），從 datastore 路徑反查
        content = self._content()
        ds = vmdk_path.split("]")[0].lstrip("[").strip()
        dc = self._find_dc_of_datastore(ds)
        self._wait(content.virtualDiskManager.DeleteVirtualDisk_Task(
            name=vmdk_path, datacenter=dc))

    # --- 跨 VC 搬遷（XVC-provisioning，殼 VM 承載 VMDK）---
    def get_vm_moids(self, vm_name: str, datastore_name: str,
                     peer_version: str = "", peer_build: str = "") -> dict:
        """目的端資訊：以「目標 VM 所在主機」為落點（其可見 target datastore）。
        peer_version/peer_build（發起端 vCenter）非空時先驗該主機相容，不相容直接給
        可讀的錯誤而不是 vSphere 的「does not support hosts of this type」。"""
        vm = self._find_vm(vm_name)
        host = vm.runtime.host
        if peer_version:
            prod = host.config.product
            if not _host_ok_for_vc(prod.version, prod.build, peer_version, peer_build):
                raise VSphereError(
                    f"目標 VM 所在主機 {host.name}（ESXi {prod.version} build {prod.build}）"
                    f"比發起端 vCenter（{peer_version} build {peer_build}）新，XVM 會被拒絕；"
                    f"請把目標 VM 移到較舊主機、升級來源 vCenter，或改用其他傳輸模式")
        pool = host.parent.resourcePool
        dc = self._get_datacenter(vm)
        ds = self._find_datastore(datastore_name)
        return {
            "host": host._moId, "pool": pool._moId,
            "folder": dc.vmFolder._moId, "datastore": ds._moId,
            "instance_uuid": self._content().about.instanceUuid,
            "vc_version": self._content().about.version,
            "vc_build": self._content().about.build,
        }

    def vm_host_release(self, vm_name: str) -> tuple[str, str, str]:
        """VM 所在主機的 (名稱, ESXi 版本, build)。"""
        h = self._find_vm(vm_name).runtime.host
        return h.name, h.config.product.version, h.config.product.build

    def vcenter_version(self) -> str:
        """本 vCenter 版本（如 '7.0.3'）。XVM 主機挑選的相容性上限用。"""
        return self._content().about.version

    def vcenter_release(self) -> tuple[str, str]:
        """(版本, build)，如 ('8.0.3', '24674346')。同版本不同 build 也會被 XVM 拒絕，
        兩個值都要拿來比。"""
        about = self._content().about
        return about.version, about.build

    def pick_xvc_host(self, datastore_name: str, max_version: str = "",
                      max_build: str = "") -> str:
        """挑主機：已連線、看得到指定 datastore，取「與對端 vCenter 相容」者中最新的。

        XVM 的兩端 vCenter 都要認得「對方那台主機」的型別，而 vCenter 只認識不比自己新的
        ESXi：版本較低可以、同版本則 build 不得高於 vCenter。兩個方向都會被拒
        （vCenter does not support hosts of this type）——Run #49：8.0.3 主機 → 7.0.3
        vCenter；Run #119：目的端 8.0.3 build 25595708 主機比來源 vCenter 8.0.3 build
        24674346 新，同樣被拒。max_version/max_build = 對端 vCenter 的版本與 build。"""
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            cands = []
            for h in view.view:
                try:
                    if str(h.runtime.connectionState) != "connected":
                        continue
                    if datastore_name not in [d.name for d in h.datastore]:
                        continue
                    prod = h.config.product
                    cands.append((_ver_tuple(prod.version), int(prod.build or 0), h.name,
                                  prod.version, prod.build))
                except Exception:
                    continue
        finally:
            view.Destroy()
        if not cands:
            raise VSphereError(
                f"找不到可承載的主機（需已連線且看得到 {datastore_name}）")
        eligible = cands
        if max_version:
            eligible = [c for c in cands
                        if _host_ok_for_vc(c[3], c[4], max_version, max_build)]
            if not eligible:
                vers = ", ".join(sorted({f"{c[3]} build {c[4]}" for c in cands}))
                raise VSphereError(
                    f"看得到 {datastore_name} 的主機（ESXi {vers}）皆比對端 vCenter"
                    f"（{max_version} build {max_build or '?'}）新，XVM 搬遷會被拒絕；"
                    f"請升級該 vCenter 或改用 OVF 串流 / 共享 datastore 傳輸模式"
                )
        eligible.sort(reverse=True)
        return eligible[0][2]

    def _find_host(self, name: str):
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            for h in view.view:
                if h.name == name:
                    return h
        finally:
            view.Destroy()
        raise VSphereError(f"找不到主機：{name}")

    def create_shell_vm(self, name: str, datastore: str, vmdk_path: str,
                        host_name: str) -> None:
        """建立最小殼 VM（無網卡、32MB RAM）並掛上既有 VMDK，落點於指定主機。"""
        host = self._find_host(host_name)
        pool = host.parent.resourcePool
        dc = host.parent
        while dc and not isinstance(dc, vim.Datacenter):
            dc = dc.parent
        ctrl = vim.vm.device.VirtualLsiLogicController(
            key=100, busNumber=0,
            sharedBus=vim.vm.device.VirtualSCSIController.Sharing.noSharing,
        )
        disk = vim.vm.device.VirtualDisk(
            key=-100,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                fileName=vmdk_path, diskMode="persistent",
            ),
            controllerKey=100, unitNumber=0,
        )
        cfg = vim.vm.ConfigSpec(
            name=name, guestId="otherGuest64", numCPUs=1, memoryMB=32,
            # 固定低硬體版本（vmx-13 = 6.5+）：殼 VM 若沿用新主機預設（如 vmx-21），
            # 搬到較舊的目的端主機會不相容；殼 VM 只掛碟不開機，低版本無妨
            version="vmx-13",
            files=vim.vm.FileInfo(vmPathName=f"[{datastore}]"),
            deviceChange=[
                vim.vm.device.VirtualDeviceSpec(
                    operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=ctrl),
                vim.vm.device.VirtualDeviceSpec(
                    operation=vim.vm.device.VirtualDeviceSpec.Operation.add, device=disk),
            ],
        )
        self._wait(dc.vmFolder.CreateVM_Task(config=cfg, pool=pool, host=host))

    @staticmethod
    def _network_backing(net: dict):
        """依網路資訊（_find_network_info 產出）組網卡 backing。"""
        if net["type"] == "dvs":
            return vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                port=vim.dvs.PortConnection(
                    switchUuid=net["dvs_uuid"], portgroupKey=net["pg_key"]))
        return vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
            deviceName=net["name"], network=vim.Network(net["moid"]))

    def xvc_relocate_vm(self, vm_name: str, dest_info: dict, dest_vc: dict,
                        on_progress=None) -> None:
        """Cross-vCenter Relocate：把 VM（連碟）搬到另一座 vCenter。
        dest_info 含 "network" 時，一併把所有網卡重對應到目的端 portgroup
        （兩座 VC 的 portgroup 名稱/moid 不同，不重對應會搬遷失敗）。"""
        vm = self._find_vm(vm_name)
        spec = vim.vm.RelocateSpec()
        spec.datastore = vim.Datastore(dest_info["datastore"])
        spec.host = vim.HostSystem(dest_info["host"])
        spec.pool = vim.ResourcePool(dest_info["pool"])
        spec.folder = vim.Folder(dest_info["folder"])
        net = dest_info.get("network")
        if net:
            changes = []
            for dev in vm.config.hardware.device:
                if isinstance(dev, vim.vm.device.VirtualEthernetCard):
                    dev.backing = self._network_backing(net)
                    changes.append(vim.vm.device.VirtualDeviceSpec(
                        operation=vim.vm.device.VirtualDeviceSpec.Operation.edit,
                        device=dev))
            spec.deviceChange = changes
        spec.service = vim.ServiceLocator(
            instanceUuid=dest_info["instance_uuid"],
            url=f"https://{dest_vc['host']}/sdk",
            sslThumbprint=_ssl_thumbprint(dest_vc["host"],
                                          verify=not dest_vc.get("insecure", True)),
            credential=vim.ServiceLocatorNamePassword(
                username=dest_vc["user"], password=dest_vc["password"],
            ),
        )
        task = vm.RelocateVM_Task(
            spec=spec, priority=vim.VirtualMachine.MovePriority.highPriority
        )
        self._wait(task, on_progress)

    def shell_unwrap(self, shell_name: str, final_datastore: str, final_name: str) -> str:
        """（目的端）取下殼 VM 的碟 → 移入 snapman-clones/ → 銷毀殼 VM，回傳最終路徑。"""
        vm = self._find_vm(shell_name)
        disk = None
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                disk = dev
                break
        if disk is None:
            raise VSphereError(f"殼 VM {shell_name} 上找不到磁碟")
        src_file = disk.backing.fileName
        # 拆碟（保留檔案）
        spec = vim.vm.device.VirtualDeviceSpec(
            operation=vim.vm.device.VirtualDeviceSpec.Operation.remove, device=disk)
        self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(deviceChange=[spec])))
        # 移出殼 VM 目錄（銷毀殼 VM 會刪其資料夾）
        content = self._content()
        dc = self._get_datacenter(vm)
        try:
            content.fileManager.MakeDirectory(
                name=f"[{final_datastore}] snapman-clones", datacenter=dc,
                createParentDirectories=True)
        except vim.fault.FileAlreadyExists:
            pass
        dest = f"[{final_datastore}] snapman-clones/{final_name}.vmdk"
        self._wait(content.virtualDiskManager.MoveVirtualDisk_Task(
            sourceName=src_file, sourceDatacenter=dc,
            destName=dest, destDatacenter=dc, force=True))
        self._wait(vm.Destroy_Task())
        return dest

    def destroy_vm_if_exists(self, name: str) -> None:
        """回滾用：銷毀殼 VM（連同其掛載的碟一併刪除）。"""
        try:
            vm = self._find_vm(name)
        except VSphereError:
            return
        try:
            self._wait(vm.Destroy_Task())
        except Exception:
            pass

    # --- 整機同步（vmsync：完整 VM 複本 → 另一座 vCenter）---
    def list_networks(self) -> list[str]:
        """列出網路（portgroup）名稱，排除 DVS uplink。供 vmsync 網卡對應選單。"""
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Network], True
        )
        try:
            names = []
            for n in view.view:
                try:
                    if (isinstance(n, vim.dvs.DistributedVirtualPortgroup)
                            and getattr(n.config, "uplink", False)):
                        continue
                    names.append(n.name)
                except Exception:
                    continue
        finally:
            view.Destroy()
        return sorted(set(names), key=str.lower)

    def _find_network_info(self, name: str) -> dict:
        """網路名 → 組網卡 backing 所需資訊（標準 portgroup 或 DVS portgroup）。"""
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Network], True
        )
        try:
            for n in view.view:
                if n.name == name:
                    if isinstance(n, vim.dvs.DistributedVirtualPortgroup):
                        return {"type": "dvs", "pg_key": n.key,
                                "dvs_uuid": n.config.distributedVirtualSwitch.uuid}
                    return {"type": "std", "moid": n._moId, "name": name}
        finally:
            view.Destroy()
        raise VSphereError(f"找不到網路（portgroup）：{name}")

    def get_vmsync_dest_info(self, datastore_name: str, network_name: str,
                             peer_version: str = "", peer_build: str = "") -> dict:
        """（目的端）vmsync 落點資訊：挑看得到目的 datastore、且發起端 vCenter
        認得（版本/build 不比它新）的主機中最新者，回傳 host/pool/folder/datastore
        moid、目的網路資訊與 instanceUuid（XVM ServiceLocator 用）。"""
        host = self._find_host(self.pick_xvc_host(datastore_name, peer_version, peer_build))
        pool = host.parent.resourcePool
        dc = host.parent
        while dc and not isinstance(dc, vim.Datacenter):
            dc = dc.parent
        ds = self._find_datastore(datastore_name)
        info = {
            "host": host._moId, "pool": pool._moId,
            "folder": dc.vmFolder._moId, "datastore": ds._moId,
            "instance_uuid": self._content().about.instanceUuid,
        }
        if network_name:
            info["network"] = self._find_network_info(network_name)
        return info

    def clone_vm_from_snapshot(self, source_vm: str, snapshot_id: str | None,
                               clone_name: str, datastore: str, host_name: str,
                               on_progress=None, should_cancel=None) -> None:
        """從快照一致點 full clone 出一台關機的完整 VM（開機中的來源不受影響）。
        snapshot_id 為 None 時直接以來源現行狀態 clone（範本無法拍快照、本身即靜態）。
        落點指定主機——後續 XVC 搬遷由該主機發起（沿用 pick_xvc_host 的版本挑選）；
        範本沒有 resource pool，clone 亦必須明確指定 host/pool。"""
        vm_obj = self._find_vm(source_vm)
        snap = None
        if snapshot_id:
            snap = self._find_snapshot(vm_obj, snapshot_id)
            if snap is None:
                raise VSphereError("找不到快照，無法從一致點 clone 完整 VM")
        host = self._find_host(host_name)
        relo = vim.vm.RelocateSpec()
        relo.datastore = self._find_datastore(datastore)
        relo.host = host
        relo.pool = host.parent.resourcePool
        # 複本一律先以一般 VM 產生（範本不可 Relocate/OVF 匯出），需要時於目的端再標記
        spec = vim.vm.CloneSpec(location=relo, powerOn=False, template=False)
        if snap is not None:
            spec.snapshot = snap
        dc = self._get_datacenter(vm_obj)
        task = vm_obj.CloneVM_Task(folder=dc.vmFolder, name=clone_name, spec=spec)
        self._wait(task, on_progress, should_cancel)

    def rename_vm(self, name: str, new_name: str) -> None:
        vm = self._find_vm(name)
        self._wait(vm.Rename_Task(newName=new_name))

    # --- 複本所有權標記（vmsync ⑥ 覆蓋前比對，防撞名刪到非 SnapMan 的 VM）---
    OWNER_KEY = "snapman.owner"

    def set_vm_owner_tag(self, name: str, owner: str, source_vm: str) -> None:
        vm = self._find_vm(name)
        self._wait(vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(extraConfig=[
            vim.option.OptionValue(key=self.OWNER_KEY, value=owner),
            vim.option.OptionValue(key="snapman.source", value=source_vm),
        ])))

    def get_vm_owner_tag(self, name: str) -> str:
        vm = self._find_vm(name)
        try:
            for o in vm.config.extraConfig or []:
                if o.key == self.OWNER_KEY:
                    return str(o.value or "")
        except Exception:
            return ""
        return ""

    def mark_as_template(self, name: str) -> None:
        """把（關機的）VM 標記為範本；已是範本則略過。MarkAsTemplate 為同步呼叫、無 task。"""
        vm = self._find_vm(name)
        if self._is_template(vm):
            return
        if str(vm.runtime.powerState) != "poweredOff":
            raise VSphereError(f"{name} 非關機狀態，無法標記為範本")
        vm.MarkAsTemplate()

    # --- OVF/HTTP 串流（vmsync 傳輸方式二：免共享儲存、免主機版本相容）---
    def _find_network_obj(self, name: str):
        content = self._content()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Network], True
        )
        try:
            for n in view.view:
                if n.name == name:
                    return n
        finally:
            view.Destroy()
        raise VSphereError(f"找不到網路（portgroup）：{name}")

    def vm_has_vtpm(self, name: str) -> bool:
        """VM 是否帶 vTPM（vTPM/加密 VM 不可 OVF 匯出，前置檢查用）。"""
        vm = self._find_vm(name)
        return any(isinstance(d, vim.vm.device.VirtualTPM)
                   for d in vm.config.hardware.device)

    def _http_ctx(self):
        ctx = None
        if self.insecure:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _session_cookie(self) -> str:
        """目前 SOAP session 的 cookie（datastore /folder API 沿用同一 session）。"""
        self._content()  # 順帶驗 session、逾時自動重連
        return self._si._stub.cookie.split(";")[0]

    def _datastore_request(self, method: str, ds_path: str, dc_name: str,
                           data: bytes | None = None,
                           timeout: float = 120.0) -> bytes:
        """經 vCenter /folder API 讀寫 datastore 檔案（GET/PUT，帶 session cookie）。
        ds_path 形如 "[DS] dir/file"；多 datacenter 環境 dcPath 必帶。"""
        import urllib.request
        from urllib.parse import quote
        ds, rel = ds_path.split("] ", 1)
        url = (f"https://{self.host}/folder/{quote(rel)}"
               f"?dcPath={quote(dc_name)}&dsName={quote(ds.lstrip('['))}")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Cookie": self._session_cookie()})
        try:
            with urllib.request.urlopen(
                    req, context=self._http_ctx(), timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            raise VSphereError(f"datastore 檔案 {method} 失敗（{ds_path}）：{exc}") from exc

    @staticmethod
    def _nvram_file(vm) -> str:
        """VM 的 .nvram datastore 路徑（layoutEx；沒有則回空字串）。"""
        try:
            for f in vm.layoutEx.file:
                if str(f.type) == "nvram":
                    return f.name
        except Exception:
            pass
        return ""

    @staticmethod
    def _wait_lease_ready(lease, what: str, timeout_s: int = 300) -> None:
        for _ in range(timeout_s * 2):
            if lease.state == vim.HttpNfcLease.State.ready:
                return
            if lease.state == vim.HttpNfcLease.State.error:
                msg = getattr(lease.error, "msg", "") or "unknown"
                raise VSphereError(f"{what} lease 失敗：{msg}")
            time.sleep(0.5)
        raise VSphereError(f"{what} lease 逾時未就緒")

    @staticmethod
    def _ovf_errs(errs) -> str:
        return "; ".join(
            getattr(e, "localizedMessage", None) or str(e) for e in (errs or []))

    def ovf_stream_vm(self, vm_name: str, tgt_client: "RealVSphere",
                      dest_datastore: str, dest_network: str,
                      on_progress=None, should_cancel=None, on_log=None,
                      nvram_src_vm: str = "") -> None:
        """把（關機的）VM 以 OVF/HTTP 串流複製到另一座 vCenter。

        資料流：來源主機 →（HTTP GET）→ 本程式記憶體緩衝 →（HTTP POST）→
        目的主機，不落地、不需共享儲存、不做跨 VC 遷移操作——因此沒有
        XVC 的主機版本相容限制（Run #49 的版本三明治即以此解）。
        中途失敗 abort 兩端 lease：目的端半成品 VM 由 vSphere 自動清除。
        網卡對應：OVF networkMapping 把來源全部網路接到 dest_network。
        NVRAM：OVF lease 只傳磁碟，.nvram（EFI 開機項目）不隨行——匯入後
        另以 datastore /folder API 補複製（見 _copy_nvram；JOB03 複本開不了機
        的根因修正）。vm_name 沒有 .nvram 時退回 nvram_src_vm（原始來源 VM）。
        限制：VM 硬體版本仍須目的端主機支援；vTPM/加密 VM 不可匯出。"""
        vm = self._find_vm(vm_name)
        content = self._content()
        # 1) OVF 描述檔（來源側）＋ 目的端 import spec（網路對應、thin、落點）
        # pyVmomi 參數名為 obj/cdp（VMODL 定義），以位置參數傳遞
        desc = content.ovfManager.CreateDescriptor(
            vm, vim.OvfManager.CreateDescriptorParams())
        if desc.error:
            raise VSphereError(f"OVF 描述檔建立失敗：{self._ovf_errs(desc.error)}")
        tgt_content = tgt_client._content()
        parsed = tgt_content.ovfManager.ParseDescriptor(
            desc.ovfDescriptor,
            vim.OvfManager.ParseDescriptorParams(deploymentOption=""))
        net_obj = tgt_client._find_network_obj(dest_network)
        host = tgt_client._find_host(tgt_client.pick_xvc_host(dest_datastore))
        pool = host.parent.resourcePool
        dc = host.parent
        while dc and not isinstance(dc, vim.Datacenter):
            dc = dc.parent
        spec = tgt_content.ovfManager.CreateImportSpec(
            desc.ovfDescriptor, pool, tgt_client._find_datastore(dest_datastore),
            vim.OvfManager.CreateImportSpecParams(
                entityName=vm_name, diskProvisioning="thin",
                networkMapping=[
                    vim.OvfManager.NetworkMapping(name=n.name, network=net_obj)
                    for n in (parsed.network or [])
                ],
            ))
        if spec.error:
            raise VSphereError(
                f"OVF 匯入規格建立失敗（硬體版本超出目的端支援？）："
                f"{self._ovf_errs(spec.error)}")
        # 2) 兩端 lease
        src_lease = vm.ExportVm()
        self._wait_lease_ready(src_lease, "匯出")
        try:
            dst_lease = pool.ImportVApp(spec.importSpec, folder=dc.vmFolder, host=host)
            self._wait_lease_ready(dst_lease, "匯入")
            dst_vm = dst_lease.info.entity  # 匯入完成後補傳 NVRAM 用
        except Exception:
            try:
                src_lease.HttpNfcLeaseAbort()
            except Exception:
                pass
            raise
        # 3) 逐碟管線串流（不落地、零本機暫存空間）。
        #    deviceUrl 的 * 依 VMware 文件替換為「VM 所在 ESXi 主機」
        #    （Run #51 教訓：換成 vCenter 會經 reverse proxy，又慢又被重置）。
        #    來源收尾停滯由 _pipe_stream 的「涓流保留」處理（Run #54 根因）。
        #    keepalive 執行緒每 25 秒對兩端 lease 回報進度、每 3 分鐘發報告。
        total = sum(d.capacityInKB * 1024 for d in vm.config.hardware.device
                    if isinstance(d, vim.vm.device.VirtualDisk)) or 1
        src_host_name = vm.runtime.host.name
        state = {"moved": 0}
        done = threading.Event()
        _REPORT_SECS = 180.0

        def _keepalive() -> None:
            rep_t, rep_b = time.monotonic(), 0
            while not done.wait(25):
                moved = state["moved"]
                pct = min(99, int(moved * 100 / total))
                for ls in (src_lease, dst_lease):
                    try:
                        ls.HttpNfcLeaseProgress(pct)
                    except Exception:
                        pass
                if on_progress is not None:
                    try:
                        on_progress(pct)
                    except Exception:
                        pass
                now = time.monotonic()
                if on_log is not None and now - rep_t >= _REPORT_SECS:
                    rate = (moved - rep_b) / (now - rep_t) / (1024 * 1024)
                    try:
                        on_log(
                            f"OVF 串流：已傳 {moved / 1024**3:.1f} GB"
                            f"（容量進度約 {pct}%），"
                            f"近 {(now - rep_t) / 60:.0f} 分鐘平均 "
                            f"{rate:.1f} MB/s（≈ {rate * 8:.0f} Mbps）"
                        )
                    except Exception:
                        pass
                    rep_t, rep_b = now, moved

        ka = threading.Thread(target=_keepalive, daemon=True)
        ka.start()
        try:
            src_disks = [d for d in src_lease.info.deviceUrl if d.disk]
            dst_disks = [d for d in dst_lease.info.deviceUrl if d.disk]
            if len(src_disks) != len(dst_disks):
                raise VSphereError(
                    f"匯出/匯入磁碟數不一致（{len(src_disks)}/{len(dst_disks)}）")
            for i, (s_du, d_du) in enumerate(zip(src_disks, dst_disks)):
                s_url = (s_du.url.replace("*", src_host_name)
                         if "*" in s_du.url else s_du.url)
                d_url = (d_du.url.replace("*", host.name)
                         if "*" in d_du.url else d_du.url)
                self._pipe_stream(s_url, d_url, self._http_ctx(),
                                  tgt_client._http_ctx(), state, should_cancel,
                                  on_log, f"磁碟 {i + 1}/{len(src_disks)}")
            src_lease.HttpNfcLeaseComplete()
            dst_lease.HttpNfcLeaseComplete()
        except Exception as exc:
            for ls in (src_lease, dst_lease):
                try:
                    ls.HttpNfcLeaseAbort()
                except Exception:
                    pass
            raise VSphereError(f"OVF 串流失敗：{exc}") from exc
        finally:
            done.set()
        # 4) 補複製 NVRAM（lease 已完成、目的端 VM 已成形）。EFI 複本沒有
        #    NVRAM 就沒有開機項目（開不了機），失敗即讓本步失敗；BIOS 只記警告。
        #    此時失敗不會清目的端半成品——留給下一輪 clone_vm 的前綴認領清理。
        try:
            self._copy_nvram(vm, nvram_src_vm, tgt_client, dst_vm, dc.name, on_log)
        except Exception as exc:
            if vm.config.firmware == "efi":
                raise VSphereError(
                    f"NVRAM 複製失敗（EFI 複本將無開機項目）：{exc}") from exc
            if on_log is not None:
                try:
                    on_log(f"OVF 串流：NVRAM 複製失敗（BIOS 韌體、不影響開機）：{exc}")
                except Exception:
                    pass
        if on_progress is not None:
            try:
                on_progress(100)
            except Exception:
                pass

    def _copy_nvram(self, src_vm, fallback_name: str, tgt_client: "RealVSphere",
                    dst_vm, dst_dc_name: str, on_log=None) -> None:
        """把來源 VM 的 .nvram 複製到 OVF 匯入後的目的端 VM 資料夾。

        來源優先取 src_vm（暫存複本，clone 會帶原 VM 的 nvram）；layoutEx
        找不到時退回 fallback_name（原始來源 VM）。目的檔名沿用匯入 VM vmx
        既有的 nvram 設定，沒有就以 VM 名補設（ReconfigVM extraConfig）——
        之後 vs_swap 只改 inventory 名、不動檔案，此設定持續有效。"""
        nv_path = self._nvram_file(src_vm)
        nv_owner = src_vm.name
        if not nv_path and fallback_name:
            try:
                nv_path = self._nvram_file(self._find_vm(fallback_name))
                nv_owner = fallback_name
            except VSphereError:
                pass
        if not nv_path:
            if src_vm.config.firmware == "efi":
                raise VSphereError(
                    f"{src_vm.name} 與後備 {fallback_name or '（未指定）'} 皆無 .nvram 檔")
            if on_log is not None:
                try:
                    on_log("OVF 串流：來源無 .nvram（BIOS 韌體），略過 NVRAM 複製")
                except Exception:
                    pass
            return
        data = self._datastore_request(
            "GET", nv_path, self._get_datacenter(src_vm).name)
        dst_cfg = dst_vm.config
        nv_name = next((str(o.value) for o in dst_cfg.extraConfig
                        if o.key == "nvram" and o.value), "") or f"{dst_vm.name}.nvram"
        dst_dir = dst_cfg.files.vmPathName.rsplit("/", 1)[0]
        tgt_client._datastore_request(
            "PUT", f"{dst_dir}/{nv_name}", dst_dc_name, data=data)
        if not any(o.key == "nvram" and o.value for o in dst_cfg.extraConfig):
            self._wait(dst_vm.ReconfigVM_Task(spec=vim.vm.ConfigSpec(
                extraConfig=[vim.option.OptionValue(key="nvram", value=nv_name)])))
        if on_log is not None:
            try:
                on_log(f"OVF 串流：已複製 NVRAM（{nv_owner}，{len(data)} bytes"
                       f"，EFI 開機項目隨行）")
            except Exception:
                pass


    @staticmethod
    def _open_conn(url: str, ssl_ctx, timeout: float):
        """開 HTTP(S) 連線並啟用 TCP keepalive（15 秒閒置後每 5 秒探測）——
        防線路中間設備（防火牆/NAT）把長連線視為閒置而中斷。"""
        import http.client
        import socket as _socket
        from urllib.parse import urlsplit
        u = urlsplit(url)
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(
                u.hostname, u.port or 443, context=ssl_ctx, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(
                u.hostname, u.port or 80, timeout=timeout)
        conn.connect()
        try:
            conn.sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            if hasattr(_socket, "SIO_KEEPALIVE_VALS"):  # Windows
                conn.sock.ioctl(_socket.SIO_KEEPALIVE_VALS, (1, 15000, 5000))
        except Exception:
            pass
        path = u.path + (f"?{u.query}" if u.query else "")
        return conn, path, u.hostname

    @classmethod
    def _pipe_stream(cls, src_url: str, dst_url: str, src_ctx, dst_ctx,
                     state: dict, should_cancel, on_log=None,
                     label: str = "") -> None:
        """單一磁碟管線串流：讀端執行緒 GET → 有界佇列（16×4MB ≈ 64MB
        記憶體上限）→ 本執行緒 chunked POST。不落地、零本機暫存空間。

        涓流保留（Run #54 根因的解法）：ESXi 匯出流在接近結尾處會停滯數分鐘
        （隔離實測 15.9GB 的流在 15.1GB 處停了近 5 分鐘），期間上傳端若無
        資料可送會被目的端以閒置切斷（10053/10054）。因此上傳永遠保留最後
        64KB 不送出；來源停滯時每 15 秒以 1-byte chunk 涓流送出保留資料
        維持連線活性（資料不變、只是尾段送得慢），資料恢復後一併補上。
        64KB ÷ 15 秒 ≈ 可撐 11 天的停滯。下載端逾時 600 秒容忍停滯。"""
        _RESERVE = 64 * 1024
        _TRICKLE_IDLE = 15.0

        def _log(msg: str) -> None:
            if on_log is not None:
                try:
                    on_log(f"OVF 串流 {label}：{msg}")
                except Exception:
                    pass

        q: queue.Queue = queue.Queue(maxsize=16)
        stop = threading.Event()
        hold: dict = {"exc": None, "src_bytes": 0}

        s_conn, s_path, src_host = cls._open_conn(src_url, src_ctx, 600.0)
        d_conn, d_path, dst_host = cls._open_conn(dst_url, dst_ctx, 300.0)

        def _reader() -> None:
            try:
                s_conn.request("GET", s_path)
                resp = s_conn.getresponse()
                if resp.status != 200:
                    raise VSphereError(f"HTTP {resp.status} {resp.reason}")
                while not stop.is_set():
                    chunk = resp.read(4 << 20)
                    if not chunk:
                        break
                    hold["src_bytes"] += len(chunk)
                    while not stop.is_set():
                        try:
                            q.put(chunk, timeout=1)
                            break
                        except queue.Full:
                            continue
                if not stop.is_set():
                    _log(f"來源下載完成（{hold['src_bytes'] / 1024**3:.1f} GB）")
            except Exception as exc:
                hold["exc"] = VSphereError(
                    f"下載中斷（{src_host}，已收 "
                    f"{hold['src_bytes'] / 1024**3:.1f} GB）：{exc}")
            try:
                q.put(None, timeout=1)
            except queue.Full:
                pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        sent = 0

        def _send_chunk(data: bytes) -> None:
            nonlocal sent
            try:
                d_conn.send(b"%x\r\n" % len(data))
                d_conn.send(data)
                d_conn.send(b"\r\n")
            except Exception as exc:
                raise VSphereError(
                    f"上傳中斷（{dst_host}，已送 {sent / 1024**3:.1f} GB）"
                    f"：{exc}") from exc
            sent += len(data)
            state["moved"] += len(data)

        try:
            d_conn.putrequest("POST", d_path)
            d_conn.putheader("Content-Type", "application/x-vnd.vmware-streamVmdk")
            d_conn.putheader("Transfer-Encoding", "chunked")
            d_conn.endheaders()
            pending = bytearray()   # 涓流保留緩衝（上傳恆落後來源 ≤ 64KB）
            idle_since = None
            trickled = False
            while True:
                if should_cancel is not None and should_cancel():
                    raise VSphereError("使用者要求停止")
                try:
                    chunk = q.get(timeout=1)
                except queue.Empty:
                    if not t.is_alive():
                        if hold["exc"] is not None:
                            raise hold["exc"]
                        break  # 讀端已正常結束且佇列空
                    now = time.monotonic()
                    if idle_since is None:
                        idle_since = now
                    elif pending and now - idle_since >= _TRICKLE_IDLE:
                        # 來源停滯中：滴一個 byte 維持上傳連線活性
                        _send_chunk(bytes(pending[:1]))
                        del pending[:1]
                        idle_since = now
                        if not trickled:
                            trickled = True
                            _log("來源暫時停滯（ESXi 收尾處理），"
                                 "以涓流維持上傳連線待其恢復…")
                    continue
                if chunk is None:
                    if hold["exc"] is not None:
                        raise hold["exc"]
                    break
                idle_since = None
                pending.extend(chunk)
                if len(pending) > _RESERVE:
                    out = bytes(pending[:-_RESERVE])
                    del pending[:-_RESERVE]
                    _send_chunk(out)
            # 讀端結束：補送保留資料 → 終止 chunk → 等目的端確認
            if pending:
                _send_chunk(bytes(pending))
            _log(f"資料傳輸完成（{sent / 1024**3:.1f} GB），等待目的端確認…")
            try:
                d_conn.send(b"0\r\n\r\n")
                resp = d_conn.getresponse()
                body = resp.read(4096)
            except Exception as exc:
                raise VSphereError(
                    f"等待目的端確認失敗（{dst_host}，已送 "
                    f"{sent / 1024**3:.1f} GB）：{exc}") from exc
            if resp.status not in (200, 201):
                raise VSphereError(
                    f"目的端回應 HTTP {resp.status}（{dst_host}）："
                    f"{body[:200]!r}")
            _log(f"目的端已確認接收（HTTP {resp.status}）")
        finally:
            stop.set()
            t.join(timeout=5)
            for c in (s_conn, d_conn):
                try:
                    c.close()
                except Exception:
                    pass

    def remap_vm_network(self, vm_name: str, network_name: str) -> None:
        """把 VM 所有網卡重接到指定 portgroup（同 VC 的 vmsync 用，
        跨 VC 時由 xvc_relocate_vm 的 deviceChange 處理）。"""
        vm = self._find_vm(vm_name)
        net = self._find_network_info(network_name)
        changes = []
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualEthernetCard):
                dev.backing = self._network_backing(net)
                changes.append(vim.vm.device.VirtualDeviceSpec(
                    operation=vim.vm.device.VirtualDeviceSpec.Operation.edit,
                    device=dev))
        if changes:
            self._wait(vm.ReconfigVM_Task(
                spec=vim.vm.ConfigSpec(deviceChange=changes)))

    # --- 更多內部工具 ---
    def _get_datacenter(self, vm):
        parent = vm.parent
        while parent and not isinstance(parent, vim.Datacenter):
            parent = parent.parent
        return parent

    def _primary_datastore_name(self, disk) -> str:
        """指定來源碟「當下所在」的 datastore（xvc 暫存留空時的自動落點）。
        backing 無 datastore 參照時（如取自快照 config），從檔案路徑解析。"""
        if getattr(disk.backing, "datastore", None):
            return disk.backing.datastore.name
        file_name = getattr(disk.backing, "fileName", "") or ""
        if file_name.startswith("["):
            return file_name.split("]")[0].lstrip("[").strip()
        raise VSphereError(f"無法判定來源碟所在 datastore：{file_name or '(無 backing 路徑)'}")

    def _find_scsi_controller(self, vm):
        for dev in vm.config.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualSCSIController):
                return dev
        raise VSphereError(f"VM {vm.name} 無 SCSI 控制器")

    def _next_unit_number(self, vm, controller) -> int:
        used = {
            dev.unitNumber
            for dev in vm.config.hardware.device
            if isinstance(dev, vim.vm.device.VirtualDisk) and dev.controllerKey == controller.key
        }
        for n in range(0, 16):
            if n != 7 and n not in used:  # 7 保留給控制器
                return n
        raise VSphereError("SCSI 控制器無可用 unit number")
