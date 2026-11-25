"""终端界面的纯函数部分：一行输入怎么解释、路径怎么压。

单独放一个模块是为了能测。它们错了不会报错，只会表现成"这条命令被当成
消息发给了模型"或者"抬头那行折了行、缩进全歪"—— 在交互界面里几乎不可能
一眼看出来，所以宁愿费一道手续把它们变成可断言的东西。
"""

from __future__ import annotations

import os
import re

from rich.cells import cell_len

#: 只在桌面端有意义的命令：它们会切后端那一段对话或截断历史，而转写区
#: 不会重画 —— 发过去只会得到一句"已切换"，屏幕上还是旧对话。
#: 补全里不列（见 `app._SUGGEST_HIDDEN`），手打也会在这里被拦下。
_DESKTOP_ONLY = ("/resume", "/rewind")

#: `/mode` 的这两个词都指向「完全权限」（中文名与英文键，`core.modes.mode_of`
#: 两条都认）。TUI 在发出去之前要认出来：切到完全权限得先过一次安全提示。
_FULL_ACCESS = ("full_access", "完全权限")


def targets_full_access(text: str) -> bool:
    """这一行是不是「把模式切到完全权限」。

    只认 `/mode <词>` 这一种形状 —— 正文里出现这个词不算
    （`/btw 完全权限是什么` 是提问，不是切换）。
    """
    parts = (text or "").strip().split(maxsplit=1)
    # 命令名大小写敏感 —— 与后端那一层的匹配同一口径。这里放宽的话，
    #   会出现「先提示一次，然后命令没被当成命令执行」。
    if len(parts) != 2 or parts[0] != "/mode":
        return False
    return parts[1].strip() in _FULL_ACCESS


def model_command(text: str) -> tuple[str, str]:
    """`/model` 的子命令：`("status"|"more"|"set"|"switch"|"", 参数)`。

    TUI 只拦 `set`（它要弹掩码编辑面板，后端没有输入口）；
      其余原样交给后端 `_cmd_model`，两处不各写一套语义。
    """
    parts = (text or "").strip().split(maxsplit=2)
    if not parts or parts[0] != "/model":
        return "", ""
    if len(parts) == 1:
        return "status", ""
    sub = parts[1]
    if sub == "more":
        return "more", ""
    if sub == "set":
        return "set", (parts[2].strip() if len(parts) > 2 else "")
    return "switch", text.strip().split(maxsplit=1)[1]


def parse_command(text: str) -> tuple[str, str]:
    """把一行输入分成"本地动作"和"要发出去的话"。

    返回 `(action, payload)`，action 取值 exit / cls / new / desktop / chat。
    不带斜杠也认：用惯了 REPL 的人会直接敲 `exit`。
    """
    t = text.strip()
    low = t.lower()
    if low in ("/exit", "/quit", "exit", "quit"):
        return ("exit", "")
    if low == "/cls":
        return ("cls", "")
    if low == "/new":
        return ("new", "")
    for cmd in _DESKTOP_ONLY:
        if low == cmd or low.startswith(cmd + " "):
            return ("desktop", t)
    return ("chat", t)


def short_path(path: str, limit: int) -> str:
    """把路径压进 limit 列，保留头尾、掐掉中间。

    掐中间不是图省事：末尾那一段是"哪个项目"，盘符是"在哪台机器"，
    中间那串嵌套目录名删掉最不心疼。

    宽度按**显示列**算，不按字符数：中文目录名一个字符占两列，按字符数切
    会把抬头那一行撑破、换行之后缩进全乱。
    """
    if cell_len(path) <= limit:
        return path
    parts = [p for p in re.split(r"[\\/]+", path) if p]
    if len(parts) < 2:
        return path[: max(0, limit - 1)] + "…"
    for n in (3, 2, 1):
        if len(parts) <= n:
            continue
        cand = "…" + os.sep + os.sep.join(parts[-n:])
        if cell_len(cand) <= limit:
            return cand
    # 连最后一段都放不下：硬切，但留个省略号说明被切过。
    keep = parts[-1]
    if cell_len(keep) <= limit - 1:
        return "…" + os.sep + keep
    return keep[: max(1, limit - 1)] + "…"
