"""剪贴板里的图：取一张，转成 data URL。

为什么要有子进程：终端应用拿不到剪贴板的位图 —— 剪贴板是操作系统那一层的，
不是终端的。

- Windows：`Get-Clipboard -Format Image`。系统自带，不用装依赖；
  一次冷启动约 0.4 秒，所以只在按键时才调，不轮询。
- Linux：Wayland 用 `wl-paste`，X11 用 `xclip`。两个都不预装，所以
  找不到时必须说清装哪个包 —— 只说"读不到"的话，用户会去查权限。

两条路都只认 `image/png`：与 Windows 那条（`ImageFormat::Png`）保持同一种载荷，
上层不必按平台分叉。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys

from .i18n import t

#: 取图并转 base64 的脚本（Windows）。一次调用做完全部转换 —— 不落临时文件：
#: 落盘要么留在用户目录里，要么还得负责删，两个都是尾巴。
_SCRIPT = (
    "$i = Get-Clipboard -Format Image; "
    "if ($null -eq $i) { exit 3 }; "
    "$ms = New-Object IO.MemoryStream; "
    "$i.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); "
    "[Convert]::ToBase64String($ms.ToArray())"
)

#: 脚本用这个退出码说"剪贴板里没有图"。它不是错误。
_NO_IMAGE = 3

#: Linux 工具在"剪贴板里没有图"时的说法（大小写不敏感）。这也不算失败：
#: 用户按了贴图键、而剪贴板里是文字，与"工具坏了"是两件事。
_NO_IMAGE_HINTS = ("no suitable type", "not available",
                   "nothing is copied", "no selection owner")


class ClipboardError(RuntimeError):
    """读剪贴板失败：没有工具、超时、或者工具自己报错。"""


def _win_read() -> str | None:
    """Windows：PowerShell 取图。

    `-Sta` 不能省：.NET 的 `Clipboard` 要求 STA 线程，MTA 下直接抛异常。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Sta",
             "-Command", _SCRIPT],
            capture_output=True, timeout=20, check=False, creationflags=flags,
        )
    except FileNotFoundError as exc:
        raise ClipboardError(t("未找到 powershell")) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(t("剪贴板读取超时（20 秒）")) from exc

    if proc.returncode == _NO_IMAGE:
        return None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "ignore").strip()
        raise ClipboardError(stderr.splitlines()[-1] if stderr
                             else t("powershell 退出码 %s")
                             % proc.returncode)

    b64 = proc.stdout.decode("ascii", "ignore").strip()
    return f"data:image/png;base64,{b64}" if b64 else None


def _linux_cmd() -> list[str] | None:
    """按会话类型挑一个能用的工具；都没有返回 None。

    `--no-newline` 不能省：`wl-paste` 默认在输出末尾补一个换行，而这里拿的是
    PNG 二进制 —— 多一个字节就少一截 IEND，base64 之后是一张打不开的图。
    """
    wayland = ["wl-paste", "--no-newline", "--type", "image/png"]
    x11 = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return wayland
    if os.environ.get("DISPLAY") and shutil.which("xclip"):
        return x11
    # 会话变量没有、工具却在（比如剪贴板经由 XWayland）：两种都试一次，
    # 顺序只是偏好，不代表会话类型。
    if shutil.which("wl-paste"):
        return wayland
    if shutil.which("xclip"):
        return x11
    return None


def _linux_read() -> str | None:
    cmd = _linux_cmd()
    if cmd is None:
        raise ClipboardError(
            t("没装剪贴板工具：Wayland 装 wl-clipboard，X11 装 xclip"))
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20, check=False)
    except FileNotFoundError as exc:                 # which 与 exec 之间的窗口
        raise ClipboardError(t("未找到 %s") % cmd[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClipboardError(t("剪贴板读取超时（20 秒）")) from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()
        low = stderr.lower()
        # stderr 为空也按"没有图"处理：wl-paste 在没有选区时不吭声就退出 1。
        # 真出错（权限、协议）会带文字，走下面那条。
        if not stderr or any(h in low for h in _NO_IMAGE_HINTS):
            return None
        raise ClipboardError(stderr.splitlines()[-1])

    if not proc.stdout:
        return None
    return "data:image/png;base64," + base64.b64encode(proc.stdout).decode("ascii")


def read_image() -> str | None:
    """剪贴板里的图 → `data:image/png;base64,…`；没有图返回 None。"""
    if sys.platform == "win32":
        return _win_read()
    if sys.platform.startswith("linux"):
        return _linux_read()
    raise ClipboardError(t("剪贴板图片读取仅在 Windows 和 Linux 下支持"))
