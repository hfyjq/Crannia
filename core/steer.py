"""跑着的时候用户补的那句话 —— 每会话的收件箱。

## 它解决什么

一轮跑着的时候用户又发了一句（非命令）。今天它只能在轮末变成新的一轮
（桌面前端甚至在本地压着、连 POST 都不发）。这一层给那句话一个中途投递的口：

    用户发 → `/api/steer` → 本模块（每会话一个队列）
                                ↓  agent 循环的迭代开头取走
                          下一次请求里就是一条普通 user 消息

## 判据

1. 只在「步」的边界取走 —— agent 循环的迭代开头：上一次工具批全部回填之后、
   下一次 `provider.chat` 之前。那里 `messages` 是平衡的（注入不会让
   `tool_call` 悬空，上游 400）。
2. 注入的是普通 `user` 消息（不带 `kind`）：它就是用户说的话 ——
   重载后照常显示、`history_search` 算"往来"、能收藏。
   与循环里另外两条注入（`OUTPUT_RECOVERY_MSG` / `_nudge`）不同：那两条是
   框架的话，带 `kind`。
3. `drain` 是取走（一次性），不是"读一眼"。轮末由调度器取空：
   剩下的交给 `task.end.steer_leftover`，由前端用同一条气泡接着跑一轮 ——
   绝不留给下一次对话（否则"上一句话在下一段里凭空出现"）。
4. 谁可以推进来由 `web/server.py::api_steer` 一处判：有一轮在跑 · 非 `/` 命令 ·
   没有待审批。本模块只管存，不重复判（两处判据会各自漂）。

## 线程模型

推的一方是 waitress 的请求线程，取的一方是跑那一轮的那个线程，必须上锁。
状态按会话键（`core/session_key.py` 的通道键，与 agent 循环里的
`session_id` 逐字相同）；`/clear` 由 `core/state_registry.purge_session` 清
（已在 `SESSION_SCOPED` 登记 —— 不登记 census 当场红）。
"""

from __future__ import annotations

import threading
from typing import Dict, List

_lock = threading.Lock()

#: 会话键 → `[{id, text}]`。`id` 是前端的 `client_msg_id` —— 回执里带回去，
#: 前端据此把那条气泡的「排队中」摘掉（对不上就是"发出去了没人认领"）。
_inbox: Dict[str, List[dict]] = {}


def push(session_key: str, item: dict) -> None:
    """排进这个会话的收件箱。不判能不能注入（判据在 `/api/steer`）。"""
    if not session_key:
        return
    with _lock:
        _inbox.setdefault(str(session_key), []).append(dict(item or {}))


def drain(session_key: str) -> List[dict]:
    """取走这个会话排队的全部消息（没有就空列表）。"""
    if not session_key:
        return []
    with _lock:
        return _inbox.pop(str(session_key), [])


def pending(session_key: str) -> int:
    """还有几条没被取走（只读；端点/自检用）。"""
    with _lock:
        return len(_inbox.get(str(session_key), ()))


def drop(session_key: str, item_id: str) -> int:
    """把某一条从收件箱里摘掉，返回摘掉几条。

    读者是前端的轮末兜底：这一轮结束时它还挂着（服务端没报、`steer.delivered`
    也没来），前端决定自己接管这条（用同一条气泡接着跑一轮）。
    那时必须先把它从收件箱摘掉 —— 否则它会在下一轮的边界被再注入一次
    （同一个问题被回答两遍，而且第二条的用户气泡根本不存在）。
    """
    if not session_key or not item_id:
        return 0
    with _lock:
        items = _inbox.get(str(session_key))
        if not items:
            return 0
        keep = [it for it in items if str(it.get("id") or "") != str(item_id)]
        dropped = len(items) - len(keep)
        if keep:
            _inbox[str(session_key)] = keep
        else:
            _inbox.pop(str(session_key), None)
        return dropped


def clear(session_key: str) -> None:
    """`/clear` 走这里（`state_registry` 的 purge 适配器）。"""
    if not session_key:
        return
    with _lock:
        _inbox.pop(str(session_key), None)
