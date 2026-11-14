# -*- coding: utf-8 -*-
"""技能的后台服务：模块里的 `start(ctx)` 与 `stop(ctx)`。

一个技能可以在 `handler.py` 里定义：

    def start(ctx):
        ...   # 轮询、长连接、订阅

    def stop(ctx):
        ...

`start` 在一个守护线程里跑（它可以直接写循环），每个技能只启动一次。
停用时调用 `stop`，等指定秒数后不再等待。启动失败会记下来并打印，不拖垮启动。
"""

from __future__ import annotations

import importlib
import threading
import time
from typing import Callable, Iterable, Optional

__all__ = ["start_for", "stop_all", "running", "errors", "note", "status",
           "status_all", "reset"]

#: 已启动的服务：技能名 -> {thread, stop, ctx, error}。
_SERVICES: dict[str, dict] = {}

_lock = threading.Lock()

#: 停止时等待的秒数。
STOP_TIMEOUT = 10.0


def _default_loader(name: str):
    return importlib.import_module("skills.%s.handler" % name)


def start_for(names: Iterable[str],
              loader: Optional[Callable[[str], object]] = None) -> dict:
    """启动这些技能里声明了 `start` 的服务。返回 `{started, skipped, failed}`。"""
    load = loader or _default_loader
    started, skipped, failed = [], [], []
    for name in names:
        key = str(name or "").strip()
        if not key:
            continue
        with _lock:
            if key in _SERVICES:
                skipped.append(key)
                continue
        try:
            module = load(key)
        except Exception as exc:                              # noqa: BLE001
            failed.append((key, "import: %s: %s" % (type(exc).__name__, exc)))
            print("[service] %s 装载失败：%s: %s" % (key, type(exc).__name__, exc))
            continue
        start = getattr(module, "start", None)
        if start is None:
            skipped.append(key)
            continue
        try:
            from core.extension import get_context
            ctx = get_context(key)
        except Exception as exc:                              # noqa: BLE001
            failed.append((key, "context: %s: %s" % (type(exc).__name__, exc)))
            print("[service] %s 取不到上下文：%s: %s" % (key, type(exc).__name__, exc))
            continue

        # 数据版本：不兼容就不启动，并把原因说清楚。
        _chk = ctx.check_data(getattr(module, "migrate", None))
        if not _chk.get("ok"):
            failed.append((key, "data: " + str(_chk.get("error") or "")))
            print("[service] %s 没启动：%s" % (key, _chk.get("error") or ""))
            continue
        if _chk.get("migrated"):
            print("[service] %s 的数据已从 v%s 迁移到 v%s"
                  % (key, _chk.get("found"), _chk.get("ours")))

        rec = {
            "stop": getattr(module, "stop", None),
            "ctx": ctx,
            "error": "",
            "started_at": time.time(),
            "last_at": 0.0,
            "last_text": "",
            "level": "info",
        }

        def _run(_start=start, _ctx=ctx, _rec=rec, _key=key) -> None:
            try:
                _start(_ctx)
            except Exception as exc:                          # noqa: BLE001
                _rec["error"] = "%s: %s" % (type(exc).__name__, exc)
                print("[service] %s 启动失败：%s: %s"
                      % (_key, type(exc).__name__, exc))

        thread = threading.Thread(target=_run, daemon=True,
                                  name="skill-service:%s" % key)
        rec["thread"] = thread
        with _lock:
            _SERVICES[key] = rec
        thread.start()
        started.append(key)
        print("[service] 已启动 %s" % key)
    return {"started": started, "skipped": skipped, "failed": failed}


def stop_all(timeout: float = STOP_TIMEOUT) -> list:
    """停止全部服务，返回停下来的技能名。"""
    names = []
    with _lock:
        items = list(_SERVICES.items())
        _SERVICES.clear()
    for name, rec in items:
        stop = rec.get("stop")
        if callable(stop):
            try:
                stop(rec.get("ctx"))
            except Exception as exc:                          # noqa: BLE001
                print("[service] %s 停止失败：%s: %s"
                      % (name, type(exc).__name__, exc))
        thread = rec.get("thread")
        if thread is not None:
            thread.join(timeout)
        names.append(name)
        print("[service] 已停止 %s" % name)
    return names


def note(name: str, text: str, level: str = "info") -> bool:
    """记一条状态，显示在技能卡片上。没有在跑的服务时返回 False。

    只覆盖最近一次状态；不落盘，重启后清空。
    """
    key = str(name or "").strip()
    with _lock:
        rec = _SERVICES.get(key)
        if rec is None:
            return False
        rec["last_text"] = str(text or "")[:200]
        rec["last_at"] = time.time()
        rec["level"] = str(level or "info")
        return True


def status(name: str) -> dict | None:
    key = str(name or "").strip()
    with _lock:
        rec = _SERVICES.get(key)
        if rec is None:
            return None
        return {
            "running": True,
            "started_at": rec.get("started_at", 0.0),
            "last_at": rec.get("last_at", 0.0),
            "text": rec.get("last_text", ""),
            "level": rec.get("level", "info"),
            "error": rec.get("error", ""),
        }


def status_all() -> dict:
    with _lock:
        names = list(_SERVICES)
    out = {}
    for name in names:
        item = status(name)
        if item is not None:
            out[name] = item
    return out


def running() -> list:
    with _lock:
        return sorted(_SERVICES)


def errors() -> dict:
    with _lock:
        return {k: v.get("error") or "" for k, v in _SERVICES.items()
                if v.get("error")}


def reset() -> None:
    """测试用：清空表，不调用 stop。"""
    with _lock:
        _SERVICES.clear()
