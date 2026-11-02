"""超长工具输出的确定性收缩（不调模型、不动库）。

## 它解决什么

上下文吃紧时，最先吃空间、又最先"用完就没用"的是旧的工具输出
（读过的文件、跑过的命令、抓过的网页）。把它们的中间段换成一句标记，
通常足以把占用拉回线下，而不需要一次摘要调用。

## 判据

1. 只动 `role="tool"` 的内容 —— `user` / `assistant` 一个字节都不改。
   用户说过的话、模型给过的回答是劳动成果。
2. 最近 `keep_recent` 条工具结果不动 —— 它们可能正是模型下一步要引用的。
3. 只有超过 `threshold_chars` 的才缩；缩成 `头 + 标记 + 尾`。
4. 返回新列表，调用方自己决定是否替换 —— 本模块不修改入参。
   （持久化那份历史不该被"这一次请求"的形状改写。）
5. 幂等：已经带标记的内容不再缩（否则每轮都截一点，越截越短）。

## 名字

本仓自己的叫法：`trim` / `output_trim`。刻意不与常见实现里的 `microcompact` /
`prune` 同名 —— 两者行为不同（一个换整条内容、一个留头尾），重名会让后来者
把两套实现当成一套。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

__all__ = ["TRIM_MARK", "trim_tool_outputs", "stats_of"]

#: 中间段被换成的标记。放在内容里（而不是别处），模型读到的就是它。
TRIM_MARK = "\n\n[... 中间部分已裁剪；原始输出在工具结果落盘里 ...]\n\n"

#: 默认值：超过 8 KiB 才缩，留头 4 KiB、尾 1 KiB。
DEFAULT_THRESHOLD_CHARS = 8192
DEFAULT_HEAD_CHARS = 4096
DEFAULT_TAIL_CHARS = 1024

#: 默认保住最近 6 条工具结果。它按"条"计，与摘要那边的"轮"是两件事。
DEFAULT_KEEP_RECENT = 6


def _is_tool(msg: Any) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "tool"


def _text_of(msg: Dict) -> str | None:
    """只处理纯文本内容；多模态块原样放过（缩它会把结构弄坏）。"""
    c = msg.get("content")
    return c if isinstance(c, str) else None


def trim_tool_outputs(
    messages: List[Dict],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> Tuple[List[Dict], Dict[str, int]]:
    """返回 `(新消息列表, 读数)`；入参不被修改。

    读数：`trimmed`（缩了几条）· `freed`（省了几个字符）· `kept`（最近保留区里几条）。
    """
    stats = {"trimmed": 0, "freed": 0, "kept": 0}
    if not messages:
        return messages, stats

    idx = [i for i, m in enumerate(messages) if _is_tool(m)]
    keep = max(0, int(keep_recent))
    protected = set(idx[-keep:]) if keep else set()
    stats["kept"] = len(protected)

    out: List[Dict] = list(messages)
    for i in idx:
        if i in protected:
            continue
        text = _text_of(messages[i])
        if text is None or len(text) <= threshold_chars or TRIM_MARK in text:
            continue
        head = text[:head_chars]
        tail = text[-tail_chars:] if tail_chars > 0 else ""
        new = head + TRIM_MARK + tail
        if len(new) >= len(text):
            continue                       # 缩不动就不缩（别让它变长）
        out[i] = {**messages[i], "content": new}
        stats["trimmed"] += 1
        stats["freed"] += len(text) - len(new)
    return out, stats


def stats_of(messages: List[Dict]) -> Dict[str, int]:
    """只看不缩：当前有多少条工具输出会被缩、能省多少字符。

    调用方拿它做"这一次到底值不值得缩"的判断（省得太少就不动）。
    """
    _, st = trim_tool_outputs(messages)
    return st
