"""
WeChat iLink authentication — QR code login + credential cache.

Login flow:
  1. GET  /ilink/bot/get_bot_qrcode?bot_type=3  → {qrcode, qrcode_img_content}
  2. Display QR code in terminal
  3. GET  /ilink/bot/get_qrcode_status?qrcode=xxx  (long poll 40s)
     status: wait → scaned → confirmed → {bot_token, ilink_bot_id, ilink_user_id}
  4. Persist credentials to disk (0o600) for reuse on restart

Credentials format::

    {
      "bot_token": "...",
      "ilink_bot_id": "...",
      "ilink_user_id": "xxx@im.wechat",
      "base_url": "https://ilinkai.weixin.qq.com"
    }
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

#: 出网走一处口径（`core/net_proxy`）。登录这条路上的两次 GET
#:   原来用的是裸 `httpx.get`，`trust_env=True` 会静默采纳环境里的
#:   `ALL_PROXY`/`HTTPS_PROXY`（或用户设置的死代理），扫码那一步直接
#:   `WinError 10061 目标计算机积极拒绝`，界面上却只看到"唤起中"。
#:   通道主体（`handler.run`）早就走这条口径了，登录这条路漏了。
from core import net_proxy as _np

# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

BASE_URL = "https://ilinkai.weixin.qq.com"
QR_POLL_TIMEOUT = 40  # seconds
MAX_QR_RETRIES = 3

# ------------------------------------------------------------------
# Web-mode login state (shared between channel thread and web server)
# ------------------------------------------------------------------

class WebLoginState:
    """Thread-safe state for Web UI QR code login flow.

    The WeChat channel writes to this from its background thread;
    the web server reads from it via API endpoints.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.phase = "idle"       # idle | starting | qr_ready | scanned | confirmed | expired | error
            self.qrcode_key = ""
            self.img_url = ""         # raw URL from iLink (data to encode, NOT an image)
            self.qr_data_uri = ""     # generated QR code PNG as base64 data URI
            self.bot_id = ""
            self.error_msg = ""

    def set_phase(self, phase: str, **kwargs) -> None:
        with self._lock:
            self.phase = phase
            for k, v in kwargs.items():
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return {
                "phase": self.phase,
                "qrcode_key": self.qrcode_key,
                "img_url": self.img_url,
                "qr_data_uri": self.qr_data_uri,
                "bot_id": self.bot_id,
                "error_msg": self.error_msg,
            }


# Singleton — one login flow at a time
_web_login = WebLoginState()


def get_web_login_state() -> WebLoginState:
    return _web_login


def reset_web_login_state() -> None:
    _web_login.reset()


# ------------------------------------------------------------------
# Credential persistence
# ------------------------------------------------------------------

def load_credentials(path: str | Path) -> dict[str, Any] | None:
    """Load cached credentials from disk.  Returns None if absent or corrupt."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "bot_token" not in data:
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def save_credentials(path: str | Path, creds: dict[str, Any]) -> None:
    """Persist credentials to disk with restricted permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(creds, ensure_ascii=False, indent=2), encoding="utf-8")
    # Restrict to owner-only on POSIX; on Windows this is a no-op
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------
# QR code login
# ------------------------------------------------------------------

def fetch_qrcode(base_url: str = BASE_URL) -> dict[str, str]:
    """Get a fresh QR code from the server.

    Returns {"qrcode": "<key>", "qrcode_img_content": "<url>"}.
    """
    resp = httpx.get(f"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3",
                     **_np.httpx_kwargs())
    resp.raise_for_status()
    data = resp.json()
    return {"qrcode": data["qrcode"], "qrcode_img_content": data.get("qrcode_img_content", "")}


def poll_qrcode_status(
    qrcode_key: str,
    base_url: str = BASE_URL,
    timeout: int = QR_POLL_TIMEOUT,
) -> dict[str, Any]:
    """Long-poll the QR code status until the user acts or it expires.

    Returns the full status dict.  Caller should inspect `status["status"]`:
    `"wait"` | `"scaned"` | `"confirmed"` | `"expired"`.
    """
    resp = httpx.get(
        f"{base_url}/ilink/bot/get_qrcode_status?qrcode={qrcode_key}",
        headers={"iLink-App-ClientVersion": "1"},
        timeout=timeout,
        **_np.httpx_kwargs(),
    )
    resp.raise_for_status()
    return resp.json()


def display_qrcode_terminal(qrcode_url: str) -> None:
    """Render a QR code URL as ASCII art in the terminal."""
    try:
        import qrcode as qr_module
        qr = qr_module.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(f"[wechat] QR code URL: {qrcode_url}")
        print("[wechat] (install 'qrcode' package for terminal QR display)")


def do_login(
    base_url: str = BASE_URL,
    credential_path: str | Path = "data/wechat_credentials.json",
    web_callback: Callable[[str, dict], None] | None = None,
) -> dict[str, Any]:
    """Full QR code login flow.  Blocks until user confirms on phone.

    Args:
        base_url: iLink API base URL.
        credential_path: Where to persist credentials.
        web_callback: If provided, called on state changes instead of
            printing to terminal.  Signature: cb(phase, data) where
            phase is one of `qr_ready`, `scanned`, `confirmed`,
            `expired`, `error`, and *data* is a dict with phase-specific
            fields (`qrcode_key`, `img_url`, `bot_id`, `error`).

    Returns the credentials dict ready for persistence and use.
    May retry up to *max_retries* times if the QR code expires.
    """
    def _notify(phase: str, **kwargs) -> None:
        if web_callback:
            try:
                web_callback(phase, kwargs)
            except Exception:
                pass  # Never let a callback failure kill the login flow

    for attempt in range(1, MAX_QR_RETRIES + 1):
        if not web_callback:
            print(f"[wechat] Fetching QR code (attempt {attempt}/{MAX_QR_RETRIES})...")
        qr = fetch_qrcode(base_url)
        qrcode_key = qr["qrcode"]
        img_url = qr.get("qrcode_img_content", "")

        if web_callback:
            _notify("qr_ready", qrcode_key=qrcode_key, img_url=img_url)
        else:
            display_qrcode_terminal(img_url)
            print("[wechat] Scan the QR code with WeChat to log in.")

        while True:
            try:
                status = poll_qrcode_status(qrcode_key, base_url)
            except httpx.HTTPError as e:
                msg = str(e)
                if not web_callback:
                    print(f"[wechat] QR poll error: {e}")
                _notify("error", error=msg)
                break

            state = status.get("status", "")
            if state == "wait":
                continue  # keep polling
            elif state == "scaned":
                if web_callback:
                    _notify("scanned")
                else:
                    print("[wechat] Scanned! Please confirm on your phone...")
            elif state == "confirmed":
                token = status.get("bot_token", "")
                if not token:
                    _notify("error", error="Login confirmed but no bot_token returned")
                    raise RuntimeError("Login confirmed but no bot_token returned")
                creds = {
                    "bot_token": token,
                    "ilink_bot_id": status.get("ilink_bot_id", ""),
                    "ilink_user_id": status.get("ilink_user_id", ""),
                    "base_url": status.get("baseurl") or base_url,
                }
                save_credentials(credential_path, creds)
                bot_id_short = creds["ilink_bot_id"][:12] if creds["ilink_bot_id"] else "?"
                if web_callback:
                    _notify("confirmed", bot_id=creds.get("ilink_bot_id", ""))
                else:
                    print(f"[wechat] Login successful! bot_id={bot_id_short}...")
                return creds
            elif state == "expired":
                if web_callback:
                    _notify("expired")
                else:
                    print("[wechat] QR code expired, regenerating...")
                break
            else:
                if not web_callback:
                    print(f"[wechat] Unknown QR status: {state}")
                time.sleep(1)

    msg = (
        f"Login failed after {MAX_QR_RETRIES} QR code attempts. "
        "Please check your network and try again."
    )
    _notify("error", error=msg)
    raise RuntimeError(msg)
