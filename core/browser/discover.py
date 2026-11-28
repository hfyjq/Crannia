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

#: (product, user-data 目录, exe 名, 显示名, 环境基准)
#: 环境基准 LOCALAPPDATA / APPDATA —— Opera 把 user-data 放在 Roaming，
#: 其余 Chromium 系放 Local。加新浏览器必须先确认它也有「审批式调试开关」
#: （`<scheme>://inspect/#remote-debugging` 里的 "Allow remote debugging"），
#: 否则 `--remote-debugging-port` 在默认 profile 上被忽略（Chrome 136+），
#: 只加路径是接不到真实登录态的。
_PRODUCT_LOCAL = "LOCALAPPDATA"
_PRODUCT_ROAMING = "APPDATA"

PRODUCTS = (
    # (product, user-data 目录, exe 名, 显示名, 环境基准)
    ("edge", Path("Microsoft") / "Edge" / "User Data", "msedge.exe", "Edge", _PRODUCT_LOCAL),
    ("edge", Path("Microsoft") / "Edge Beta" / "User Data", "msedge.exe", "Edge Beta", _PRODUCT_LOCAL),
    ("edge", Path("Microsoft") / "Edge Dev" / "User Data", "msedge.exe", "Edge Dev", _PRODUCT_LOCAL),
    ("edge", Path("Microsoft") / "Edge SxS" / "User Data", "msedge.exe", "Edge Canary", _PRODUCT_LOCAL),
    ("chrome", Path("Google") / "Chrome" / "User Data", "chrome.exe", "Chrome", _PRODUCT_LOCAL),
    ("chrome", Path("Google") / "Chrome Beta" / "User Data", "chrome.exe", "Chrome Beta", _PRODUCT_LOCAL),
    ("chrome", Path("Google") / "Chrome Dev" / "User Data", "chrome.exe", "Chrome Dev", _PRODUCT_LOCAL),
    ("chrome", Path("Google") / "Chrome SxS" / "User Data", "chrome.exe", "Chrome Canary", _PRODUCT_LOCAL),
    # 以下 Chromium 家族：同为 CDP + Accessibility.getFullAXTree，能力面一致。
    ("chromium", Path("Chromium") / "User Data",
     "chrome.exe", "Chromium", _PRODUCT_LOCAL),
    # Opera 在 Roaming，且目录就是 user-data 本身（无 User Data 子层）
    ("opera", Path("Opera Software") / "Opera Stable",
     "opera.exe", "Opera", _PRODUCT_ROAMING),
    ("opera", Path("Opera Software") / "Opera GX Stable",
     "opera.exe", "Opera GX", _PRODUCT_ROAMING),
)

_MARKER = "DevToolsActivePort"

#: 各产品的 inspect 页 —— setup_hint 里逐条列出，别让用户猜 scheme
_INSPECT = {
    "edge": "edge://inspect/#remote-debugging",
    "chrome": "chrome://inspect/#remote-debugging",
    "chromium": "chrome://inspect/#remote-debugging",
    "opera": "opera://inspect/#remote-debugging",
}


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


def _base_for(env_key: str) -> Path:
    raw = os.environ.get(env_key) or ""
    if raw:
        return Path(raw)
    leaf = "Local" if env_key == _PRODUCT_LOCAL else "Roaming"
    return Path.home() / "AppData" / leaf


def _local_appdata() -> Path:
    """兼容旧调用者；新代码用 `_base_for()`。"""
    return _base_for(_PRODUCT_LOCAL)


def candidates() -> list[Candidate]:
    """按 PRODUCTS 声明顺序（Edge → Chrome → 其他 Chromium）列出候选。"""
    out: list[Candidate] = []
    for product, rel, exe, label, env_key in PRODUCTS:
        ud = _base_for(env_key) / rel
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
    pages = " / ".join(
        "`%s`" % _INSPECT[p] for p in ("edge", "chrome", "opera"))
    return (
        "没有找到浏览器调试端口。在你用的 Chromium 系浏览器里：\n"
        "1) 地址栏打开对应检查页：" + pages + "；\n"
        "2) 勾选「Allow remote debugging for this browser instance」；\n"
        "3) 回到这里重试**一次**（连接时浏览器会弹「Allow remote debugging?」，点允许）；\n"
        "   **不要连续重试** —— 还是不行就直接走下面的兜底。\n"
        "★ 这一下「允许」**不算干预**：它发生在进入操作检测之前（先连、后 arm）。\n"
        "⚠️ 浏览器需 145+（审批式开关）；Chrome 136+ 会忽略默认 profile 上的"
        " `--remote-debugging-port`，所以别指望命令行参数，要走上面的开关。\n"
        "⚠️ 不想开这个开关也可以**直接用 `computer` 操作浏览器窗口**"
        "（桌面接管已验证：截图 + 0–1000 比例 + 点击红十字）。"
        " Firefox 不走 CDP，只能用这条兜底。")
