"""vSphere 客戶端介面與共用型別。

所有方法都是「同步阻塞」設計；工作流會用 asyncio.to_thread 包起來呼叫。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class VSphereError(RuntimeError):
    """vSphere 操作失敗。"""


@dataclass
class VMInfo:
    name: str
    power_state: str          # poweredOn / poweredOff
    tools_running: bool
    sql_writer_ok: bool = False
    is_template: bool = False  # VM 範本（不可拍快照、恆關機；vmsync 直接 clone）


@dataclass
class DiskInfo:
    label: str                # 例：Hard disk 2
    file_name: str            # backing 檔路徑，例：[ds] vm/vm.vmdk
    capacity_gb: float = 0.0
    datastore: str = ""       # 所在 datastore 名稱


@dataclass
class GuestResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class VSphereClient(Protocol):
    """精簡到本工作流需要的操作面。"""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def get_vm(self, name: str) -> VMInfo: ...

    def list_vms(self) -> list[VMInfo]:
        """列出所有 VM（供設定頁下拉選單）。"""
        ...

    def list_disks(self, vm: str) -> list[DiskInfo]:
        """列出指定 VM 的所有虛擬磁碟。"""
        ...

    def list_datastores(self) -> list[str]:
        """列出所有 datastore 名稱。"""
        ...

    def check_sql_writer(self, vm: str, guest_user: str, guest_password: str) -> bool:
        """確認 guest 內 SQL Server VSS Writer 是否可用。"""
        ...

    def guest_run(
        self, vm: str, program: str, args: str, guest_user: str, guest_password: str,
        timeout_s: int = 600,
    ) -> GuestResult:
        """透過 VMware Tools Guest Operations 在 guest 內執行程式。

        timeout_s：等待程式結束的上限（秒）；長時間 SQL 操作（DBCC 等）由呼叫端放寬。
        """
        ...

    def create_quiesced_snapshot(self, vm: str, name: str) -> str:
        """建立靜默（quiesce）快照，回傳 snapshot id。"""
        ...

    def remove_snapshot(self, vm: str, snapshot_id: str) -> None: ...

    def clone_data_disk(
        self, source_vm: str, source_disk: str, target_datastore: str,
        target_name: str, snapshot_id: str | None = None,
        on_progress=None, should_cancel=None,
    ) -> str:
        """從快照一致點 clone 出一顆獨立 VMDK，回傳其 datastore 路徑。

        snapshot_id：以該快照當下的磁碟 backing 為 copy 來源
        （執行中 VM 的現行 backing 是快照後的 delta，鎖定中不可直接 copy）。
        """
        ...

    def attach_disk(self, vm: str, vmdk_path: str) -> int:
        """把既有 VMDK 熱掛到（開機中的）VM，回傳其 SCSI unit number
        （guest 內腳本據此精確定位剛掛的碟）。"""
        ...

    def detach_disk(self, vm: str, vmdk_path: str, delete_backing: bool = False) -> None: ...

    def delete_vmdk(self, vmdk_path: str) -> None: ...
