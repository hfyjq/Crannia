# -*- coding: utf-8 -*-
"""`recap` —— 离开一段时间后回来，先说一句「我们到哪了」。

判据：

    做了什么 + 当前状态 + 需要用户做什么 + 可关闭

## 它与 `/recap` 不是一件事

| | `/recap` | 本模块的 `recap` |
|---|---|---|
| 谁触发 | 用户手打 | 框架自己（离开够了就触发） |
| 花不花钱 | 一次 LLM（`core/side_call.py`） | 零 LLM |
| 落点 | 一轮回复（`stop`，主循环不跑） | 一条 `recap` 事件 → 前端一块 |

## 为什么它是零 LLM

会话笔记（`core/memory/session_memory.py`）本来就在写这件事 ——
`Current Status` / `Key Decisions` / `Open Questions` / `Next Steps`。
再花一次模型调用去总结同一件事，等于同一份派生数据付两遍钱
（「账只允许一个仪器」的同族判据）。

本模块不 import 任何 provider，它只读笔记、拼一段话。
笔记空着，不出声（没得说就不说，那不是错误）。

## 两条边界

- `last_turn_at is None`，不触发。那个值的意思是「本进程见过这个
  会话的轮末」；`None` = 没见过，我们不能断言"用户离开过"
  （读不到 ≠ 0，也 ≠ "很久"）。
- 默认关：它是用户打开的功能，不是我们替他打开的。
  关着的时候本模块一行都不做（不开支、不出事件）。

## 一次"回来"只说一遍（不只是靠 `last_turn_at` 前进）

`last_turn_at` 是「上一轮跑完了」的时刻。若那一轮在跑完之前就失败
（provider 报错、进程退出），它会停在旧值 —— 之后用户每发一句话，
`due()` 都判真，回顾就会隔一句出现一次。

所以这里另记一格：给出回顾时把它记在 `_LAST_RECAP[session]`。
下一条消息若仍对着同一个 `last_turn_at`（说明那之后还没有一轮真正跑完），
直接不说话 —— 不必等到它再老一个间隔。等哪一轮跑完，`last_turn_at` 前进，
下一个"离开够久"的窗口才重新打开。
"""

from __future__ import annotations

import re
import time
from typing import Dict, Optional

#: 设置键（`data/settings.json`）。两个键：开关 + 触发时间。
#: 「可关闭」那一格就落在 `ENABLED_KEY` 上 ——
#: 不是一个 ✕ 按钮，是设置页那一栏（在设置里可关闭）。
ENABLED_KEY = "recap_enabled"
SETTING_KEY = "recap_after_minutes"

#: 默认：关（理由见模块 docstring 的「默认关」）。
DEFAULT_ENABLED = False
#: 离开多久算"离开"（分钟）。取 30：比滑窗冷闸的 90 短
#: （那条判的是"该不该剪历史"，代价高得多），
#: 又长到不会在连续对话里误触发。
DEFAULT_MINUTES = 30
#: 边界（设置在用户手里，越界一律拒，不静默夹取）。
MIN_MINUTES = 1
MAX_MINUTES = 1440          # 一天；超过它等于"永远不触发"，那就该关开关

#: 本会话上一次给出回顾的时刻。键 = session_id，进程内。
#: 判据见模块 docstring 的「一次"回来"只说一遍」—— 它防的是
#: 「那一轮没跑完 => `last_turn_at` 不前进 => 每句话都来一段回顾」。
#: 登记在 `core/state_registry.py`（`/clear` 走 `forget`）。
_LAST_RECAP: Dict[str, float] = {}


def forget(session_id: str) -> None:
    """`/clear` 的清理（`state_registry` 的 purge 入口）。"""
    _LAST_RECAP.pop(session_id, None)


def _settings() -> dict:
    """读 `data/settings.json`。读不到 / 坏文件，空字典（读不到，默认）。"""
    import json
    from pathlib import Path

    try:
        with open(Path("data/settings.json"), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def enabled() -> bool:
    """开关。只认真 `True` —— `"true"` / `1` 都不算（`bool` 坑）。"""
    return _settings().get(ENABLED_KEY) is True


def gap_minutes() -> int:
    """触发时间（分钟）。读不到 / 越界 / 不是整数，默认。

    `bool` 必须单独拒：`isinstance(True, int)` 是 `True`
    （`effort_level: true` 会被当成档 1）。
    """
    v = _settings().get(SETTING_KEY)
    if isinstance(v, bool) or not isinstance(v, int):
        return DEFAULT_MINUTES
    if not (MIN_MINUTES <= v <= MAX_MINUTES):
        return DEFAULT_MINUTES
    return v


def due(last_turn_at: Optional[float], now: Optional[float] = None) -> bool:
    """该不该给这一轮配一段回顾。纯函数（好测、好喂假钟）。"""
    if not enabled():
        return False
    if last_turn_at is None:
        return False        # 没见过这个会话的轮末，不能断言"用户离开过"
    import time

    t = time.time() if now is None else now
    return (t - float(last_turn_at)) > gap_minutes() * 60.0


# ----------------------------------------------------------------------
# 从会话笔记里拼那段话
# ----------------------------------------------------------------------

#: 取哪几段，以及它们在「回顾」里的抬头。顺序 = 用户最想先看到的顺序。
_WANT = (
    ("Current Status", None),          # 不带抬头，它就是"做了什么 + 当前状态"
    ("Next Steps", "下一步"),
    ("Open Questions", "等你"),
)

_EMPTY = ("(none)", "(none yet)", "n/a", "- (none)", "- (none yet)")


def _section(note: str, name: str) -> str:
    """取 `## <name>` 那一段的正文（不含标题）；没有就回空串。

    只认二级标题：笔记的正文里可能有更深的标题，吃进来会串段。
    """
    # `re.S` 是必须的：段正文是多行的，而 `.` 默认不跨行，
    #    漏了这个 flag 会得到"每段都是空的"
    #    （症状是静默的空回顾：`build` 回 `None`，什么都不显示）。
    m = re.search(r"(?m)^##\s+%s\s*$(.*?)(?=^##\s|\Z)" % re.escape(name),
                  note, re.S)
    if not m:
        return ""
    body = m.group(1).strip()
    if body.strip().lower() in _EMPTY:
        return ""
    return body


#: 回顾的长度上限（字符）。与 `core/events.py::_TEXT_CAPS["recap"]`（800）成对 ——
#: 有断言钉着"这里 < 那里"，否则就会被传输层再截一次（没有任何记号的那种）。
#: 这个上限是生产者自己执行、且看得见（末尾一个 `…`）：
#: 它比"让 SSE 那一层去切"好，因为切在哪儿是这里说了算。
MAX_CHARS = 600


def build(note: str) -> Optional[str]:
    """把会话笔记拼成一段回顾；一段都取不到，`None`（不出声）。

    输出形状：一段话、不是一张表 —— 它是"回来时扫一眼"的东西，
    不是报告。
    """
    if not note or not note.strip():
        return None
    parts = []
    for name, label in _WANT:
        body = _section(note, name)
        if not body:
            continue
        # 段内的多行压成一行（`· ` 分隔）—— 回顾要短
        flat = " · ".join(ln.strip().lstrip("-*").strip()
                          for ln in body.splitlines() if ln.strip())
        parts.append(("**%s**：%s" % (label, flat)) if label else flat)
    if not parts:
        return None
    out = "※ 回顾（离开了一段时间）：" + " · ".join(parts)
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS - 1].rstrip() + "…"
    return out


def recap_for(session_id: str, now: Optional[float] = None) -> Optional[str]:
    """轮首先要的那一个调用：该给就给，不该给就 `None`。

    顺序是有讲究的：先判 `due`（纯读设置 + 一个钟），
    判过了才去读笔记文件 —— 绝大多数轮在第一道就返回了，不碰磁盘。

    另有一道「这一次回来已经说过了」：见模块 docstring。判据是
    `_LAST_RECAP[session] >= last_turn_at` —— 说明给出回顾之后
    还没有任何一轮真正跑完（`last_turn_at` 没前进），此时不再重复。
    """
    from core.memory_extract import last_turn_at

    anchor = last_turn_at(session_id)
    if anchor is None:
        return None
    stamp = _LAST_RECAP.get(session_id)
    if stamp is not None and stamp >= float(anchor):
        return None
    t = time.time() if now is None else float(now)
    if not due(anchor, t):
        return None
    from core.memory.session_memory import SessionMemory

    try:
        note = SessionMemory().load(session_id)
    except Exception:  # noqa: BLE001  —— 回顾是**锦上添花**：拿不到就算了，
        return None    # 但绝不因此弄挂这一轮（同 `context.state` 那条兜底判据）
    text = build(note or "")
    if text:
        _LAST_RECAP[session_id] = t
    return text
