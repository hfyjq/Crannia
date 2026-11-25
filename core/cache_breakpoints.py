"""显式缓存断点 —— 按 cache_format 分形状，默认关闭。

## 为什么默认关

表里的 `explicit_enabled` 目前四家全是 false（走隐式）。这一模块把"要打点"的
能力备好，但没有打开任何一家的开关 —— 真机读数（清单第 6 步）确认收益之前，
主动打点只带来风险（形状错就 400）。所以这里只有纯函数与断言用的投影，
生产路径由 `explicit_enabled` 一票否决。

## 两种形状

- 消息级（`anthropic` / `bedrock` 那一路）：标记挂在消息对象上，
  `{"cache_control": {"type": "ephemeral"}}`；
- 内容块级（其余，含 `alibaba`）：标记挂在块对象上，消息本身不动。
  这一族用字符串 content 时，要先把它变成块列表才有地方挂标记。

## 打点位置

只打两处，都在稳定前缀上：

1. system 那一块（每轮逐字相同，是最值钱的断点）；
2. 最近一条 tool-result 消息（工具结果之后历史就开始变了，断在这里损失最小）。

上限 4 个，实际只打 2 个 —— 留余量给将来，不为凑数多打。

## 不改入参

返回新列表 / 新对象，`messages` 本体一个字节不动（它还要落库、也是缓存前缀）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

Marker = dict[str, object]
_EPHEMERAL: Marker = {"type": "ephemeral"}


def place_breakpoints(
    messages: list[dict],
    *,
    cache_format: str,
    max_breakpoints: int,
    system_present: bool = True,
) -> list[dict]:
    """按形状在稳定前缀上打点。不合法的参数直接返回原列表（不改）。"""
    if max_breakpoints <= 0 or cache_format not in ("anthropic", "alibaba"):
        return list(messages)
    if not messages:
        return list(messages)

    out = deepcopy(messages)

    def _last_tool_result() -> int:
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "tool":
                return i
        return -1

    hit = _last_tool_result()
    if hit < 0:
        return out

    if cache_format == "anthropic":
        # 消息级：标记挂在消息对象上；system 由调用方单独处理。
        out[hit]["cache_control"] = dict(_EPHEMERAL)
        return out

    # 内容块级：标记挂在块上；字符串 content 先升成块列表。
    msg = out[hit]
    content = msg.get("content")
    if isinstance(content, str):
        blocks: list[dict] = [{"type": "text", "text": content,
                               "cache_control": dict(_EPHEMERAL)}]
    elif isinstance(content, list) and content:
        blocks = []
        for i, block in enumerate(content):
            b = dict(block) if isinstance(block, dict) else {
                "type": "text", "text": str(block)}
            if i == len(content) - 1:
                b["cache_control"] = dict(_EPHEMERAL)
            blocks.append(b)
    else:
        return out
    msg["content"] = blocks
    return out


def system_breakpoint(cache_format: str) -> Marker | None:
    """system 那块的标记（由调用方按形状挂到 system 对象 / 末尾块）。"""
    if cache_format not in ("anthropic", "alibaba"):
        return None
    return dict(_EPHEMERAL)


def shape_of(cache_format: str) -> Literal["message", "block", "none"]:
    """这一族把标记挂在哪一层。给 wire 断言用。"""
    if cache_format == "anthropic":
        return "message"
    if cache_format == "alibaba":
        return "block"
    return "none"
