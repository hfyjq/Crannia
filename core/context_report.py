#!/usr/bin/env python3
"""会话构成面板 —— "本会话现在装了什么、各多大、和上次比变了多少"。

来源：「它的 context 信息是不是可以侧面反应有什么功能和这些功能占用大小」。

常见的 `/context` 视图是一份带价签的功能目录（`System prompt 1.8k` ·
`System tools 14.4k` · `Memory files 30.2k` · `Skills 3.8k` · `Messages 705.5k`，
且 `Skills` 下面逐条列出每个 skill 与它的 token 数）。在它之前只有三样零散的读数：
`scripts/measure_request.py`（离线、只量固定开销三件套）· `/api/status`
（只量缓存命中率）· 启动时那一行 `[threshold]`。
没有「本会话现在装了什么、各多大」的视图。

它同时补上"它长大是无声的"这个缺口 —— 一份文档从 16.5k 长到 29.5k，
只靠偶然跑一次 `/context` 才发现。有了一条面板，这件事就不再无声。

## 三条判据（都从已经翻过的车里来）

① 每格都要标来源 —— 这个数是量的还是估的、量的是哪一条链
   （同 `window_source`；"探针喂错了源"的成因就是这个没标）。
② 它先是读数，不是优化器 —— 不许顺手加"缩一缩"的冲动
   （订正读数只许用搬走来还）。
③ 「与上次比」那一列才是信号 —— 单看绝对数没感觉
   （16.5k 还是 29.5k，肉眼分不出来）。

## 落地时才冒出来的两件事（都是判据 ① 挣来的）

(a)「系统提示词」有两个大小。

| 谁 | 量的是 | 谁在用 |
|---|---|---|
| 压缩闸门 / `describe_thresholds` | `assemble_system_prompt("")` —— 不带记忆注入 | `MemoryManager._non_history_cost` |
| 真正发出去的 | `assemble_system_prompt(memory_context)` | `core/agent_loop.py` 每轮 |

记忆非空时，闸门量的那个数比真实请求小。而 `_non_history_cost` 的 docstring
写着"要和真正发出去的那份逐字一致" —— 那句话在记忆非空时不成立。
这里不替它下结论（判据 ②）：面板把两个数都列出来，差额单独一格叫
「记忆注入」。要不要据此改闸门，是另一件事 —— 那会动「一个仪器」那条线。

(b) `get_context_usage()` 的部件是 raw，而它的 `estimated_tokens` 是乘过因子的。
`estimate_request_tokens` 返回的 `system`/`tools`/`messages` 没乘因子，`total` 乘了
两者加不起来（factor 实际约 0.98，差 2% —— 小，但正是"两个数对不上也不报错"的形状）。
本模块一律报乘过因子的数（`round(raw × factor)`），这样各格加得起来、
且加出来就是 `estimated_tokens`（同一个量、同一个因子、一处应用）。
因子本身照常单列一格。

## 边界

- 不落盘。要的是"本会话内部长没长"；跨进程的文件级基准（某份指令文件多大）
  是另一条线的活，两件事别混。
- 不调 `pool.get_context()` —— 它会吃掉 `_pending_memory_inject`。
  记忆那一格要的是"上一次实际注入的"，
  由调度器记下来，面板只读。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 上一次读数，按 `session_id` 键 —— 「与上次比」那一列的唯一来源。
#: 只在进程内；`/clear` 时由 `MemoryManager.new_session` 显式清（见 `forget`）。
_last: Dict[str, Dict[str, int]] = {}

#: 上一次请求实际注入的记忆文本，按 `session_id` 键。
#: 记的是文本不是 token 数 —— token 要由仪器算，而仪器要 provider
#: （调用方交对象，不交数字）。这里只回答"发出去的是什么"。
#: 调度器在轮末记（它手上才有 `ctx.memory_context`），面板只读。
_last_memory: Dict[str, str] = {}

#: 上限：六个通道共用一个池，进程活着就一直攒，明写一个界，超了丢最早插入的那个。
#: （不做真 LRU：`remember` 命中已有键时不移动位置。对"上一个读数"这个用途，
#:  近似足够 —— 而精确 LRU 要额外的序，为一段读数付那个钱不值。）
_MAX_SESSIONS = 64

#: 格的名单 —— 一处定义（别在别处再抄一份分类）。
#: `(键, 中文标签, 来源说明, 是否某格的子集)`
_CELLS = (
    ("system", "系统提示词",
     "闸门口径 assemble_system_prompt(\"\")，**不含**记忆注入（见模块 docstring (a)）",
     None),
    ("memory", "会话笔记",
     "★ 这一格是**会话笔记**（模型自己上一轮的笔记），"
     "**不是长期记忆** —— 切流后 `get_context()` 只剩这一段（画像 / facts 已删）。"
     "长期记忆改走 **`<memory_index>`**（索引常驻），而它挂在最后一条 user 消息的副本上 "
     "⇒ ★ **算在「对话历史」那一格里，不在这里**。"
     "来源：带记忆的系统 − 不带记忆的系统（同一仪器两次应用）。"
     "⚠️ **没注入过就是 0，那是结论不是缺数**（`/clear` 之后、以及非首轮）",
     None),
    ("tools", "工具表",
     "本轮 wire 的 `tools=`（常驻 ∪ 已发现），实际，与压缩闸门同源同量纲（同一台仪器）",
     None),
    ("messages", "对话历史",
     "`messages` 全体（含**技能索引**与**记忆索引**那两份副本 —— 两者都挂在 user 消息尾巴上，"
     "所以算在这里）",
     None),
    ("skill_index", "技能索引",
     "★ `messages` 的**子集** —— 挂在最后一条 user 消息的副本上，**不参与加总**。"
     "⚠️ 这一格是**估**的（字符数 ÷ 4）—— 索引没有仪器读数，所以来源必须写明",
     "messages"),
)


def build_cells(parts: Dict[str, int], prev: Optional[Dict[str, int]]) -> List[dict]:
    """把各部分的 token 数折成一格一格。纯函数 —— 不读盘 / 不联网 / 不碰全局。

    *prev* 是同一会话的上一次读数。`None`，每一格的 `delta` 都是 `None` ——
    不是 `0`："没量到"与"量到了没变"是两件事。
    同理，某一格在上一次里不存在时，它的 `delta` 也是 `None`（不是"从 0 涨上来的"）。
    """
    out: List[dict] = []
    for key, label, source, subset_of in _CELLS:
        tokens = parts.get(key)
        p = None if prev is None else prev.get(key)
        cell: Dict[str, Any] = {
            "key": key,
            "label": label,
            "tokens": tokens,
            "source": source,
            "delta": None if (p is None or tokens is None) else tokens - p,
        }
        if subset_of:
            cell["subset_of"] = subset_of
        out.append(cell)
    return out


def remember(session_id: str, parts: Dict[str, int]) -> None:
    """记下这一次读数 —— 下一次调用就是"与它比"。"""
    if not session_id:
        return
    _last[session_id] = dict(parts)
    while len(_last) > _MAX_SESSIONS:
        _last.pop(next(iter(_last)))


def note_memory(session_id: str, memory_context: str) -> None:
    """记下这一轮实际注入的记忆文本 —— 面板里「记忆注入」那一格的输入。"""
    if not session_id:
        return
    _last_memory[session_id] = memory_context or ""
    while len(_last_memory) > _MAX_SESSIONS:
        _last_memory.pop(next(iter(_last_memory)))


def forget(session_id: str) -> None:
    """`/clear` 的那一份 —— purge 是不变量，不是一串地方。

    不清的代价是具体的：下一段对话第一次打开面板，看到的"变化量"是
    上一段对话长出来的。
    """
    _last.pop(session_id, None)
    _last_memory.pop(session_id, None)


def _factored(raw: Optional[int], factor: float) -> Optional[int]:
    if raw is None:
        return None
    return int(round(raw * factor))


def breakdown(
    mgr: Any,
    session_id: str,
    messages: List[dict],
    memory_context: Optional[str] = None,
    skill_stats: Optional[Dict[str, int]] = None,
) -> dict:
    """取数 → 折格 → 记快照。唯一的取数口（别在别处再抄一遍）。

    Args:
        mgr: `MemoryManager`。取数只用它的公开面（`get_context_usage` /
            `measure_system`），不碰私有属性。
        session_id: 已经解析过的会话 id（调用方负责解析，见 `SessionPool`）。
        messages: 当前会话的消息列表。
        memory_context: 上一次请求实际注入的那份记忆。
            `None` = 用 `note_memory` 记下来的那份（生产走这条路）；
            显式给值 = 覆盖它（测试与一次性诊断用）。
            不在这里现算 —— 现算要走 `pool.get_context()`，
            而它会吃掉 `_pending_memory_inject`（见模块 docstring 的边界那一段）。
        skill_stats: `core.skill_index.skill_index_stats()` 的结果；不给就现取。

    Returns:
        `{"cells": [...], "estimated_tokens": …, "factor": …,
          "window": …, "window_source": …, "pct": …,
          "threshold_tokens": …, "armed": …, "source": …}`
    """
    usage = mgr.get_context_usage(messages, session_id=session_id)
    factor = float(usage.get("factor") or 1.0)

    if memory_context is None:
        memory_context = _last_memory.get(session_id, "")

    # 记忆那一格：带记忆的系统 − 不带记忆的系统。同一个仪器，两次应用
    #   （不许在别处先量一遍再乘）。
    mem_raw = 0
    if memory_context:
        with_mem = mgr.measure_system(memory_context, session_id=session_id)
        mem_raw = max(0, int(with_mem) - int(usage.get("system_tokens") or 0))

    if skill_stats is None:
        try:
            from core.skill_index import skill_index_stats
            skill_stats = skill_index_stats()
        except Exception:
            skill_stats = {"entries": 0, "chars": 0}

    parts = {
        "system": _factored(usage.get("system_tokens"), factor),
        "memory": _factored(mem_raw, factor),
        "tools": _factored(usage.get("tool_tokens"), factor),
        "messages": _factored(usage.get("message_tokens"), factor),
        # 索引的 token 数没有仪器读数 —— 只能估（字符数 ÷ 4），
        #    所以 `_CELLS` 里那一格的来源说明写明了"这是估的"（判据 ①）。
        "skill_index": int(skill_stats.get("chars", 0)) // 4,
    }

    prev = _last.get(session_id)
    cells = build_cells(parts, prev)
    remember(session_id, {k: v for k, v in parts.items() if v is not None})

    return {
        "cells": cells,
        "estimated_tokens": usage.get("estimated_tokens"),
        "factor": factor,
        "window": usage.get("context_window"),
        "window_source": usage.get("window_source"),
        "pct": usage.get("percentage"),
        "threshold_tokens": usage.get("threshold_tokens"),
        "threshold_pct": usage.get("threshold_pct"),
        "armed": usage.get("armed"),
        "model": getattr(getattr(mgr, "_provider", None), "_model", "?"),
        "session_id": session_id,
        "messages": len(messages),
        "skill_index_stats": dict(skill_stats),
        "source": ("本轮 wire 的 system / tools / messages，"
                   "与压缩闸门同一个仪器）· 只读，不改任何东西"),
    }
