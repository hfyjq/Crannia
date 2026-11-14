"""模型连接重试的状态与节奏。

## 用户看到的形态

网络错误时，绿色横幅变成红色横幅并常驻，内容（以网络错误为例）：

    '网络连接异常--请检查宽带互联网连接    正在重试 1/10'

重试间隔逐渐变慢：1s/2s/3s/5s/10s/15s/30s/60s/60s/60s。
一旦成功连接，横幅变绿、取消常驻，恢复原样。

## 谁在用

- `providers/openai_compat.py`（`chat` 与 `chat_stream` 两条路）在
  连接 / 超时错误上按这里的节奏重试；
- 每次失败 → `note()`：记状态（`/api/status` 读）+ 发事件
  （`llm.retry`，SSE 立刻送到界面）；
- 成功 → `clear()`：状态清掉 + 发 `llm.retry_ok`（横幅变绿、取消常驻）。

## 节奏

共 10 次重试，第 10 次沿用最后一个 60s（`RETRY_DELAYS_MS` 补齐到 10 项）。
最坏情况下这一轮要等约 4 分钟 —— 这是刻意取舍（宁可慢也别直接断）。

只有连接 / 超时重试。401 / 400 / 429 / 超长不重试 ——
重试它们只会把同一个错误再犯十次。

## 状态是会话级的

键 = 通道键（`request_snapshot.current_session_id()`，与 `/api/status` 同一口径）。
取不到（辅助调用 / 测试直驱），不发事件、不记状态，但照常睡
（重试本身与界面无关）。
"""

from __future__ import annotations

import time

__all__ = [
    "RETRY_DELAYS_MS", "MAX_RETRIES", "SILENT_DELAY_MS", "SILENT_MAX",
    "RETRYABLE", "delay_for", "step", "note", "fail", "clear", "get",
    "reset", "reset_if_retrying", "wait", "wait_async",
]

#: 每次重试前等多久（毫秒）。第 9、10 项都是 60s —— 上限是 60s。
RETRY_DELAYS_MS: tuple[int, ...] = (
    1_000, 2_000, 3_000, 5_000, 10_000, 15_000, 30_000, 60_000, 60_000, 60_000,
)

MAX_RETRIES = len(RETRY_DELAYS_MS)

#: 上面那 10 次都失败之后转入"静默期"：每 120 秒一次，最多 10 次。
#: 静默期只是等待上游恢复，横幅上不再报次数（那会变成一条永远在跳的计数器）。
SILENT_DELAY_MS = 120_000
SILENT_MAX = 10

#: 会重试的类别。`rate_limit` 不在里面 —— 429 是"过一会儿再来"，
#: 但自动重试会在限流窗口里继续打上游；交给用户决定（文案已写明重试）。
RETRYABLE: tuple[str, ...] = (
    "connection", "timeout", "server_fault", "server_busy", "server_other",
)

#: 状态过期秒数 —— 进程被杀 / 分支走丢时，界面不会永远挂一条红横幅。
EXPIRY_S = 900.0

#: `{session_key: record}`。`/clear` 清当前会话（会话级状态登记表里有它一条）。
_state: dict[str, dict] = {}


def delay_for(retry_index: int) -> int:
    """第 *retry_index* 次重试（1 起）之前等多少毫秒。越界取最后一项。"""
    n = max(1, int(retry_index or 1))
    return RETRY_DELAYS_MS[min(n, len(RETRY_DELAYS_MS)) - 1]


def _emit(type_: str, session_key: str, **data) -> None:
    """发事件。绝不抛（与 `EventBus.emit` 同一条判据）。"""
    try:
        from core.events import get_event_bus
        get_event_bus().emit(type_, session_key=session_key, **data)
    except Exception:                                           # noqa: BLE001
        pass


def step(state: dict, kind: str) -> tuple[str, int]:
    """下一步做什么、等多久。`state` 由调用方持有（一次调用一份）。

    返回 `(动作, 毫秒)`：
      · `("retry", d)`  —— 常规重试，横幅带「正在重试 n/max」
      · `("silent", d)` —— 常规次数用完，转入静默期（每 120 秒一次）
      · `("giveup", 0)` —— 静默次数也完了，按失败收尾
      · `("no_retry", 0)` —— 这一类不该重试（余额 / 参数 / 认证 / 内容…）
    """
    k = str(kind or "").strip().lower()
    if k not in RETRYABLE:
        return ("no_retry", 0)
    n = int(state.get("retry") or 0)
    if n < MAX_RETRIES:
        state["retry"] = n + 1
        return ("retry", delay_for(n + 1))
    s = int(state.get("silent") or 0)
    if s < SILENT_MAX:
        state["silent"] = s + 1
        return ("silent", SILENT_DELAY_MS)
    return ("giveup", 0)


def note(session_key: str, retry_index: int, *, kind: str = "",
         text: str = "", phase: str = "retry") -> int:
    """记一次"马上要重试"的状态并返回该睡多少毫秒。

    `retry_index` 是第几次重试（1 起）。状态里的 `retry` 就是它 ——
    界面显示「正在重试 {retry}/{max}」。`phase == "silent"` 时界面不显示次数，
    只留 `text` 那一句（用户已经知道"它在等上游恢复"）。
    """
    phase = str(phase or "retry").lower()
    delay = SILENT_DELAY_MS if phase == "silent" else delay_for(retry_index)
    key = str(session_key or "")
    if key:
        rec = {"retry": int(retry_index),
               "max": SILENT_MAX if phase == "silent" else MAX_RETRIES,
               "delay_ms": delay, "kind": str(kind or ""), "phase": phase,
               "text": str(text or "")[:300], "ts": time.time()}
        _state[key] = rec
        from core.events import EVT_LLM_RETRY
        _emit(EVT_LLM_RETRY, key, **rec)
    return delay


def fail(session_key: str, *, kind: str = "", text: str = "") -> None:
    """这一轮放弃了：横幅留着，但不带次数、也不再重试。

    与 `clear()` 的分工：`clear()` 是"好了"（横幅收掉、界面变绿）；
      `fail()` 是"结束了，但结果是坏的"——用户发下一条消息时由
      `clear()` 收掉（新一轮从绿条开始）。
    """
    key = str(session_key or "")
    if not key:
        return
    rec = {"retry": 0, "max": 0, "delay_ms": 0, "kind": str(kind or ""),
           "phase": "failed", "text": str(text or "")[:300], "ts": time.time()}
    _state[key] = rec
    from core.events import EVT_LLM_RETRY
    _emit(EVT_LLM_RETRY, key, **rec)


def clear(session_key: str) -> bool:
    """连接恢复 / 这一轮结束 —— 清状态并告诉界面"变绿"。回有没有清到东西。"""
    key = str(session_key or "")
    if not key or key not in _state:
        return False
    _state.pop(key, None)
    from core.events import EVT_LLM_RETRY_OK
    _emit(EVT_LLM_RETRY_OK, key)
    return True


def get(session_key: str) -> dict | None:
    """`/api/status` 读的那一份。过期 / 没有，`None`（不是空 dict）。"""
    key = str(session_key or "")
    rec = _state.get(key)
    if not rec:
        return None
    if time.time() - float(rec.get("ts") or 0) > EXPIRY_S:
        _state.pop(key, None)
        return None
    return dict(rec)


def reset(session_key: str) -> None:
    """`/clear` 走 `state_registry.purge_session` 调它（不发事件）。"""
    _state.pop(str(session_key or ""), None)


def reset_if_retrying(session_key: str) -> bool:
    """轮末收口：还在重试的收掉；`phase == "failed"` 留着。

    判据：重试中的状态是"过程"，留着它会让 `/api/status` 每 5 秒把红条
      复活；而失败收尾的状态是"结果"，用户得看得见它 —— 它在下一条消息
      发出时由 `clear()` 收掉（新一轮从绿条开始）。
    """
    key = str(session_key or "")
    rec = _state.get(key)
    if not rec or str(rec.get("phase") or "") == "failed":
        return False
    _state.pop(key, None)
    from core.events import EVT_LLM_RETRY_OK
    _emit(EVT_LLM_RETRY_OK, key)
    return True


def _cancelled(session_key: str) -> bool:
    """用户是不是已经按了「打断」。只读（不消费标志 —— 那是循环的事）。"""
    if not session_key:
        return False
    try:
        from core.cancellation import is_cancelled
        return bool(is_cancelled(session_key))
    except Exception:                                           # noqa: BLE001
        return False


def wait(session_key: str, delay_ms: int) -> bool:
    """可打断的同步等待。回 `True` = 睡满；`False` = 用户打断了。

    为什么切成小片：`time.sleep(60)` 期间按打断没有任何反应 ——
      重试循环不能只管睡。切片 250ms，最迟 0.25 秒就能掉头。
    """
    import time as _time
    left = max(0, int(delay_ms or 0))
    while left > 0:
        if _cancelled(session_key):
            return False
        step = min(250, left)
        _time.sleep(step / 1000.0)
        left -= step
    return not _cancelled(session_key)


async def wait_async(session_key: str, delay_ms: int) -> bool:
    """`:func:`wait` 的异步版（流式那条用 `asyncio.sleep`，别阻塞事件循环）。"""
    import asyncio
    left = max(0, int(delay_ms or 0))
    while left > 0:
        if _cancelled(session_key):
            return False
        step = min(250, left)
        await asyncio.sleep(step / 1000.0)
        left -= step
    return not _cancelled(session_key)
