"""按键诊断：`python -m crannia_tui --keys`。

只在"某个键按了没反应"时用。它把收到的每个 Key 事件同时写进一个日志文件
（键名、字符、是否可打印），不连后端、不读配置。

判据：按下去日志里一行都不加 = 键在终端/输入法那层就被吞了；
加了但字符为空 = "键到了、字没有"，由 `ui.Composer` 按键盘面补字。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from .i18n import t

LOG = Path(tempfile.gettempdir()) / "crannia-keys.log"


def head() -> str:
    """开屏那三行。写成函数：语言在入口才定，模块级常量会冻在导入那一刻。"""
    return (t("按下小键盘的 /，Ctrl+Q 退出。\n"
              "记录：%s\n\n") % LOG)


class KeyProbeApp(App):
    CSS = "#log { padding: 1 2; }"
    BINDINGS = [("ctrl+q", "quit", "退出")]

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []
        try:
            LOG.write_text("", encoding="utf-8")
        except OSError:
            pass

    def compose(self) -> ComposeResult:
        yield Static(head(), id="log")

    def on_key(self, event) -> None:
        line = (f"{event.key!r} | char={event.character!r} | "
                f"printable={event.is_printable}")
        self._lines = (self._lines + [line])[-6:]
        try:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        self.query_one("#log", Static).update(head() + "\n".join(self._lines))
