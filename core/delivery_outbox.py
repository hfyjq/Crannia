"""本轮要交付给用户的文件 —— 进程内的待发件箱。

## 为什么要有它

`deliver_file` 是一个同步工具（派发它在 agent 事件循环的线程上，见
`core/agent_loop.py` 的 `handler(tool_args)`），而"把文件发出去"是通道的
异步能力**（要上传、要用 `context_token`）。

同步工具不能 `await` 自己所在的那个循环（那是死锁，不是"稍微等一下"），
也不能另起一个循环去用通道的 HTTP 客户端（客户端绑在它自己的循环上）。

工具只登记意图，通道在它自己的循环里、这条回复发完之后取走并发送。
三条判据：

  * 登记与发送在同一个循环上，不跨循环用客户端；
  * 发送排在回复之后，用户先看见那句话，再收到附件；
  * 失败回一句话给用户（通道那一侧负责），不静默。

## 为什么取的时候要按键筛

`take_all()` 是给 Web 那条路用的（`/api/chat` 一次请求取一次，历史上就这么写的）。
IM 通道必须只取自己那一轮的：六个通道共用一个进程，而私聊跨通道共用
`workspace:private` 这一个会话键（`core/session_key.py`），只按会话键筛，
会把别人的文件发给自己。三格都要对上：`session_key` + `channel` + `user_id`。
"""

from __future__ import annotations

import threading
from typing import Optional

#: 最多留多少条（防"通道一直没起来"时无限长）。
MAX_PENDING = 50

_lock = threading.Lock()
_items: list[dict] = []


def put(name: str, path: str, method: str, size_kb: float,
        session_key: str = "", channel: str = "", user_id: str = "") -> dict:
    """登记一条。返回登记进去的那条（调用方拿它拼回执）。"""
    rec = {
        "name": name,
        "path": path,
        "size_kb": size_kb,
        "method": method,
        "session_key": str(session_key or ""),
        "channel": str(channel or ""),
        "user_id": str(user_id or ""),
    }
    with _lock:
        _items.append(rec)
        if len(_items) > MAX_PENDING:
            del _items[:-MAX_PENDING]
    return rec


def take_all() -> list[dict]:
    """取走给界面看的那些（`method != "channel"`），取走即清。

    为什么把 `method="channel"` 留下：那几条属于某条通道的待发队列。
      Web 端点（`/api/chat`）若把它们一起取走，微信那一轮的文件就再也发不出去，
      而它还会出现在另一个会话的下载按钮上 —— 跨会话串台。
    """
    global _items
    with _lock:
        out = [r for r in _items if r.get("method") != "channel"]
        _items = [r for r in _items if r.get("method") == "channel"]
    return out


def _matches(rec: dict, session_key: str, channel: str, user_id: str) -> bool:
    if session_key and rec.get("session_key") != session_key:
        return False
    if channel and rec.get("channel") != channel:
        return False
    if user_id and rec.get("user_id") != user_id:
        return False
    return True


def take_for(session_key: str = "", channel: str = "",
             user_id: str = "") -> list[dict]:
    """取走这三格都对得上的那些，其余原样留着。

    三条都给空串时等价于 `take_all()` —— 那是给测试用的，生产调用点
      必须给全（否则"只按会话键筛"就会串台，见模块 docstring）。
    """
    global _items
    with _lock:
        kept: list[dict] = []
        out: list[dict] = []
        for rec in _items:
            (out if _matches(rec, session_key, channel, user_id) else kept).append(rec)
        _items = kept
    return out


def pending_count() -> int:
    with _lock:
        return len(_items)


def clear(session_key: Optional[str] = None) -> None:
    """清空。给 `session_key` 时只清那一条会话的（`/clear` 用）。"""
    global _items
    with _lock:
        if session_key is None:
            _items = []
        else:
            _items = [r for r in _items if r.get("session_key") != session_key]


def forget(session_id: str = "") -> None:
    """`/clear` 用（`core/state_registry.py` 的登记项）。

    传进来的 `session_id` 是通道键，而记录里存的是会话键 + 用户 id
两边对不上，这里清空整张表。判据：正常情况下通道每轮都会把它取空
    （`take_for`），剩下的只可能是"没发成"的残留 —— 新一段对话不该替上一段补发。
    """
    clear()
