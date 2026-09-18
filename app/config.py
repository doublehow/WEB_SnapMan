"""應用設定：優先讀 config.json，其次環境變數 / .env。

- config.json 由 Web 設定頁（/settings）維護，是設定的權威來源。
- 存檔時會「就地更新」記憶體中的 settings 單例，執行中的工作流立即讀到新值。
- config.json 含明碼帳密，已列入 .gitignore，切勿提交。
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
# 首次啟動自動產生的本機 admin 初始密碼（明文、僅擁有者可讀）；改密碼後自動刪除
INITIAL_ADMIN_PW_FILE = DATA_DIR / "initial_admin_password.txt"

# 由 Web 設定頁管理、可持久化到 config.json 的欄位
UI_FIELDS = (
    "vcenter_host",
    "vcenter_user",
    "vcenter_password",
    "vcenter_insecure",
    "guest_user",
    "guest_password",
    "local_admin_password",
    # AD 驗證
    "ad_enabled",
    "ad_domain",
    "ad_servers",
    "ad_service_user",
    "ad_service_password",
    "ad_allowed_group",
    "ad_base_dn",
    "ad_use_ssl",
    "ad_ssl_verify",
    # 帳號分權：未在對照表中的帳號套用的預設角色
    "default_role",
    # 顯示
    "timezone",
    # 告警
    "alert_webhook_url",
    "alert_on_success",
    "smtp_host",
    "smtp_port",
    "smtp_tls",
    "smtp_tls_verify",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_to",
    # 執行紀錄保留
    "run_retention_days",
    # 併發
    "max_concurrent_runs",
)

# 可持久化但不在設定頁顯示的內部欄位
_PERSIST_FIELDS = UI_FIELDS + ("session_secret", "local_admin_initial")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SNAPMAN_",
        env_file=".env",
        env_file_encoding="utf-8",
        json_file=CONFIG_FILE,
        json_file_encoding="utf-8",
        extra="ignore",
    )

    # vCenter
    vcenter_host: str = "vcenter.example.local"
    vcenter_user: str = "administrator@vsphere.local"
    vcenter_password: str = ""
    vcenter_insecure: bool = False   # 略過 vCenter TLS 驗證；預設驗證，自簽環境於設定頁逐座勾選

    # Guest 憑證
    guest_user: str = "Administrator"
    guest_password: str = ""

    # 本機管理員（緊急備援登入）。空值且 AD 未啟用時，啟動自動產生隨機初始密碼
    # （見 auth.bootstrap_local_admin）；以初始或不合規密碼登入會被強制變更。
    local_admin_password: str = ""
    local_admin_initial: bool = False   # True = 目前密碼為系統產生的初始密碼，登入後強制變更

    # AD（NTLM）驗證登入
    ad_enabled: bool = False
    ad_domain: str = ""              # NetBIOS 名，例：CORP
    ad_servers: list[str] = []       # AD 伺服器 IP 清單
    ad_service_user: str = ""        # Service Account（搜尋使用者用）
    ad_service_password: str = ""
    ad_allowed_group: str = ""       # 允許登入的群組
    ad_base_dn: str = ""             # 例：DC=example,DC=com；留空則自動偵測
    ad_use_ssl: bool = False         # LDAPS（636）；預設 389 明文以相容既有環境，建議開啟
    ad_ssl_verify: bool = True       # LDAPS 時驗證 AD 憑證（自簽 CA 未匯入本機信任時可關）

    # 帳號分權：帳號→角色對照表存 DB（account_roles）；
    # 未指派的帳號套用此預設（full_admin 維持升級前「登入即全功能」的行為）
    default_role: str = "admin_readonly"  # full_admin / admin_readonly / audit（未指派帳號預設唯讀）

    # 顯示時區（IANA 名稱，如 Asia/Taipei；留空 = 伺服器系統時區）
    timezone: str = ""

    # 告警（webhook：POST {"text": ...}；SMTP：留空 smtp_host 即停用）
    # alert_subject_tag：主旨標籤（環境變數 SNAPMAN_ALERT_SUBJECT_TAG 或
    # 測試程序直接設定），非空時所有告警主旨/內文加此前綴，如「[測試]」；
    # 不入 UI/config.json，正式環境恆為空
    alert_subject_tag: str = ""
    alert_webhook_url: str = ""
    alert_on_success: bool = False   # False = 只在失敗時通知
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_tls: bool = False
    smtp_tls_verify: bool = True     # STARTTLS 時驗證郵件伺服器憑證（內部自簽可關）
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""                # 逗號分隔收件人

    # 執行紀錄保留天數（0 = 不自動清理）
    run_retention_days: int = 90

    # 全域同時執行上限（多 Profile 併發控制；排程會等待名額後補跑）
    max_concurrent_runs: int = 1

    # Session
    session_secret: str = ""

    # Web 伺服器綁定（可用 SNAPMAN_WEB_HOST / SNAPMAN_WEB_PORT 覆寫）
    web_host: str = "0.0.0.0"
    web_port: int = 8070

    # DB
    database_url: str = f"sqlite:///{(DATA_DIR / 'snapman.db').as_posix()}"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 優先序：初始化參數 > config.json > 環境變數 > .env > secrets
        return (
            init_settings,
            JsonConfigSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


settings = Settings()
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _read_config_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_save_lock = threading.Lock()


def save_settings(updates: dict) -> None:
    """更新記憶體單例並持久化到 config.json（只接受可持久化欄位）。

    - 加鎖：多執行緒（FastAPI threadpool）同時存檔時，read-modify-write
      交錯會互相蓋掉對方的更新。
    - 原子寫入（暫存檔 + os.replace）：直接覆寫時程序中途死掉會留下半截
      JSON，下次讀取靜默退回 {} → 所有設定（含密碼、session secret）蒸發。"""
    with _save_lock:
        data = _read_config_file()
        for key, value in updates.items():
            if key not in _PERSIST_FIELDS:
                continue
            setattr(settings, key, value)
            data[key] = value
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, CONFIG_FILE)


def ensure_session_secret() -> str:
    """回傳 session 簽章密鑰；不存在時自動生成並持久化。"""
    if not settings.session_secret:
        save_settings({"session_secret": secrets.token_hex(32)})
    return settings.session_secret
