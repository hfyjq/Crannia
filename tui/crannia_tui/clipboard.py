"""剪贴板里的图：取一张，转成 data URL。

为什么要有子进程：终端应用拿不到剪贴板的位图 —— 剪贴板是操作系统那一层的，
不是终端的。`Get-Clipboard -Format Image` 是系统自带、不用装依赖的那条路；
一次冷启动约 0.4 秒，所以只在按键时才调，不轮询。
"""

from __future__ import annotations

import subprocess
import sys

from .i18n import t

#: 取图并转 base64 的脚本。一次调用做完全部转换 —— 不落临时文件：落盘要么
#: 留在用户目录里，要么还得负责删，两个都是尾巴。
_SCRIPT = (
    "$i = Get-Clipboard -Format Image; "
    "if ($null -eq $i) { exit 3 }; "
    "$ms = New-Object IO.MemoryStream; "
    "$i.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png); "
    "[Convert]::ToBase64String($ms.ToArray())"
)

#: 脚本用这个退出码说"剪贴板里没有图"。它不是错误。
_NO_IMAGE = 3


class ClipboardError(RuntimeError):
    """读剪贴板失败：没有 powershell、超时、或者脚本自己报错。"""


def read_image() -> str | None:
    """剪贴板里的图 → `data:image/png;base64,…`；没有图返回 None。

    `-Sta` 不能省：.NET 的 `Clipboard` 要求 STA 线程，MTA 下直接抛异常。
    """
    if sys.platform != "win32":
        raise ClipboardError(t("剪贴板图片读取仅在windows下支持"))

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
