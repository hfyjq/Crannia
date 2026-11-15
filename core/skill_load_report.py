# -*- coding: utf-8 -*-
"""装载结果与失败阶段。

四个阶段：`discover`（有没有清单）· `compatibility`（版本与能力）·
`import`（模块能不能导入）· `start`（服务能不能起来）。

失败只影响那一个包，记录在这里，供技能页显示具体原因。
"""

from __future__ import annotations

import threading

__all__ = ["STAGES", "note", "get", "all", "clear"]

STAGES = ("discover", "compatibility", "import", "start")

_REPORT: dict[str, dict] = {}
_lock = threading.Lock()


def note(name: str, stage: str, ok: bool, message: str = "") -> None:
    key = str(name or "").strip()
    if not key:
        return
    with _lock:
        _REPORT[key] = {
            "stage": stage if stage in STAGES else "discover",
            "ok": bool(ok),
            "message": str(message or ""),
        }


def get(name: str) -> dict | None:
    with _lock:
        item = _REPORT.get(str(name or "").strip())
        return dict(item) if item else None


def all() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _REPORT.items()}


def clear() -> None:
    with _lock:
        _REPORT.clear()
