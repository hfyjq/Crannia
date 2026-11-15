"""
Mail parser skill — IMAP inbox + SMTP send + email summarization.

Uses only Python stdlib (imaplib, smtplib, email) — zero extra dependencies.

Hard constraints:
  1. Streaming: server-side search via IMAP criteria, not client-side filtering.
  2. Security: all connections use SSL/TLS. Passwords never logged.
  3. Chunking: email bodies truncated at 2000 chars before returning to LLM context.
  4. Credentials: ephemeral — never stored to disk or logged. The LLM must
     provide them per-call (the user shares them in conversation).
     ✅ 这条必须真的成立（不只是文档）：凭据若经 `tool_calls` 进 `messages`
     就会落进 `short_term.db` 的 `extra_json`、进 `logs/audit.log`、并每一轮
     随历史上 wire。现在在凭据进入历史的那一刻就换掉
     （`agent_loop._redact_call_for_history`），
     审计那条独立的路另打一次（`SecurityGuard.redact_credential_args`）。
     按参数名打码，不按内容模式 —— 授权码没有形状，模式认不出它。

Supported email providers:
  QQ邮箱:  imap.qq.com:993 / smtp.qq.com:465
  163邮箱: imap.163.com:993 / smtp.163.com:465
  Gmail:   imap.gmail.com:993 / smtp.gmail.com:587 (STARTTLS)
  Outlook: outlook.office365.com:993 / smtp.office365.com:587 (STARTTLS)
"""

from __future__ import annotations

import email
import imaplib
import imaplib
import smtplib
import ssl
import time
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from email.utils import formatdate, parseaddr
from pathlib import Path
from typing import Any, List, Optional

from core.security import SecurityGuard
from core.workspace import find_in_roots, lookup_roots

# MIME type mapping by extension
_MIME_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".wav": "audio/wav",
    ".mov": "video/quicktime",
    ".zip": "application/zip",
    ".rar": "application/x-rar-compressed",
    ".7z": "application/x-7z-compressed",
    ".py": "text/x-python",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
}

# ------------------------------------------------------------------
# Injected by skill_loader at startup
# ------------------------------------------------------------------

_security: SecurityGuard | None = None


def init(security: SecurityGuard) -> None:
    """Called by skill_loader after module import."""
    global _security
    _security = security


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

# Max body length returned to LLM context (prevent context pollution)
_MAX_BODY_CHARS = 2000
_MAX_PREVIEW_CHARS = 300

# Common IMAP folder names (for display)
_FOLDER_LABELS: dict[str, str] = {
    "INBOX": "收件箱",
    "Sent": "已发送",
    "Drafts": "草稿箱",
    "Trash": "已删除",
    "Junk": "垃圾邮件",
    "Spam": "垃圾邮件",
    "Archive": "归档",
}


# Tool: list_folders

def list_folders(
    imap_server: str,
    email_address: str,
    password: str,
    port: int = 993,
) -> dict:
    """
    List all IMAP folders with status indicators.

    Returns:
        dict with folders list (name, flags, label)
    """
    _require_security()

    try:
        conn = _imap_connect(imap_server, port, email_address, password)
    except Exception as e:
        return {"success": False, "error": f"IMAP 连接失败: {e}"}

    try:
        folders: list[dict] = []
        status, raw = conn.list()
        if status != "OK":
            return {"success": False, "error": f"列出文件夹失败: {status}"}

        for line in raw:
            if isinstance(line, bytes):
                line_str = line.decode("utf-8", errors="replace")
                # Parse: (\\HasNoChildren) "/" "INBOX"
                parts = line_str.split(' "')
                flags = parts[0].strip('() ') if parts else ""
                folder_name = parts[-1].strip('"') if len(parts) >= 2 else line_str

                # Decode IMAP modified UTF-7 folder names (QQ邮箱 uses this)
                decoded_name = _decode_imap_utf7(folder_name)
                label = _FOLDER_LABELS.get(folder_name, "") or _FOLDER_LABELS.get(decoded_name, "")
                folders.append({
                    "name": decoded_name,
                    "raw_name": folder_name if folder_name != decoded_name else None,
                    "flags": flags,
                    "label": label,
                })

        return {
            "success": True,
            "count": len(folders),
            "folders": folders,
        }
    finally:
        _imap_disconnect(conn)


# Tool: search_emails

def search_emails(
    imap_server: str,
    email_address: str,
    password: str,
    folder: str = "INBOX",
    query: str = "",
    limit: int = 10,
    unread_only: bool = False,
    port: int = 993,
) -> dict:
    """
    Search emails by IMAP criteria. Returns summaries (no full body).

    Args:
        query: IMAP search criteria (e.g. 'FROM "boss" SUBJECT "urgent"').
               Leave empty for recent emails.
    """
    _require_security()

    limit = min(max(limit, 1), 50)

    try:
        conn = _imap_connect(imap_server, port, email_address, password)
    except Exception as e:
        return {"success": False, "error": f"IMAP 连接失败: {e}"}

    try:
        # Select folder
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return {"success": False, "error": f"无法打开文件夹: {folder}"}

        # Build search criteria
        criteria_parts = []
        if unread_only:
            criteria_parts.append("UNSEEN")
        if query.strip():
            criteria_parts.append(f"({query})" if " " in query else query)
        if not criteria_parts:
            criteria_parts.append("ALL")

        search_criteria = " ".join(criteria_parts)

        # Search
        status, uids = conn.uid("SEARCH", None, search_criteria)
        if status != "OK":
            return {"success": False, "error": f"搜索失败: {status}"}

        uid_list = uids[0].split() if uids[0] else []
        if not uid_list:
            return {
                "success": True,
                "count": 0,
                "criteria": search_criteria,
                "folder": folder,
                "emails": [],
                "message": "没有找到匹配的邮件",
            }

        # Fetch latest N (IMAP returns oldest first)
        uid_list = uid_list[-limit:]

        # Batch fetch: envelope + body structure (no full body)
        results: list[dict] = []
        for uid_bytes in uid_list:
            uid = uid_bytes.decode("utf-8") if isinstance(uid_bytes, bytes) else str(uid_bytes)
            info = _fetch_email_summary(conn, uid)
            if info:
                results.append(info)

        # Reverse so newest first
        results.reverse()

        return {
            "success": True,
            "count": len(results),
            "folder": folder,
            "criteria": search_criteria,
            "emails": results,
            "hint": "使用 get_email 获取邮件完整正文（需提供 uid）",
        }
    finally:
        _imap_disconnect(conn)


# Tool: get_email

def get_email(
    imap_server: str,
    email_address: str,
    password: str,
    uid: str,
    folder: str = "INBOX",
    port: int = 993,
) -> dict:
    """
    Get full content of a specific email by UID.
    """
    _require_security()

    try:
        conn = _imap_connect(imap_server, port, email_address, password)
    except Exception as e:
        return {"success": False, "error": f"IMAP 连接失败: {e}"}

    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return {"success": False, "error": f"无法打开文件夹: {folder}"}

        # Fetch full RFC822 message
        status, data = conn.uid("FETCH", uid, "(RFC822 FLAGS)")
        if status != "OK" or not data or not data[0]:
            return {"success": False, "error": f"邮件 {uid} 不存在或无法获取"}

        # Parse raw email
        raw_email = None
        for item in data:
            if isinstance(item, tuple):
                raw_email = item[1]
                break

        if not raw_email:
            return {"success": False, "error": "无法解析邮件数据"}

        msg = email.message_from_bytes(raw_email)

        # Extract headers
        subject = _decode_header_value(msg["Subject"]) or "(无主题)"
        from_header = _decode_header_value(msg["From"]) or ""
        to_header = _decode_header_value(msg["To"]) or ""
        date_header = _decode_header_value(msg["Date"]) or ""
        cc_header = _decode_header_value(msg["Cc"]) or ""

        # Extract body
        text_body, html_body = _extract_body(msg)

        # Extract attachments info
        attachments = _list_attachments(msg)

        return {
            "success": True,
            "uid": uid,
            "folder": folder,
            "subject": subject,
            "from": from_header,
            "to": to_header,
            "cc": cc_header,
            "date": date_header,
            "body_text": text_body[:_MAX_BODY_CHARS] if text_body else "",
            "body_html": html_body[:_MAX_BODY_CHARS] if html_body else "",
            "attachments": attachments,
            "has_attachments": len(attachments) > 0,
        }
    finally:
        _imap_disconnect(conn)


# Tool: send_email

def send_email(
    smtp_server: str = "",
    email_address: str = "",
    password: str = "",
    to: str = "",
    subject: str = "",
    body: str = "",
    cc: str = "",
    bcc: str = "",
    port: int = 0,
    use_tls: bool = True,
    attachments: Optional[List[str]] = None,
) -> dict:
    """
    Send an email via SMTP, optionally with file attachments.

    Supports both SSL (port 465) and STARTTLS (port 587) modes.

    凭据优先取设置页配好的那一份。

    | 参数 | 不给会怎样 |
    |---|---|
    | `smtp_server` / `email_address` / `password` | 去 `core/mail_config` 读设置页配好的当前生效账号；两者都没有，回一句「请先去设置页配」， 不向用户索要 |
    | `port` | `0` = 用配好的（QQ 465 / Gmail 587）；兜底 465 |

    显式传进来的仍然赢 —— 所以"用户自己把凭据打过来"那条路照样成立。
    读配置只走 `core/mail_config` 那一个入口，别在这里内联
    `settings.get("skill_configs")`（两份读取会各自漂移）。
    """
    _require_security()

    # ⓪ 金丝雀（`ICE` 网络侧）—— 排在发信之前
    #
    # 盯的是出站正文：诱饵唯一串只要出现在收件人 / 主题 / 正文里就阻断。
    # 判据与 `web_fetch` 那条同源（`core/ice/canary.py` 的模块 docstring）。
    # 附件不在扫描范围（明确缺口）：
    #    诱饵被当作附件发出去时这条闸不响。
    from core.ice.canary import check_outbound
    _canary = check_outbound("\n".join([to or "", cc or "", bcc or "",
                                        subject or "", body or ""]),
                             where="send_email")
    if _canary is not None:
        return _canary
    # 弱信号：收件人是域名，正是这套启发式的用武之地
    #    （钓鱼邮件最常见的形态就是"发去一个看着像随机串的域名"）。
    #    只报不拦，而且附件仍然不扫（同金丝雀那条缺口）。
    from core.ice.net_heuristics import note_signals
    for _addr in (to, cc, bcc):
        for _a in str(_addr or "").split(","):
            _a = _a.strip()
            if "@" in _a:
                note_signals(_a.split("@", 1)[1], where="send_email")

    # 凭据：显式给的赢，没给的取设置页那一份
    if not (smtp_server and email_address and password):
        try:
            from core import mail_config as _mc
            _cfg = _mc.resolve() or {}
        except Exception as _exc:  # noqa: BLE001
            print(f"[mail] 读邮箱配置失败：{type(_exc).__name__}: {_exc}")
            _cfg = {}
        smtp_server = smtp_server or str(_cfg.get("smtp_server") or "")
        email_address = email_address or str(_cfg.get("email_address") or "")
        password = password or str(_cfg.get("password") or "")
        if not port:
            port = int(_cfg.get("smtp_port") or 0)
    if not port:
        port = 465

    # 缺件就出声，别回头去问用户要授权码 —— 那条路的终点是
    #   "授权码进上下文"（`short_term.db` / 压缩摘要 / 后台抽取器都读得到）。
    if not (smtp_server and email_address and password):
        return {
            "success": False,
            "error": ("邮箱尚未配置（或配置不完整）—— 请先到**设置页 → 邮件配置**"
                      "把发件邮箱配好，保存时它会自动发一封测试邮件验证。"
                      "★ 不要去问用户要授权码：让他自己在设置页填。"),
        }
    if not (to and subject and body):
        return {"success": False,
                "error": "缺 to / subject / body —— 收件人、主题、正文都得给。"}

    # Build message
    msg = MIMEMultipart()
    msg["From"] = email_address
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)

    recipients = [a.strip() for a in to.split(",") if a.strip()]

    if cc:
        msg["Cc"] = cc
        recipients += [a.strip() for a in cc.split(",") if a.strip()]
    if bcc:
        # BCC not in headers (privacy)
        recipients += [a.strip() for a in bcc.split(",") if a.strip()]

    # Determine content type
    if "<html" in body.lower() or "<body" in body.lower() or "<p" in body.lower():
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    # Attach files
    attached = 0
    if attachments:
        for file_path in attachments:
            p = Path(file_path)
            if not p.exists():
                # Try under workspaces
                # 「查」—— 附件可能在任何一个工作组里
                p = find_in_roots(p.name) or (lookup_roots()[0] / p.name)
            if not p.exists():
                continue
            ext = p.suffix.lower()
            mime_type = _MIME_TYPES.get(ext, "application/octet-stream")
            main, sub = mime_type.split("/", 1)
            with open(p, "rb") as f:
                part = MIMEBase(main, sub)
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment")
            part.set_param("filename", p.name, header="Content-Disposition")
            msg.attach(part)
            attached += 1

    # Send
    try:
        if port == 587 or (use_tls and port != 465):
            # STARTTLS mode (Gmail, Outlook)
            ctx = ssl.create_default_context()
            with smtplib.SMTP(smtp_server, port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(email_address, password)
                server.sendmail(email_address, recipients, msg.as_string())
        else:
            # SSL mode (QQ, 163)
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_server, port, context=ctx, timeout=30) as server:
                server.login(email_address, password)
                server.sendmail(email_address, recipients, msg.as_string())

        return {
            "success": True,
            "message": f"邮件已发送",
            "to": to,
            "subject": subject,
            "cc": cc if cc else None,
            "bcc": bcc if bcc else None,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except smtplib.SMTPAuthenticationError:
        return {"success": False, "error": "SMTP 认证失败，请检查邮箱地址和授权码是否正确"}
    except smtplib.SMTPConnectError:
        return {"success": False, "error": f"无法连接到 SMTP 服务器 {smtp_server}:{port}，请检查服务器地址和端口"}
    except Exception as e:
        return {"success": False, "error": f"发送失败: {type(e).__name__}: {e}"}


# Tool: summarize_emails

def summarize_emails(
    imap_server: str,
    email_address: str,
    password: str,
    folder: str = "INBOX",
    limit: int = 5,
    unread_only: bool = True,
    port: int = 993,
) -> dict:
    """
    Get an AI-friendly summary list of recent emails.

    Returns sender, subject, date, and body preview (first 300 chars).
    Designed as the first tool to call for a quick inbox overview.
    """
    _require_security()

    limit = min(max(limit, 1), 30)

    try:
        conn = _imap_connect(imap_server, port, email_address, password)
    except Exception as e:
        return {"success": False, "error": f"IMAP 连接失败: {e}"}

    try:
        status, _ = conn.select(folder, readonly=True)
        if status != "OK":
            return {"success": False, "error": f"无法打开文件夹: {folder}"}

        # Search unread or all
        criteria = "UNSEEN" if unread_only else "ALL"
        status, uids = conn.uid("SEARCH", None, criteria)
        if status != "OK":
            return {"success": False, "error": f"搜索失败: {status}"}

        uid_list = uids[0].split() if uids[0] else []
        if not uid_list:
            return {
                "success": True,
                "count": 0,
                "folder": folder,
                "unread_only": unread_only,
                "emails": [],
                "message": "没有未读邮件" if unread_only else "文件夹为空",
            }

        # Get latest N
        uid_list = uid_list[-limit:]

        summaries: list[dict] = []
        for uid_bytes in uid_list:
            uid = uid_bytes.decode("utf-8") if isinstance(uid_bytes, bytes) else str(uid_bytes)
            info = _fetch_email_summary(conn, uid)
            if info:
                summaries.append(info)

        summaries.reverse()

        total_msg = conn.status(folder, "(MESSAGES UNSEEN)")
        total_count = 0
        unseen_count = 0
        if total_msg[0] == "OK":
            # Parse: folder (MESSAGES N UNSEEN M)
            status_str = total_msg[1][0].decode("utf-8", errors="replace")
            import re
            m = re.search(r"MESSAGES\s+(\d+)", status_str)
            if m:
                total_count = int(m.group(1))
            m = re.search(r"UNSEEN\s+(\d+)", status_str)
            if m:
                unseen_count = int(m.group(1))

        return {
            "success": True,
            "count": len(summaries),
            "folder": folder,
            "total_messages": total_count,
            "unread_count": unseen_count,
            "unread_only": unread_only,
            "emails": summaries,
        }
    finally:
        _imap_disconnect(conn)


# IMAP helpers

def _imap_connect(server: str, port: int, username: str, password: str) -> imaplib.IMAP4_SSL:
    """Connect and login to IMAP server via SSL."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    # Allow legacy renegotiation for some servers
    ctx.verify_mode = ssl.CERT_REQUIRED

    conn = imaplib.IMAP4_SSL(server, port, ssl_context=ctx, timeout=30)
    try:
        conn.login(username, password)
    except imaplib.IMAP4.error as e:
        conn.logout()
        raise ConnectionError(f"IMAP 登录失败: {e}") from e
    return conn


def _imap_disconnect(conn: imaplib.IMAP4_SSL) -> None:
    """Safe disconnect from IMAP server."""
    try:
        conn.close()
        conn.logout()
    except Exception:
        pass


def _fetch_email_summary(conn: imaplib.IMAP4_SSL, uid: str) -> dict | None:
    """
    Fetch email envelope info + first body chunk for preview.

    Uses IMAP FETCH with BODY.PEEK to avoid marking as read.
    Returns a summary dict or None on failure.
    """
    try:
        # Fetch envelope (headers) + body preview in one call
        # BODY.PEEK[1] gets first body part without setting \Seen flag
        status, data = conn.uid(
            "FETCH", uid,
            "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)] BODY.PEEK[TEXT]<0.1024>)"
        )
        if status != "OK" or not data:
            return None

        # Parse the FETCH response
        headers: dict[str, str] = {}
        body_preview = ""

        for item in data:
            if not isinstance(item, tuple):
                continue

            # item[0] is the fetch marker, item[1] is the data
            raw = item[1] if len(item) > 1 else b""
            if not raw:
                continue

            msg = email.message_from_bytes(raw)

            # Headers
            for hdr in ("From", "To", "Subject", "Date"):
                val = _decode_header_value(msg[hdr])
                if val:
                    headers[hdr.lower()] = val

            # Body preview — iterate over payload parts
            body_chunks: list[str] = []
            if msg.is_multipart():
                for part in msg.walk():
                    ctype = part.get_content_type()
                    if ctype == "text/plain" and not part.get("Content-Disposition"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_chunks.append(payload.decode("utf-8", errors="replace"))
            else:
                ctype = msg.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_chunks.append(payload.decode("utf-8", errors="replace"))

            body_preview = "\n".join(body_chunks)[:_MAX_PREVIEW_CHARS]

        # Check if unread
        # The FLAGS response is in the first item
        flags_str = ""
        if data and data[0] and isinstance(data[0], tuple):
            flags_raw = data[0][0]
            if isinstance(flags_raw, bytes):
                flags_str = flags_raw.decode("utf-8", errors="replace")

        is_unread = "\\Seen" not in flags_str

        return {
            "uid": uid,
            "from": headers.get("from", "(未知)"),
            "to": headers.get("to", ""),
            "subject": headers.get("subject", "(无主题)"),
            "date": headers.get("date", ""),
            "preview": body_preview[:200],
            "unread": is_unread,
        }
    except Exception:
        return None


def _extract_body(msg: email.message.Message) -> tuple[str, str]:
    """
    Extract plain text and HTML body from an email message.

    Returns (text_body, html_body) — both strings, may be empty.
    """
    text_parts: list[str] = []
    html_parts: list[str] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = part.get("Content-Disposition", "")
            # Skip attachments
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            try:
                decoded = payload.decode("utf-8", errors="replace")
            except Exception:
                decoded = payload.decode("latin-1", errors="replace")

            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            try:
                decoded = payload.decode("utf-8", errors="replace")
            except Exception:
                decoded = payload.decode("latin-1", errors="replace")
            if ctype == "text/plain":
                text_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)

    return "\n".join(text_parts), "\n".join(html_parts)


def _list_attachments(msg: email.message.Message) -> list[dict]:
    """List attachment metadata from an email message."""
    attachments: list[dict] = []
    for part in msg.walk():
        disp = part.get("Content-Disposition", "")
        if "attachment" in disp or (part.get_content_maintype() not in ("text", "multipart") and part.get_filename()):
            filename = part.get_filename()
            if filename:
                filename = _decode_header_value(filename)
                attachments.append({
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "size": len(part.get_payload(decode=True) or b""),
                })
    return attachments


def _decode_imap_utf7(name: str) -> str:
    """Decode IMAP modified UTF-7 encoded folder name (RFC 3501)."""
    if not name or "&" not in name:
        return name
    try:
        import base64
        result: list[str] = []
        i = 0
        while i < len(name):
            if name[i] == "&":
                end = name.find("-", i)
                if end == -1:
                    result.append(name[i:])
                    break
                encoded = name[i + 1 : end]
                if not encoded:
                    # && is literal &
                    result.append("&")
                else:
                    try:
                        b64 = encoded.replace(",", "/")
                        b64 += "=" * ((4 - len(b64) % 4) % 4)
                        raw = base64.b64decode(b64)
                        result.append(raw.decode("utf-16-be"))
                    except Exception:
                        result.append(name[i : end + 1])
                i = end + 1
            else:
                result.append(name[i])
                i += 1
        return "".join(result)
    except Exception:
        return name


def _decode_header_value(value: str | None) -> str:
    """Decode RFC 2047 encoded email header (e.g. =?UTF-8?B?...?=)."""
    if not value:
        return ""
    parts = decode_header(value)
    result: list[str] = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                result.append(text.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(text.decode("utf-8", errors="replace"))
        else:
            result.append(str(text))
    return "".join(result)


# Internal helpers

def _require_security() -> None:
    """Raise RuntimeError if SecurityGuard hasn't been injected yet."""
    if _security is None:
        raise RuntimeError(
            "SecurityGuard not initialized. "
            "The skill_loader must call init() before any handler is invoked."
        )
