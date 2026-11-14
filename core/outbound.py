# -*- coding: utf-8 -*-
"""出站：把一条消息或文件发到指定会话。

## 收件人

`to` 的格式是 `platform:kind:id`，例如 `telegram:private:123456`、
`feishu:group:oc_xxx`。它直接映射到 `MessageSession`，与通道、会话键同一套口径。

## 发送方式

- `chat` 动作：用框架自己的通道发送（`resolve_channel` + `channel.send`）。
  只有通道返回 `True` 才算送达，其余按失败处理。
- 其他动作：调用注册的 handler，由插件实现自己的协议。

## 限制

- 每个收件人每分钟最多 20 条。
- 审计只记动作、收件人与结果；正文不写日志。
- 文件必须在当前工作区之内。

## 审批

本模块只负责传输。审批在工具调用之前完成：`send_outbound` 声明为不可逆，
由兜底闸按收件人询问，选项里的「本次对话内不再询问这个操作」对该收件人生效。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["register_outbound_action", "available_actions", "send",
           "MAX_PER_MINUTE"]

#: 每个收件人每分钟的条数上限。
MAX_PER_MINUTE = 20

#: 已注册的动作：名字 -> {handler, description, input_schema, hosts, platforms}。
_ACTIONS: dict[str, dict] = {}

#: 限流窗口：收件人键 -> 最近一分钟内的时间戳。
_WINDOW: dict[str, list[float]] = {}

_lock = threading.Lock()


def register_outbound_action(
    name: str,
    handler: Optional[Callable[..., Any]] = None,
    *,
    description: str = "",
    input_schema: Optional[dict] = None,
    hosts=(),
    platforms=(),
) -> Callable[[], None]:
    """注册一个出站动作，返回撤销函数。

    *handler* 为空表示"由框架的 `chat` 通道发送"。给了 handler 的动作在
    审批通过之后调用 `handler(**params)`，返回值应是 {"ok": bool, ...}。
    """
    key = str(name or "").strip()
    if not key or key == "chat":
        raise ValueError("动作名不能为空，也不能用保留名 chat")
    spec = {
        "handler": handler,
        "description": str(description or ""),
        "input_schema": dict(input_schema or {}),
        "hosts": tuple(str(h) for h in (hosts or ()) if str(h).strip()),
        "platforms": tuple(str(p) for p in (platforms or ()) if str(p).strip()),
    }
    with _lock:
        _ACTIONS[key] = spec

    def _dispose() -> None:
        with _lock:
            if _ACTIONS.get(key) is spec:
                _ACTIONS.pop(key, None)

    return _dispose


def available_actions() -> list[str]:
    with _lock:
        return sorted(_ACTIONS)


def _rate_ok(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        stamps = [t for t in _WINDOW.get(key, []) if now - t < 60.0]
        if len(stamps) >= MAX_PER_MINUTE:
            _WINDOW[key] = stamps
            return False
        stamps.append(now)
        _WINDOW[key] = stamps
    return True


def _checked_file(raw) -> Any:
    """文件必须存在且位于当前工作区之内。返回 Path 或错误字典。"""
    try:
        from core.workspace import primary_output_dir
        root = Path(primary_output_dir()).resolve()
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": "文件路径解析失败：%s: %s"
                % (type(exc).__name__, exc)}
    if not p.is_file():
        return {"ok": False, "error": "文件不存在：%s" % p}
    try:
        p.relative_to(root)
    except ValueError:
        return {"ok": False, "error": "文件不在工作区内：%s" % p}
    return p


def _send_via_channel(session, text: str, files) -> dict:
    from core.cron.runner import PERSIST_ONLY_PLATFORMS, resolve_channel

    if session.platform in PERSIST_ONLY_PLATFORMS:
        return {"ok": True, "detail": "已落库（%s 通道按落库算交付）"
                % session.platform}

    channel = resolve_channel(session.platform)
    if channel is None:
        return {"ok": False,
                "error": "通道 %s 现在没在运行，消息没有发出去" % session.platform}

    from channels.message import File, MessageChain, Plain

    chain = MessageChain()
    if text:
        chain.add(Plain(text))
    for item in files or ():
        fp = _checked_file(item)
        if isinstance(fp, dict):
            return fp
        chain.add(File(name=fp.name, local_path=str(fp)))
    if not chain:
        return {"ok": False, "error": "没有要发的内容"}

    try:
        delivered = asyncio.run(channel.send(session, chain))
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if delivered is not True:
        return {"ok": False, "error": "通道报告未送达（%s）" % session.platform}
    return {"ok": True, "detail": ""}


def send(*, to: str, text: str = "", files=None, action: str = "chat",
         params: Optional[dict] = None) -> dict:
    """发出一条消息或调用一个已注册动作。"""
    target = str(to or "").strip()
    if not target:
        return {"ok": False, "error": "缺少收件人（格式 platform:kind:id）"}

    from channels.base import MessageSession

    try:
        session = MessageSession.from_str(target)
    except Exception:                                         # noqa: BLE001
        return {"ok": False,
                "error": "收件人格式应为 platform:kind:id（收到 %r）" % target}

    if not _rate_ok(str(session)):
        return {"ok": False,
                "error": "这个收件人一分钟内已发 %d 条，达到上限" % MAX_PER_MINUTE}

    act = str(action or "chat").strip()
    if act == "chat":
        return _send_via_channel(session, str(text or ""), files)

    with _lock:
        spec = dict(_ACTIONS.get(act) or {})
    if not spec:
        names = ", ".join(available_actions()) or "（无）"
        return {"ok": False, "error": "没有这个出站动作：%s（可用：%s）"
                % (act, names)}
    handler = spec.get("handler")
    if handler is None:
        return {"ok": False,
                "error": "动作 %s 没有实现，注册时未提供 handler" % act}
    payload = dict(params or {})
    payload.setdefault("to", target)
    try:
        result = handler(**payload)
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    if isinstance(result, dict):
        result.setdefault("ok", False)
        return result
    return {"ok": True, "detail": str(result or "")}
