"""逐条轨迹 —— 记录「调了什么 · 参数是什么 · 它想了什么 · 结果是什么」。

## 它补的是哪一半

事件流能说「发生了什么事」（谁 · 什么时候 · 多久 · 成没成），
说不了「具体说了什么」：
· `tool.start` 只有 `arg_hint`（一个「`_s2.py`」这种提示）
· `thinking` 只有 `phase=start/end`
· `tool.end` 只有 `result_chars` + `error`

这一层把全文（参数 / 思考 / 结果）按上限截断后存进进程内的环形缓冲，
界面按需拉取（`GET /api/trace`）。

## 三条判据

1. 默认关 —— 逐字轨迹是排障数据，不是主流程；开关交给用户。
   开关是 `data/settings.json` 的 `trace_full`，每次请求现读，改完当场生效。
2. 不进 `DURABLE_TYPES` —— 这些是"读一次就够"的排障数据，
   落进 `data/events/*.jsonl` 会让日志在几天内涨到几百 MB。
3. 有界：每会话最多 `MAX_RECORDS` 条、每格最多 `FIELD_CAP` 字符。
   超了丢最旧的（环形），并且记着丢过多少（`dropped`）——
   "没看见"与"没发生"必须分得开。

## 它不是什么

· 不是审计（审计账本记的是"谁批准了什么"，落盘、长期）
· 不是模型上下文的一部分 —— 这些字永远不回灌给模型。

键是会话键（通道键），与审批浮窗同一个口径（`session_key_for`）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

__all__ = [
    "SETTING_KEY", "MAX_RECORDS", "FIELD_CAP", "enabled", "record_tool",
    "record_thinking", "snapshot", "reset", "clear_all", "stats",
]

SETTING_KEY = "trace_full"

#: 每个会话最多留几条（200 条 × 4k ≈ 800KB，够回答"刚才那几步"）。
MAX_RECORDS = 200

#: 每一格最多留多少字符（超出截断并标出来）。
FIELD_CAP = 4000

_SETTINGS_PATH = Path("data/settings.json")

_lock = threading.Lock()

#: `{session_key: [record, ...]}` —— 环形缓冲。
#: `/clear` 清当前会话（会话级状态登记表里有它一条）。
_records: dict[str, list[dict]] = {}

#: `{session_key: int}` —— 每个会话丢过多少条旧记录（"没看见 ≠ 没发生"）。
_dropped: dict[str, int] = {}

#: `{session_key: int}` —— 下一条的序号（单调、从 1 起）。
_seq: dict[str, int] = {}


def enabled() -> bool:
    """开关。读不到 / 坏值，关（默认不打扰）。

    每次现读：设置页改完当场生效，不要求重启。
    """
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        return raw.get(SETTING_KEY) is True
    except Exception:                                           # noqa: BLE001
        return False


def _clip(text, cap: int = FIELD_CAP) -> str:
    s = str(text if text is not None else "")
    if len(s) <= cap:
        return s
    return s[:cap] + "\n…（轨迹截断：原文 %d 字符）" % len(s)


def _append(session_key: str, record: dict) -> None:
    key = str(session_key or "")
    if not key:
        return
    with _lock:
        seq = _seq.get(key, 0) + 1
        _seq[key] = seq
        record["seq"] = seq
        record["ts"] = time.time()
        bucket = _records.setdefault(key, [])
        bucket.append(record)
        if len(bucket) > MAX_RECORDS:
            cut = len(bucket) - MAX_RECORDS
            del bucket[:cut]
            _dropped[key] = _dropped.get(key, 0) + cut


def record_tool(session_key: str, *, name: str, call_id: str = "",
                args="", result="", ok: bool = True,
                duration_ms: int = 0) -> None:
    """记一条工具调用。关着就什么都不做（零成本 —— 一次字典读）。"""
    if not enabled():
        return
    _append(session_key, {
        "kind": "tool",
        "name": str(name or ""),
        "call_id": str(call_id or ""),
        "args": _clip(args),
        "result": _clip(result),
        "ok": bool(ok),
        "duration_ms": int(duration_ms or 0),
    })


def record_thinking(session_key: str, *, text="", duration_ms: int = 0) -> None:
    """记一段模型的思考（`reasoning_content`）。空的不记（多数模型没有）。"""
    body = str(text or "").strip()
    if not body:
        return
    if not enabled():
        return
    _append(session_key, {
        "kind": "thinking",
        "text": _clip(body),
        "duration_ms": int(duration_ms or 0),
    })


def snapshot(session_key: str, *, since: int = 0) -> dict:
    """当前会话的轨迹（`since` = 只要序号大于它的）。

    回 `{enabled, records, dropped, last_seq}`。 `dropped` 是真话：
    用户该知道"这里少了几条"，而不是把 200 条当成全部。
    """
    key = str(session_key or "")
    with _lock:
        rows = [dict(r) for r in (_records.get(key) or [])
                if int(r.get("seq") or 0) > int(since or 0)]
        return {"enabled": enabled(), "records": rows,
                "dropped": int(_dropped.get(key, 0)),
                "last_seq": int(_seq.get(key, 0))}


def stats() -> dict:
    """给测试/诊断用：`{会话数, 总条数}`（不暴露内容）。"""
    with _lock:
        return {"sessions": len(_records),
                "records": sum(len(v) for v in _records.values())}


def reset(session_key: str) -> None:
    """清一个会话的轨迹（`/clear` 走 `state_registry.purge_session`）。"""
    key = str(session_key or "")
    with _lock:
        _records.pop(key, None)
        _dropped.pop(key, None)
        _seq.pop(key, None)


def clear_all() -> None:
    """清全部（测试夹具用；生产只有 `/clear` 那条按会话的路）。"""
    with _lock:
        _records.clear()
        _dropped.clear()
        _seq.clear()
