"""執行結果告警：webhook（JSON POST）與 SMTP 郵件，設定於設定頁。

同步阻塞實作，呼叫端以 asyncio.to_thread 包裹；任何失敗都吞掉不影響主流程
（告警失敗不該讓工作流跟著失敗），但會回傳訊息字串供事件日誌顯示。
"""
from __future__ import annotations

import ipaddress
import json
import smtplib
import socket
import ssl
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

from .config import settings

# 只掛 HTTP(S) handler：預設 opener 含 file:// / ftp://，webhook URL 若被設成
# file:///... 會變成讀本機檔案的探測管道
_opener = urllib.request.build_opener(urllib.request.HTTPHandler, urllib.request.HTTPSHandler)


def validate_webhook_url(url: str) -> str | None:
    """webhook URL 檢查（設定頁儲存與送出前共用）；合格回 None，否則回錯誤訊息。
    只接受 http/https、主機不得為 loopback / link-local（SnapMan 主機可直連兩側
    ESXi，不能被拿來當內網探測跳板）。"""
    if not url:
        return None
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return "Webhook URL 格式錯誤"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "Webhook URL 只接受 http:// 或 https://，且須含主機名"
    host = u.hostname
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80))}
    except socket.gaierror:
        return f"Webhook 主機「{host}」無法解析"
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return f"Webhook 主機「{host}」解析到不允許的位址（{a}）"
    return None


def notify_run_result(
    profile: str, run_id: int, status: str, error: str | None, duration_s: int
) -> list[str]:
    icon = "✅" if status == "success" else "❌"
    text = (
        f"{icon} SnapMan [{profile}] Run #{run_id}：{status}"
        f"（耗時 {duration_s // 60} 分 {duration_s % 60} 秒）"
    )
    if error:
        text += f"\n錯誤：{error[:500]}"
    return _send(text, f"[SnapMan] {profile} Run #{run_id} {status}")


def send_report(subject: str, text: str) -> list[str]:
    """通用報告訊息（DR 演練報告等），不受 alert_on_success 限制。"""
    return _send(text, subject)


def send_test() -> list[str]:
    """設定頁的測試通知。"""
    if not (settings.alert_webhook_url or (settings.smtp_host and settings.smtp_to)):
        return ["未設定任何告警管道（webhook / SMTP），請先儲存設定"]
    return _send("🔔 SnapMan 告警測試訊息 — 看到這則代表通知管道正常。",
                 "[SnapMan] 告警測試")


def _send(text: str, subject: str) -> list[str]:
    # 主旨標籤（如「[測試]」）：測試程序設定 settings.alert_subject_tag 後，
    # 告警照發但主旨/內文都帶標記，收件人一眼可辨、不會誤判為正式事件
    tag = (settings.alert_subject_tag or "").strip()
    if tag:
        subject = f"{tag} {subject}"
        text = f"{tag} {text}"
    results: list[str] = []
    if settings.alert_webhook_url:
        problem = validate_webhook_url(settings.alert_webhook_url)
        if problem:
            results.append(f"webhook 未送出：{problem}")
        else:
            try:
                req = urllib.request.Request(
                    settings.alert_webhook_url,
                    data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                _opener.open(req, timeout=15)
                results.append("webhook 已送出")
            except Exception as exc:
                # 只回類別（連線拒絕 / 逾時 / HTTP 狀態），不回顯完整例外
                code = getattr(exc, "code", "")
                results.append(f"webhook 失敗：{exc.__class__.__name__}{f' {code}' if code else ''}")
    if settings.smtp_host and settings.smtp_to:
        try:
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = subject
            sender = settings.smtp_from or f"snapman@{settings.smtp_host}"
            tos = [t.strip() for t in settings.smtp_to.split(",") if t.strip()]
            msg["From"] = sender
            msg["To"] = ", ".join(tos)
            s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
            try:
                if settings.smtp_tls:
                    # 未帶 context 時 smtplib 用不驗證憑證的 context（中間人可取 SMTP 帳密）；
                    # 預設驗證，內部自簽可於設定頁關閉 smtp_tls_verify
                    if settings.smtp_tls_verify:
                        ctx = ssl.create_default_context()
                    else:
                        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE
                    s.starttls(context=ctx)
                if settings.smtp_user:
                    s.login(settings.smtp_user, settings.smtp_password)
                s.sendmail(sender, tos, msg.as_string())
            finally:
                s.quit()
            results.append(f"郵件已送出（{', '.join(tos)}）")
        except Exception as exc:
            results.append(f"郵件失敗：{exc}")
    return results
