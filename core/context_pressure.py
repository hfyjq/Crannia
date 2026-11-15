"""上下文压力：先收缩旧工具输出，再提醒模型收尾（不调模型）。

## 两个水位

| 水位 | 动作 |
|---|---|
| `WARN_RATIO`（窗口的 80%） | ① 把旧的工具输出缩成头尾；② 在这次请求的副本上附一句收尾提醒 |
| `FORCE_RATIO`（95%） | 留给调用方：到这一档就不该再等模型自己收尾了 |

## 判据

1. 只改这一次发出去的副本 —— 持久化的历史不动（`output_trim` 本来就返回新列表）。
2. 先缩、再提醒：缩完可能已经不用提醒了（`ratio` 按缩后的量重算）。
3. 读不到窗口，什么都不做（不猜分母）。
4. 提醒只在超过水位时出现；它一变，前缀就跟着变（一次缓存未命中），
   所以不常驻。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from core.i18n import t as tr
from core.image_input import append_text_block
from core.memory.output_trim import trim_tool_outputs

__all__ = [
    "WARN_RATIO", "FORCE_RATIO", "ratio_of", "notice_text",
    "append_notice", "trim_for_pressure",
    "note_ratio", "armed", "request_compaction", "take_compaction_request",
    "note_trim", "take_trim", "forget_session",
]

#: 80%：开始收缩 + 提醒。
WARN_RATIO = 0.80
#: 95%：调用方应当强制压缩（本模块只给常量与读数）。
FORCE_RATIO = 0.95


def ratio_of(total: int, window: int | None) -> float:
    """占用比例；窗口读不到，`-1`（调用方据此"什么都不做"）。"""
    if not window or window <= 0 or total < 0:
        return -1.0
    return total / float(window)


def notice_text() -> str:
    """收尾提醒的正文（界面语言）。一句，不解释机制。"""
    return tr(
        "上下文即将达到自动压缩线：请尽快收尾，把关键状态写进笔记或交付物。"
        "到 95% 仍未收尾会被强制压缩。"
    )


def append_notice(messages: List[dict], text: str = "") -> List[dict]:
    """把提醒附在最后一条 user 消息后面，返回新列表（找不到 user 就原样）。"""
    body = text or notice_text()
    if not body:
        return messages
    block = f"\n\n<context_notice>\n{body}\n</context_notice>"
    return append_text_block(messages, block)


def trim_for_pressure(
    messages: List[dict],
    provider,
    *,
    system=None,
    tools=None,
) -> Tuple[List[dict], Dict[str, float]]:
    """按压力收缩旧工具输出。返回 `(要发出去的那份, 读数)`。

    读数：`ratio_before` / `ratio_after` / `trimmed` / `freed`。
    入参不改；没到水位就原样返回同一个对象。
    """
    out = {"ratio_before": -1.0, "ratio_after": -1.0, "trimmed": 0, "freed": 0}
    try:
        window = provider.context_window
        total = provider.estimate_request_tokens(
            messages, system=system, tools=tools)["total"]
    except Exception:                                          # noqa: BLE001
        return messages, out
    r0 = ratio_of(total, window)
    out["ratio_before"] = r0
    out["ratio_after"] = r0
    if r0 < WARN_RATIO:
        return messages, out

    trimmed, st = trim_tool_outputs(messages)
    out["trimmed"], out["freed"] = st["trimmed"], st["freed"]
    if not st["trimmed"]:
        return messages, out
    try:
        after = provider.estimate_request_tokens(
            trimmed, system=system, tools=tools)["total"]
        out["ratio_after"] = ratio_of(after, window)
    except Exception:                                          # noqa: BLE001
        pass
    return trimmed, out


# 会话级的两个格子（压力读数 / 模型的压缩请求）
#
# 它们决定 `request_compaction` 发不发给这条会话（见
#   `bus/registry._session_extra_tools`），必须按会话存、`/clear` 清。
#   登记在 `core/state_registry.py`；清理由 `purge_session` 走 `forget_session`。

_STATE: Dict[str, Dict[str, object]] = {}


def note_trim(session_id: str, reason: str) -> None:
    """记下"这一轮消息表为什么变短了"（没裁过就没有这一格）。

    判据：`context.state` 只报"多少"，于是上下文突然掉一半时读日志的人
    只能猜是谁动的。裁的三条路各报各的名字（`cold_window` / `reload` /
    `pressure`），下一次跳变在事件流里直接读得出来。
    """
    if not session_id:
        return
    slot = _STATE.setdefault(session_id, {"ratio": -1.0, "requested": False})
    slot["trim"] = str(reason or "")


def take_trim(session_id: str) -> str:
    """取走并清掉那个标记（每次只报一次，与 `take_compaction_request` 同形）。"""
    slot = _STATE.get(session_id or "")
    if not slot:
        return ""
    return str(slot.pop("trim", "") or "")


def note_ratio(session_id: str, ratio: float) -> None:
    """记下这条会话这一次请求的占用比例（供下一步的工具暴露用）。

    有一步的滞后：工具表在本步请求之前就定了。可以接受 ——
    提醒从 80% 开始，晚一步拿到工具不改变"来不及收尾"这件事。
    """
    if not session_id:
        return
    slot = _STATE.setdefault(session_id, {"ratio": -1.0, "requested": False})
    slot["ratio"] = float(ratio)


def armed(session_id: str) -> bool:
    """这条会话是否处在"可以主动请求压缩"的窗口里（≥ `WARN_RATIO`）。"""
    slot = _STATE.get(session_id or "")
    if not slot:
        return False
    r = float(slot.get("ratio", -1.0) or -1.0)
    return r >= WARN_RATIO


def request_compaction(session_id: str) -> bool:
    """模型主动请求压缩（工具处理器的落点）。返回是否受理。"""
    if not armed(session_id):
        return False
    slot = _STATE.setdefault(session_id, {"ratio": -1.0, "requested": False})
    slot["requested"] = True
    return True


def take_compaction_request(session_id: str) -> bool:
    """取走并清掉"模型请求过压缩"那一个标记（每次请求只消费一次）。"""
    slot = _STATE.get(session_id or "")
    if not slot or not slot.get("requested"):
        return False
    slot["requested"] = False
    return True


def forget_session(session_id: str) -> None:
    """`/clear` 的清理口（登记在 `state_registry`）。"""
    _STATE.pop(str(session_id or ""), None)
