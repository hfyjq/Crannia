"""找到「审批式调试端口」并给出地址。

Edge 145+ / Chrome 145+：用户在 `edge://inspect/#remote-debugging` 勾选
「Allow remote debugging for this browser instance」后，浏览器在 **user data
目录**写一个 `DevToolsActivePort`：

    line 1 = 端口（常见 9222，也可能是动态的）
    line 2 = /devtools/browser/<uuid>

文件存在 ≠ 开关开着：浏览器退出后文件会留着。调用方必须真连一次
   才算数；连不上就告诉用户重新勾选（读不到 ≠ 0）。

Chrome 136+ 对默认 profile 忽略 `--remote-debugging-port`；审批式开关
   是 145+ 才有的另一条路。这里不做任何 profile 复制（Windows 上 App-Bound
   Encryption 会让复制的 cookie 罐变空）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PRODUCTS = (
    # (product, user-data 目录, exe 名, 显示名)
    ("edge", Path("Microsoft") / "Edge" / "User Data", "msedge.exe", "Edge"),
    ("edge", Path("Microsoft") / "Edge Beta" / "User Data", "msedge.exe", "Edge Beta"),
    ("edge", Path("Microsoft") / "Edge Dev" / "User Data", "msedge.exe", "Edge Dev"),
    ("edge", Path("Microsoft") / "Edge SxS" / "User Data", "msedge.exe", "Edge Canary"),
    ("chrome", Path("Google") / "Chrome" / "User Data", "chrome.exe", "Chrome"),
    ("chrome", Path("Google") / "Chrome Beta" / "User Data", "chrome.exe", "Chrome Beta"),
    ("chrome", Path("Google") / "Chrome Dev" / "User Data", "chrome.exe", "Chrome Dev"),
    ("chrome", Path("Google") / "Chrome SxS" / "User Data", "chrome.exe", "Chrome Canary"),
)

_MARKER = "DevToolsActivePort"


@dataclass
class Candidate:
    product: str
    exe: str
    user_data: Path
    marker: Path
    port: int
    ws_path: str
    mtime: float
    label: str

    @property
    def ws_url(self) -> str:
        return "ws://127.0.0.1:%d%s" % (self.port, self.ws_path)


def _local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA") or ""
    return Path(raw) if raw else Path.home() / "AppData" / "Local"


def candidates() -> list[Candidate]:
    """按优先级（Edge 稳定版 → Chrome 稳定版 → 各 Beta/Dev）列出候选。"""
    base = _local_appdata()
    out: list[Candidate] = []
    for product, rel, exe, label in PRODUCTS:
        ud = base / rel
        marker = ud / _MARKER
        if not marker.is_file():
            continue
        try:
            lines = marker.read_text(encoding="utf-8").splitlines()
            if len(lines) < 2:
                continue
            port = int(str(lines[0]).strip())
            ws_path = str(lines[1]).strip()
            if not ws_path.startswith("/devtools/"):
                continue
            out.append(Candidate(
                product=product, exe=exe, user_data=ud, marker=marker,
                port=port, ws_path=ws_path,
                mtime=marker.stat().st_mtime, label=label))
        except Exception:                            # noqa: BLE001 - 坏文件跳过
            continue
    return out


def setup_hint() -> str:
    """用户没勾开关时那句能照着做的话（不是"连接失败"）。"""
    return (
        "没有找到浏览器调试端口。请在 Edge/Chrome 里：\n"
        "1) 地址栏打开 `edge://inspect/#remote-debugging`"
        "（Chrome 用 `chrome://inspect/#remote-debugging`）；\n"
        "2) 勾选「Allow remote debugging for this browser instance」；\n"
        "3) 回到这里重试**一次**（连接时浏览器会弹「Allow remote debugging?」，点允许）；\n"
        "   **不要连续重试** —— 还是不行就直接走下面的兜底。\n"
        "★ 这一下「允许」**不算干预**：它发生在进入操作检测之前（先连、后 arm）。\n"
        "⚠️ 不想开这个开关也可以**直接用 `computer` 操作浏览器窗口**"
        "（桌面接管已验证：截图 + 0–1000 比例 + 点击红十字）。")
