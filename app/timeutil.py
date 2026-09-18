"""顯示時間工具：依設定頁的「顯示時區」格式化。

- DB 一律存 UTC；顯示時轉換到 settings.timezone（IANA 名稱，如 Asia/Taipei）。
- 未設定或名稱無效時，退回伺服器系統時區。
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .config import settings


def display_tz() -> ZoneInfo | None:
    """設定的顯示時區；None 代表伺服器系統時區（astimezone(None) 的語意）。"""
    if settings.timezone:
        try:
            return ZoneInfo(settings.timezone)
        except Exception:
            pass
    return None


def to_display(ts: dt.datetime | None, fmt: str = "%H:%M:%S") -> str:
    """DB 內的 UTC 時間 → 顯示時區字串（SQLite 讀回為 naive，一律視為 UTC）。"""
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(display_tz()).strftime(fmt)


def now_hms() -> str:
    """現在時刻（顯示時區）HH:MM:SS，供 WebSocket 即時事件時戳。"""
    return dt.datetime.now(dt.timezone.utc).astimezone(display_tz()).strftime("%H:%M:%S")
