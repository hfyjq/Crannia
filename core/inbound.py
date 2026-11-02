# -*- coding: utf-8 -*-
"""外部事件入口：把一条消息送进管线。

## 来源要先注册

插件在装载时调用 `register_source("qq", platforms=("qq",))`。未注册的来源一律拒绝，
避免任何进程内代码随手往对话里塞消息。

## 投递方式

事件按 `platform:kind:id` 映射到 `MessageSession`，与通道共用同一套会话键。
投递走每会话的 worker 队列（`core.channel_turns`），因此不会阻塞调用方：
`submit()` 入队即返回，真正跑轮的是 worker 线程。

## 限制

- 每个来源每分钟最多 20 条。
- 同一个 `dedupe_key` 在 10 分钟内只接受一次。
- 第一版只收文本；带媒体的调用会被拒绝而不是静默丢掉。

## 回复

*reply* 给了就在这一轮结束时收到最终文本；没给时本轮照常落库，但没有出口。
审批在这个入口里无法完成：它同样依赖"回答变成新的一轮"，而外部事件没有那一轮。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, Optional

__all__ = ["register_source", "available_sources", "configure", "submit",
           "MAX_PER_MINUTE", "DEDUPE_TTL"]

#: 每个来源每分钟的条数上限。
MAX_PER_MINUTE = 20

#: 同一个去重键的保留时长（秒）。
DEDUPE_TTL = 600.0

_SOURCES: dict[str, dict] = {}
_WINDOW: dict[str, list[float]] = {}
_SEEN: dict[str, float] = {}
_scheduler = None

_lock = threading.Lock()

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


def configure(scheduler) -> None:
    """bootstrap 调这一处：把调度器交进来。"""
    global _scheduler
    _scheduler = scheduler


def register_source(name: str, *, platforms=(), max_per_minute: int = MAX_PER_MINUTE):
    """注册一个事件来源，返回撤销函数。"""
    key = str(name or "").strip()
    if not _NAME_RE.match(key):
        raise ValueError("来源名不合法：%r" % (name,))
    spec = {
        "platforms": tuple(str(p) for p in (platforms or ()) if str(p).strip()),
        "max_per_minute": int(max_per_minute) if int(max_per_minute) > 0
        else MAX_PER_MINUTE,
    }
    with _lock:
        _SOURCES[key] = spec

    def _dispose() -> None:
        with _lock:
            if _SOURCES.get(key) is spec:
                _SOURCES.pop(key, None)

    return _dispose


def available_sources() -> list[str]:
    with _lock:
        return sorted(_SOURCES)


def _rate_ok(source: str, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        stamps = [t for t in _WINDOW.get(source, []) if now - t < 60.0]
        if len(stamps) >= limit:
            _WINDOW[source] = stamps
            return False
        stamps.append(now)
        _WINDOW[source] = stamps
    return True


def _dedupe_ok(key: str) -> bool:
    if not key:
        return True
    now = time.monotonic()
    with _lock:
        for k in [k for k, t in _SEEN.items() if now - t > DEDUPE_TTL]:
            _SEEN.pop(k, None)
        if key in _SEEN:
            return False
        _SEEN[key] = now
    return True


def submit(*, platform: str, kind: str, session_id: str, source: str,
           user_id: str = "", text: str = "", media=None,
           dedupe_key: Optional[str] = None,
           reply: Optional[Callable[[str], None]] = None) -> dict:
    """投递一条外部事件。入队即返回。"""
    src = str(source or "").strip()
    with _lock:
        spec = dict(_SOURCES.get(src) or {})
    if not spec:
        return {"accepted": False,
                "error": "来源没有注册：%s（先调用 register_source）" % src}

    plat = str(platform or "").strip()
    if not plat:
        return {"accepted": False, "error": "缺少 platform"}
    allowed = spec.get("platforms") or ()
    if allowed and plat not in allowed:
        return {"accepted": False,
                "error": "来源 %s 没有声明平台 %s（已声明：%s）"
                         % (src, plat, ", ".join(allowed))}

    kd = str(kind or "").strip().lower()
    if kd not in ("private", "group"):
        return {"accepted": False,
                "error": "kind 只能是 private 或 group（收到 %r）" % kind}
    sid = str(session_id or "").strip()
    if not sid:
        return {"accepted": False, "error": "缺少 session_id"}
    body = str(text or "")
    if media:
        return {"accepted": False,
                "error": "第一版只收文本，媒体请改用附件或通道"}
    if not body.strip():
        return {"accepted": False, "error": "正文为空"}

    key = dedupe_key or "%s|%s|%s|%s|%s" % (src, plat, kd, sid, body[:200])
    if not _dedupe_ok(str(key)):
        return {"accepted": False, "error": "重复事件（同一个去重键已处理过）"}
    if not _rate_ok(src, int(spec.get("max_per_minute") or MAX_PER_MINUTE)):
        return {"accepted": False,
                "error": "来源 %s 一分钟内的条数已达上限" % src}
    if _scheduler is None:
        return {"accepted": False, "error": "框架没在运行，事件没有投递"}

    from channels.base import (ChannelMetadata, Message, MessageEvent,
                               MessageSession, MessageType)
    from core.channel_turns import submit as queue
    from core.session_key import session_key_for

    session = MessageSession(plat, MessageType(kd), sid)
    msg = Message.from_text(sender_id=str(user_id or src), text=body,
                            message_type=MessageType(kd), session=session)
    event = MessageEvent(
        session=session,
        message=msg,
        platform_meta=ChannelMetadata(name="external:" + src,
                                      description="外部事件", platform_type=plat),
        extras={"origin": "external", "external_source": src},
    )

    if reply is not None:
        def _cb(ev, _reply=reply):
            text_out = (getattr(ev, "extras", {}) or {}).get("response") or ""
            if text_out:
                try:
                    _reply(text_out)
                except Exception as exc:                      # noqa: BLE001
                    print("[inbound] 回复回调失败（%s: %s）"
                          % (type(exc).__name__, exc))
        event.reply_callback = _cb

    sch = _scheduler

    async def _job() -> None:
        await sch.execute(event)

    key = session_key_for(session)
    if not queue(key, _job):
        #: 队列满了：这条没排上。回 `accepted: True` 就是撒谎 ——
        #:   调用方会以为投递成功，而用户什么都没等到。
        print("[inbound] 来源 %s 的队列满了，这条没有投递" % src)
        return {"accepted": False, "error": "会话队列已满，请稍后重试"}
    print("[inbound] 来源 %s 投递了一条（%s:%s:%s）" % (src, plat, kd, sid))
    return {"accepted": True, "session_key": key}
