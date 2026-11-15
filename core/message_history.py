# -*- coding: utf-8 -*-
"""消息历史的平衡性判据。

纯函数：只读一个 `list[dict]`，不碰 IO、不认识会话、不碰 provider。

## 一、为什么要它

### ① 不平衡的历史发回上游是 400

`tool_calls` 一旦进了 assistant 消息，就欠一个 `role: tool` 结果。
缺了就是悬空引用，严格的 API 直接 400（*"tool_call id without response"*）。
`core/agent_loop.py::_fill_skipped_tool_calls` 就是为这个存在的 ——
但它是"事后补"，而没有一个地方能在补之前说清"哪里不平衡"。

### ② fork 必须先知道"能剪到哪"

子 agent 的上下文走 fork（对齐缓存友好的做法）。
而 fork 的剪点不是随便切的 —— 判据是：

    种子止于最后一个 turn/end：当前工具调用轮不平衡，不能作为合法子会话回放。

    一个 fork 提供的是平衡的已完成轮前缀 …… 必须从 seq 0 连续、
    只含无损 JSON、且不含未闭合的轮/步或悬空工具调用。

没有这个判据就切，会切出一个损坏的子会话 —— 而它的失败形态是
"子 agent 一开口就 400"，看起来像子 agent 自己的问题。

## 二、 它比只查集合成员多挡三种

只查"发了没答"（集合成员关系）不够：

| # | 形状 | 为什么也要挡 | 集合视角 |
|---|---|---|---|
| ① | 发了没答（悬空 `tool_call`） | 上游 400 | ✅ 有 |
| ② | 答了没发（孤儿 `role: tool`） | 同样 400（*"role 'tool' must be a response to a preceding message with 'tool_calls'"*） | ❌ 漏 |
| ③ | 顺序反了（结果排在调用之前） | 集合成员一样，但顺序非法 | ❌ 漏（它只比集合） |
| ④ | 同一个 id 发了两次 | 到底答的是哪一个？歧义 | ❌ 漏 |

四种的共同点：它们在"集合视角"下可能完全正常。
拿"出现过没有"当判据，而不是拿"结构对不对"，就会漏掉后三种。

## 三、 判据是结构，不是内容

本模块不看消息文本、不判对错、不认识 `kind`。
它只回答一个问题：这段历史能不能被原样重放。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["BalanceReport", "check", "balanced_prefix_end", "is_balanced"]


@dataclass(frozen=True)
class BalanceReport:
    """不平衡的四种形状，各自带"在哪"（下标 + id），不只是一句"不平衡"。"""

    #: 发了、但没有任何 `role: tool` 回应的 `tool_call` id（保序）
    unanswered: tuple = ()
    #: 有 `role: tool`、但找不到对应调用的消息下标
    orphan_results: tuple = ()
    #: 同一个 `tool_call` id 被发了不止一次的 id
    duplicated: tuple = ()
    # 这里没有单独的 `out_of_order` 字段 —— 工具结果只会跟已经发出的 id
    # 比对，"结果排在调用之前" 必然先落到 `orphan_results`（那个 id 还没发过，
    # 它就是个孤儿）。单独开一个字段永远不会被 `check()` 填上。
    # 死字段比没有更糟 —— 它让读的人以为"这一种有专门的判据"，从而不再去问
    # `orphan_results` 到底覆盖了什么。
    #
    # `balanced_prefix_end` 里那条乱序判断不是同一件事 ——
    # 它判的是"能不能剪"，不是"错在哪"。

    @property
    def balanced(self) -> bool:
        return not (self.unanswered or self.orphan_results
                    or self.duplicated)

    @property
    def problems(self) -> int:
        return (len(self.unanswered) + len(self.orphan_results)
                + len(self.duplicated))

    def explain(self) -> str:
        """一句给人看的话。空字符串 = 平衡（调用方靠它判"要不要出声"）。"""
        if self.balanced:
            return ""
        bits: list[str] = []
        if self.unanswered:
            bits.append(f"{len(self.unanswered)} 个 tool_call 没有结果"
                        f"（{', '.join(str(x) for x in self.unanswered[:3])}…）")
        if self.orphan_results:
            bits.append(f"{len(self.orphan_results)} 个 tool 结果没有对应的调用"
                        f"（下标 {list(self.orphan_results[:3])}）")
        if self.duplicated:
            bits.append(f"重复的 tool_call id：{list(self.duplicated[:3])}")
        return "；".join(bits)


def _call_ids(m: dict) -> list:
    """从一条 assistant 消息里取出 `tool_call` 的 id 列表。坏形状一律当空。

    不能写 `for tc in (m.get("tool_calls") or [])` —— 当 `tool_calls` 是
    字符串时（`"abc"` 是真值），它会逐字符迭代，然后 `'a'.get("id")` 直接
    `AttributeError`。

一个"判平衡"的工具因为输入形状不对而崩掉，是最坏的失败方向：
    调用方（`agent_loop` 里那一句）拿不到判据，只会看到一条 traceback，
    而 traceback 会掩盖"历史本来就坏了"这件事。

坏形状不抛，当成"没有调用"返回 —— 那样它会落到"孤儿
    `tool` 结果"那条判据上（若真有对应结果），而不是把整条路炸掉。
    """
    tcs = m.get("tool_calls")
    if not isinstance(tcs, (list, tuple)):
        return []
    return [tc.get("id") for tc in tcs if isinstance(tc, dict)]


def check(messages) -> BalanceReport:
    """扫一遍 *messages*，返回四种不平衡形状。纯函数、不抛异常。

    不校验上游会不会真的 400 —— 那取决于具体 API 的严格程度。
    这里判的是更硬的一件事：这段历史结构上自洽吗。
    自洽是 400 不发生的必要条件（不是充分条件，但另一半天知道）。
    """
    if not messages:
        return BalanceReport()

    #: 已发出、尚未回应的 id（保序：list 而不是 set —— 顺序本身是判据之一）
    pending: list = []
    answered: set = set()
    issued_ever: dict = {}      # id -> 首次发出的下标
    seen_ids: set = set()

    unanswered: list = []
    orphan: list = []
    dup: list = []

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role == "assistant":
            for cid in _call_ids(m):
                if cid in seen_ids:
                    if cid not in dup:
                        dup.append(cid)
                    continue
                seen_ids.add(cid)
                issued_ever[cid] = i
                pending.append(cid)

        elif role == "tool":
            cid = m.get("tool_call_id")
            if cid is None:
                # 没有 id 的 tool 结果：对应不上任何调用
                orphan.append(i)
                continue
            if cid not in issued_ever:
                # ② 答了没发 —— "结果排在调用之前"也落在这里
                #（那个 id 还没发过，它就是个孤儿）。所以没有单独的乱序字段。
                orphan.append(i)
                continue
            if cid in answered:
                # ④ 同一个 id 答了两次 —— 第二次也是孤儿
                orphan.append(i)
                continue
            answered.add(cid)
            if cid in pending:
                pending.remove(cid)

    # ① 发了没答
    unanswered = list(pending)

    return BalanceReport(
        unanswered=tuple(unanswered),
        orphan_results=tuple(orphan),
        duplicated=tuple(dup),
    )


def is_balanced(messages) -> bool:
    """整段历史自不自洽。"""
    return check(messages).balanced


def balanced_prefix_end(messages) -> int:
    """能安全剪到哪 —— 最大的 `i`，使 `messages[:i]` 是平衡的。

    这是 fork 的剪点：`messages[:balanced_prefix_end(msgs)]` 可以原样
    喂给子会话；再往后就不保证了。

    算法就是"最后一个平衡边界"：
      · 每读完一条消息，若当前没有未回应的调用，`i+1` 就是一个候选边界
      · 但一旦出现过孤儿 / 重复 / 乱序，从那之后全部作废
        （那些形状修不好，只能整段丢）

    返回 `0` 表示一条都不能剪（第一条就坏，或列表为空）。
    调用方拿它当"fork 不可用"的信号，不要当"剪 0 条"。
    """
    if not messages:
        return 0

    pending: list = []
    seen_ids: set = set()
    last_ok = 0
    tainted = False

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            tainted = True
            continue
        role = m.get("role")

        if role == "assistant":
            for cid in _call_ids(m):
                if cid in seen_ids:
                    tainted = True        # 重复 id：此后不可信
                    continue
                seen_ids.add(cid)
                pending.append(cid)
        elif role == "tool":
            cid = m.get("tool_call_id")
            if cid is None or cid not in seen_ids or cid not in pending:
                tainted = True            # 孤儿 / 乱序 / 重复答
                continue
            pending.remove(cid)

        if not tainted and not pending:
            last_ok = i + 1

    return last_ok
