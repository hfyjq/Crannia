"""终端能力准备。

主循环和临时区都直接发 ANSI 序列，所以终端**必须**认得它们。Windows 上
这件事不是默认成立的：控制台有自己的一套 API，ANSI 支持要显式打开
（`ENABLE_VIRTUAL_TERMINAL_PROCESSING`）。没打开时渲染出来的不是颜色和光标
移动，而是一串字面量 —— 屏幕上看到 `?[2m` 就是 ESC 被当成了普通字符。

关掉它也有代价：临时区靠 `\\x1b[NA` 上移、`\\x1b[2K` 清行，不生效的话每一帧
都会往下堆，几分钟就把历史冲掉。
"""

from __future__ import annotations

import os
import sys

from .i18n import t

#: `ENABLE_PROCESSED_OUTPUT`。和 VT 一起开：只开 VT 不开它，部分宿主
#: 会把 `\\r` 的处理也交给应用，光标回到行首那一步就没了。
_ENABLE_PROCESSED_OUTPUT = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_STD_OUTPUT_HANDLE = -11


def enable_ansi() -> bool:
    """让当前控制台解释 ANSI。返回"现在能不能用"。

    非 Windows 上 ANSI 本来就有，直接回 True。

    这里不判断"是不是 Windows Terminal"：Windows Terminal 的 VT 一直是开的，
    再开一次是幂等的；而 conhost（`cmd.exe` 那个黑窗口）默认没开，正是要救的
    那一个。分不清楚就别分 —— 统一开一遍，失败也无害。
    """
    if os.name != "nt":
        return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        if handle in (0, -1):
            return False

        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # 输出被重定向到文件/管道时走到这里，是正常情况。
            return False

        want = (mode.value
                | _ENABLE_PROCESSED_OUTPUT
                | _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        if want == mode.value:
            return True
        return bool(kernel32.SetConsoleMode(handle, want))
    except Exception:                                        # noqa: BLE001
        # 这里失败不该让程序起不来：最坏的结果是没有颜色和原地刷新，
        # 而定稿内容仍然照常打印。
        return False


def ansi_passthrough_ok() -> bool:
    """我们写出去的转义序列，终端会不会当控制符解释。

    **这里宁可判错成"不支持"。** 猜错的两种代价不对称：
      · 猜成不支持（其实支持）→ 少一点颜色，功能全在；
      · 猜成支持（其实不支持）→ 屏幕上全是 `?[2m`，整个界面没法看。

    为什么不能靠能力探测：遇到过这样的窗口 —— `create_output()` 给出
    `Windows10_Output`（意为"透传 ANSI"），`GetConsoleMode` 的
    `ENABLE_VIRTUAL_TERMINAL_PROCESSING` 位也是 1，而屏幕上仍然是 `?[2m`。
    探测全绿、实际不解，这条路走不通。

    所以只认两种"确定支持"的情形：
      · 非 Windows；
      · `WT_SESSION` 存在（Windows Terminal 一定解释 ANSI）。
    其余的 Windows 控制台一律走 win32 设色 —— rich 那条路不产生转义序列，
    所以不可能出现字面量，代价只是颜色退到 16 色、临时区关掉。

    想要颜色就设 `CRANNIA_TUI_ANSI=1`，那是使用者自己的判断，比程序猜可靠。
    """
    if os.name != "nt":
        return True
    if os.environ.get("CRANNIA_TUI_ANSI", "") not in ("", "0"):
        return True
    if not os.environ.get("WT_SESSION"):
        return False
    return enable_ansi()


def terminal_width(default: int = 80) -> int:
    """当前终端列数。取不到就给个保守值。

    `shutil.get_terminal_size` 已经查了 `COLUMNS` 和 ioctl；Windows 上它走
    的是 `GetConsoleScreenBufferInfo`，conhost 与 Terminal 都认。这里只补一个
    "返回 0 时别把宽度算成 0"的兜底 —— 宽度为 0 的话折行函数会死循环。
    """
    import shutil

    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default
    return cols if cols > 0 else default


def warn_no_ansi(stream=None) -> None:
    """走不了 ANSI 时说一句，别让人对着满屏 `?[2m` 猜。"""
    out = stream or sys.stderr
    print(t("提示：这个窗口用不了 ANSI，颜色退到 16 色、底部实时区关掉（功能都在）。"),
          file=out)
    print(t("想恢复：换 Windows Terminal 跑，或设 CRANNIA_TUI_ANSI=1 强制开启。"),
          file=out)
