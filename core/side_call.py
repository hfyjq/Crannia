"""受限子调用 —— `/btw` 与 `/recap` 共用的那一次请求。

`/btw`（问一句题外话、不打断主对话）与 `/recap`（短暂回顾最近的对话）
要的东西是同一个：吃主对话的缓存前缀 + 只回一段话 + 答案不进主对话。
`/fork` 不同族（那是会话分叉），所以这台机器只管前两个。

三条硬约束：

1. 不改 thinking 配置 —— thinking 是 API 缓存键的一部分，与主线程分叉会打断
   提示缓存。跟着主线程走（取会话上冻结的那个档），不另给一个值。
2. 不写缓存 —— 之后没有请求会复用这段后缀。
   我们没有对应的旋钮：DeepSeek 是自动前缀缓存（客户端无处声明"别写"），
   而且它的计费没有 Anthropic 那种"写缓存加价"。这一条记在案、不实现。
3. 工具不摘、在权限层拒 + 最多一轮（所有工具调用被拒）。

我们把 ③ 落到最简：一次 `provider.chat`，根本没有循环。

```
没有循环，没有派发，没有审批，没有事件噪声
```

而工具照发（`tools` 在缓存前缀里排在 system 之前，摘掉它 = 整段前缀作废，
这就是「把 Agent 工具留在工具池里，以保持缓存定义逐字节一致」的同一条）。
模型想调也调不到 —— 一次调用打完就结束了。
这正是必须的：子 agent 里一碰审批就挂起，
而挂起需要"回答变成新的一轮"，题外话没有那一轮。

system / tools 取自请求快照，不是现算 —— 有一条硬理由：
现算要走 `pool.get_context()`，而它会吃掉 `_pending_memory_inject`
下一轮的记忆注入少一次且不出声。
快照是只读的，取它就等于"把刚才真发出去的那条再发一次，尾巴上加一句"。
（快照不属于本会话时退回现算：`assemble_system_prompt("")` + `get_tool_schemas_active`
—— 与循环用的同一对函数，差别只在有没有记忆注入那段。）

空回答不许静默：流式响应里每个内容块各是一条 assistant 消息；
取"第一条 assistant"会抓到 thinking-only 那块、找不到文本、返回 null →
用户看到"没有回应"。
两条结论：取最后那段文本、不取第一块；空回答必须有一句自己的话
（不许静默变成"没有回应"）。
"""

from __future__ import annotations

#: 回答的显示上限。`/btw` 可以展开讲（用户 2026-11-02 明确要放宽），
#: 所以给到 4000；`/recap` 本来就只有几句，够不到这个数。
#: 超了截断并留记号，不静默丢（"先截断后包裹"的记号规矩：切了就要说切了）。
MAX_CHARS = 4000


BTW_DIRECTIVE = (
    "[题外话] 用户临时问了一句与当前任务无关的话。直接回答；可以展开讲清楚，\n"
    "但不要跑题、不要开始做当前任务的事。\n"
    "你这一问不能调用任何工具（没有那一轮）：不要制定计划、不要开始做事。\n"
    "如果回答确实需要工具（读文件、查数据、跑命令……），不要猜也不要编：\n"
    "用一句话说清需要什么，并请用户回到主对话里问这个问题。\n"
    "问题：%s"
)

#: `/recap` 的语义：用户离开又回来，用 1-3 句短话说明到哪了、
#: 别写状态报告 —— 形态与语义见下面的定义。
RECAP_DIRECTIVE = (
    "[回顾] 用户想知道「刚才我们到哪了」。用 1-3 句话说明这段对话做到哪一步、"
    "下一步是什么。\n"
    "直接说结论：不要汇报状态、不要复述过程、不要列清单、不要调用工具。"
)


def system_and_tools(provider, session_id: str):
    """这一次请求的 `(system, tools)` —— 与主对话逐字节同前缀是唯一判据。

    ① 请求快照属于本会话，用它（那是"刚发出去的那条"，
       只读、无副作用）；
    ② 否则现算 —— 用的是循环同一对函数，差别只在记忆注入那段
       （`get_context` 有副作用，这里不许调它）。
    """
    from core import request_snapshot

    snap = request_snapshot.get_current()
    if snap is not None and snap.session_id == session_id and snap.system:
        return snap.system, list(snap.tools)

    from bus.registry import get_tool_schemas_active
    from core.prompt_assembler import assemble_system_prompt, model_name_of

    return (assemble_system_prompt("", model_name=model_name_of(provider)),
            get_tool_schemas_active(session_id))


def thinking_for(session_id: str):
    """这一问的思考模式 —— 与主对话同一个档，不是另给一个值。

    判据：thinking 是 API 缓存键的一部分，与主线程分叉会打断
    提示缓存，"不覆盖"的意思是跟着主线程走，
    不是"什么都不传"。取的是会话上冻结的那个档（不每请求重算）。
    """
    from core.tiers import current_spec

    return current_spec(session_id).thinking


def run(provider, messages, *, session_id: str, directive: str, header: str,
        tag: str) -> str:
    """发一次请求，返回给用户看的那段完整回复（含头一行与各种兜底）。

    三种结局各有各的话（都不静默）：
      · 拿到文本，`header + 文本`
      · 模型只回了工具调用 / 空白，说清"题外话不给工具"，并给路线
      · 抛异常，原样带出类型与消息（同 `core/shell.py` 那条"不吞错"）
    """
    system, tools = system_and_tools(provider, session_id)
    payload = list(messages) + [{"role": "user", "content": directive}]

    try:
        resp = provider.chat(payload, tools, system=system,
                             thinking=thinking_for(session_id))
    except Exception as exc:                       # noqa: BLE001 —— 不吞错：原样报
        print(f"[{tag}] 失败：{type(exc).__name__}: {exc}")
        return f"#{tag}-回复异常"

    text = (getattr(resp, "content", "") or "").strip()
    if not text:
        # 静默返回"没有回应"的原形就是这个分支 ——
        #   用户看不出是模型没说话还是框架坏了。
        print(f"[{tag}] 空回答（模型只回了工具调用或空白）")
        return f"#{tag}-模型没有给出回复，可在主对话重试"

    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + " …（截断）"
    print(f"[{tag}] 回了 {len(text)} 字符")
    return f"{header}\n{text}"
