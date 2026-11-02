"""通道「最后一次真的收到消息」的时间戳 —— 一个读数，不是业务状态。

## 它治的是什么

Telegram（2026-10-25）：轮询循环停了，而 `/api/channel/status` 仍然报
`running: true` —— 那个 flag 只在 `_run` 的 `finally` 里才翻。
「在跑」与「真的在收」长得一模一样：用户在手机上发消息没反应，
而 UI 绿框亮着、状态端点说 true、日志一片安静。三种信号里没有一种看得出问题
（唯一露馅的是 Telegram 那边的 `pending_update_count`）。

这里记每一条真正进了管线的通道事件（只记时间，不记内容）。
状态端点把它换算成 `seconds_since`，让「安静」这件事看得见。

## 它不是心跳

一个通道可以完全健康但确实没人说话（夜里、周末）。所以读数要**跟"那条
通道现在有没有未取的队列"一起看** —— 现在只有 Telegram 有那个数
（`getWebhookInfo.pending_update_count`）。别把这个时间戳单独当故障判据。

## 为什么挂在 `scheduler.execute()`

六个通道共用同一个 `PipelineScheduler` 单例，一处就能覆盖全部，
不需要每个通道各写一行 —— 一个口径一个地方。而且它记的是
"进了管线"，正好是我们关心的那件事。
"""

from __future__ import annotations

import threading
import time
from typing import Dict

_lock = threading.Lock()

#: platform → (monotonic 秒, epoch 秒)
_last: Dict[str, tuple[float, float]] = {}


def note(platform: str) -> None:
    """记一次"这条通道有一条事件进管线了"。不抛、不做 I/O。"""
    key = str(platform or "")
    if not key:
        return
    now_wall = time.time()
    with _lock:
        _last[key] = (time.monotonic(), now_wall)


def snapshot() -> dict[str, dict[str, float]]:
    """`{platform: {"seconds_since": …, "at": <epoch>}}` —— 给状态端点用。

    没有记录的平台不出现（"从来没收到过" ≠ "刚刚收到过"）。
    """
    now = time.monotonic()
    with _lock:
        items = list(_last.items())
    return {
        key: {"seconds_since": round(max(0.0, now - mono), 1), "at": wall}
        for key, (mono, wall) in items
    }


def clear() -> None:
    """清空 —— 只给测试用（进程级诊断，`/clear` 不该动它）。"""
    with _lock:
        _last.clear()
