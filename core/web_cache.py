"""`web_fetch` 的响应缓存。

一张按 URL 的表：30 分钟内同一个地址再抓一次，不再出网。

判据：

- 键是归一化后的 URL：scheme 与主机小写、去掉默认端口与 `#` 片段，
  查询串原样保留（它可能就是"另一页"）。主机名里的 `www.` 不剥
  —— 那是另一个主机名，服务端可以给出不同的内容。
- 只缓存成功响应。跳转与 4xx / 5xx 不缓存：跳转链会变，
  存一条 302 会把人钉在旧地址上；失败多数是临时的。
- 时效从抓取那一刻算起，不滑动。缓存回答的是"这条内容有多旧"，
  与"谁最近读过它"无关。
- 总量 256 MiB，按最近使用淘汰。过期条目在每次读 / 写时顺手清掉。
- 它是进程级的网络缓存，不是会话态：键是 URL，与哪条会话读过它无关，
  `/clear` 不清它（登记在 `core/state_registry.py` 的进程级那张表里）。

命中时不做任何出网动作，所以 `web_fetch` 把出网审批闸放在命中判断之后：
闸问的是"该不该把这次请求发出去"，没有发出去的就不该问。地板与金丝雀
仍然逐次跑 —— 它们只看地址，不看网络。
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    "TTL_SECONDS", "MAX_TOTAL_BYTES",
    "normalize", "key_of", "get", "put", "clear", "stats",
]

#: 一条内容活多久。网页的新鲜度以分钟计，不是以天计。
TTL_SECONDS = 30 * 60

#: 整个缓存的上限。单次抓取本身封在 64 MiB，这里能装下几份大的。
MAX_TOTAL_BYTES = 256 * 1024 * 1024

_DEFAULT_PORTS = {"http": 80, "https": 443}

_LOCK = threading.RLock()

#: 键 -> {status, content_type, raw, result, at, used}。
_ITEMS: Dict[str, Dict[str, Any]] = {}


def normalize(url: str) -> str:
    """缓存键用的 URL 形状（判据见模块 docstring）。"""
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = "[%s]" % host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host if port is None else "%s:%d" % (host, port)
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def key_of(url: str) -> str:
    """归一化之后的 URL 的短摘要（URL 本身可能很长）。"""
    return hashlib.sha256(normalize(url).encode("utf-8")).hexdigest()[:24]


def _total_bytes() -> int:
    """当前占用（按响应体计）。调用方持锁。"""
    return sum(len(item.get("raw") or b"") for item in _ITEMS.values())


def _drop_expired(now: float) -> None:
    """清掉过期的条目。调用方持锁。"""
    expired = [k for k, item in _ITEMS.items()
               if now - float(item.get("at") or 0.0) > TTL_SECONDS]
    for key in expired:
        _ITEMS.pop(key, None)


def get(url: str) -> Optional[dict]:
    """命中返回一份副本，未命中或已过期返回 `None`。

    副本里的 `age_s` 是这一份内容被缓存了多久 —— 回执要把它如实给模型，
    "这是刚抓的"与"这是半小时前的"不能长得一样。
    """
    key = key_of(url)
    now = time.monotonic()
    with _LOCK:
        item = _ITEMS.get(key)
        if item is None:
            return None
        if now - float(item.get("at") or 0.0) > TTL_SECONDS:
            _ITEMS.pop(key, None)
            return None
        item["used"] = now
        return {
            "status": item.get("status"),
            "content_type": item.get("content_type") or "",
            "raw": item.get("raw") or b"",
            "result": dict(item["result"]) if item.get("result") else None,
            "age_s": now - float(item.get("at") or now),
        }


def put(url: str, *, status, content_type: str, raw: bytes, result: dict) -> None:
    """存一份抓取结果。装不下就悄悄不存（缓存是加速，不是账本）。

    先清过期、再按最近使用淘汰，腾够了才写 —— 新这一份不会被自己挤掉。
    """
    body = bytes(raw or b"")
    key = key_of(url)
    now = time.monotonic()
    with _LOCK:
        _drop_expired(now)
        _ITEMS.pop(key, None)
        if len(body) > MAX_TOTAL_BYTES:
            return
        while _ITEMS and _total_bytes() + len(body) > MAX_TOTAL_BYTES:
            oldest = min(_ITEMS, key=lambda k: float(_ITEMS[k].get("used") or 0.0))
            _ITEMS.pop(oldest, None)
        if _total_bytes() + len(body) > MAX_TOTAL_BYTES:
            return
        _ITEMS[key] = {
            "status": int(status or 0),
            "content_type": str(content_type or ""),
            "raw": body,
            "result": dict(result or {}),
            "at": now,
            "used": now,
        }


def clear() -> None:
    """全清（测试与排障用；生产路径不调它）。"""
    with _LOCK:
        _ITEMS.clear()


def stats() -> dict:
    """当前读数：条数与占用字节（`/api` 与测试用）。"""
    with _LOCK:
        return {"entries": len(_ITEMS), "bytes": _total_bytes()}
