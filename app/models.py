"""資料模型：Profile（任務設定）、Run（一次執行）、StepLog（步驟日誌）。"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class VCenter(Base):
    """一座 vCenter 的連線資訊（多 vCenter 支援；任務各自綁定一座）。"""

    __tablename__ = "vcenters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)   # 顯示名稱
    host: Mapped[str] = mapped_column(String(200))
    user: Mapped[str] = mapped_column(String(200))
    password: Mapped[str] = mapped_column(String(300), default="")
    insecure: Mapped[bool] = mapped_column(default=False)   # 略過 TLS 驗證（預設驗證）
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

class Profile(Base):
    """一組「來源 VM → 目標 VM」的搬運設定（來源/目標可在不同 vCenter）。"""

    __tablename__ = "profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)

    # 任務類型：
    # - disk：資料碟副本（既有十步：快照 → clone 單顆資料 VMDK → 熱掛目標 VM）
    # - vmsync：整機同步（快照 → clone 完整 VM → 跨 VC 搬遷 → 目標端換手，
    #           目標端只保留最新一份複本、保持關機作為 standby）
    job_type: Mapped[str] = mapped_column(String(10), default="disk")
    # vmsync：目標端複本 VM 名（留空 = {source_vm}-replica）與網卡要接的目標 portgroup
    replica_name: Mapped[str] = mapped_column(String(120), default="")
    target_network: Mapped[str] = mapped_column(String(120), default="")

    # 跨 VC 傳輸模式：
    # - shared：clone 直接寫入共享 NFS（兩座 VC 同名掛載）
    # - xvc：clone 先落來源側暫存 datastore，再以 Cross-vCenter Relocate
    #        （殼 VM + ServiceLocator）搬到目標側，免共享儲存
    transfer_mode: Mapped[str] = mapped_column(String(10), default="shared")
    staging_datastore: Mapped[str] = mapped_column(String(120), default="")

    # 來源 / 目標 vCenter。shared 模式前提：clone 目的 datastore 為共享 NFS，
    # 同時掛給兩座 VC 的主機且「同名」。
    vcenter_id: Mapped[Optional[int]] = mapped_column(ForeignKey("vcenters.id"), default=None)
    target_vcenter_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vcenters.id"), default=None)
    vcenter: Mapped[Optional["VCenter"]] = relationship(foreign_keys=[vcenter_id])
    target_vcenter: Mapped[Optional["VCenter"]] = relationship(
        foreign_keys=[target_vcenter_id])

    source_vm: Mapped[str] = mapped_column(String(120))          # 例：VM3
    source_data_disk: Mapped[str] = mapped_column(String(200))   # 要 clone 的 SQL 資料 VMDK（disk label 或路徑）
    target_vm: Mapped[str] = mapped_column(String(120))          # 例：VM4
    target_drive_letter: Mapped[str] = mapped_column(String(4), default="E")
    target_datastore: Mapped[str] = mapped_column(String(120), default="")
    databases: Mapped[str] = mapped_column(Text, default="")     # 逗號分隔的 DB 名清單

    # SQL Server 具名執行個體（留空 = 預設執行個體）。例：OFFICESCAN → sqlcmd -S .\OFFICESCAN
    source_sql_instance: Mapped[str] = mapped_column(String(80), default="")
    target_sql_instance: Mapped[str] = mapped_column(String(80), default="")

    # 目前掛在 target 上的 clone，用於下一輪先卸載
    last_clone_path: Mapped[Optional[str]] = mapped_column(String(300), default=None)

    # 多時間點：保留副本總數（含目前掛載中的一份）
    keep_copies: Mapped[int] = mapped_column(Integer, default=1)

    # 排程（時間以「顯示時區」解讀，格式 HH:MM）
    # - daily：每日 schedule_time 執行（catch-up 補跑）
    # - once：schedule_date + schedule_time 執行一次，成功起跑後自動停用排程
    schedule_enabled: Mapped[bool] = mapped_column(default=False)
    schedule_mode: Mapped[str] = mapped_column(String(10), default="daily")  # daily / once
    schedule_time: Mapped[str] = mapped_column(String(5), default="")
    schedule_date: Mapped[str] = mapped_column(String(10), default="")  # once：YYYY-MM-DD

    # 副本驗證/DR 演練排程：定期把「最舊的保留副本」掛上驗證（DBCC +
    # 選填的自訂驗證 SQL）後換回原掛載，結果發送演練報告
    verify_enabled: Mapped[bool] = mapped_column(default=False)
    verify_weekday: Mapped[int] = mapped_column(Integer, default=6)  # 0=一 ... 6=日，7=每日
    verify_time: Mapped[str] = mapped_column(String(5), default="")
    drill_query: Mapped[str] = mapped_column(Text, default="")  # 演練自訂驗證 T-SQL（選填）

    # 副本歸檔：每日成功後把當日副本再複製一份到歸檔 datastore
    archive_enabled: Mapped[bool] = mapped_column(default=False)
    archive_datastore: Mapped[str] = mapped_column(String(120), default="")
    archive_keep: Mapped[int] = mapped_column(Integer, default=7)

    # 任務串接：每日工作流成功後接著執行的任務
    chain_next_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    runs: Mapped[list["Run"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    copies: Mapped[list["CloneCopy"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan",
        order_by="CloneCopy.created_at.desc()",
    )

    @property
    def database_list(self) -> list[str]:
        return [d.strip() for d in self.databases.split(",") if d.strip()]


class Run(Base):
    """一次工作流執行。"""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/success/failed/stopped
    kind: Mapped[str] = mapped_column(String(20), default="daily")      # daily=完整工作流 / mount=掛載歷史副本 / verify=副本驗證
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    error: Mapped[Optional[str]] = mapped_column(Text, default=None)

    profile: Mapped["Profile"] = relationship(back_populates="runs")
    steps: Mapped[list["StepLog"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="StepLog.seq"
    )


class StepLog(Base):
    """工作流中單一步驟的記錄。"""

    __tablename__ = "step_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    seq: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending/running/done/failed/skipped
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), default=None)

    run: Mapped["Run"] = relationship(back_populates="steps")


class CloneCopy(Base):
    """一份 point-in-time 副本（datastore 上的獨立 VMDK）。

    status 生命週期：created（④剛複製出來）→ mounted（⑦掛上目標）
    → kept（下一輪⑥卸下、依保留份數留存）→ 由保留策略刪除（刪列）。
    另有 archived：每日成功後複製到歸檔 datastore 的副本，由 archive_keep 修剪。
    """

    __tablename__ = "clone_copies"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id"))
    run_id: Mapped[Optional[int]] = mapped_column(Integer, default=None)  # 產生它的 run
    path: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="created")  # created/mounted/kept/archived
    # ⑧記錄的各 DB 檔案清單（目標端路徑，JSON：{db: [path, ...]}），掛載歷史副本時免查來源
    files_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    profile: Mapped["Profile"] = relationship(back_populates="copies")


class AuditLog(Base):
    """稽核：誰在何時做了什麼（登入、執行、設定、副本操作）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    user: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(40))    # login / run_start / run_stop / ...
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(45), default="")   # 來源 IP（IPv6 最長 45）


class AccountRole(Base):
    """帳號分權：登入帳號（AD sAMAccountName）→ 角色的對照表。

    LDAP 群組只控制「誰能登入」；登入後的權限由此表決定：
    full_admin（全功能）/ admin_readonly（全頁面唯讀）/ audit（僅稽核相關頁面）。
    未在表中的帳號套用 settings.default_role；內建 admin 恆為 full_admin（防鎖死）。
    """

    __tablename__ = "account_roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True)  # 不分大小寫比對
    role: Mapped[str] = mapped_column(String(20), default="full_admin")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
