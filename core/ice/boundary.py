"""边界助手：把 :mod:`core.ice` 的三个动词接到框架现有的数据结构上。

这一层只做形状变换（list/dict/str 的进出），不做任何判定 ——
判定全在 detect / rules / pseudonym 里。它存在的理由只有一个：
接线的地方应该只有"调用哪个助手"这一个决定，不该在那里再手写一遍遍历。

═══════════════════════════════════════════════════════════════════════
两个方向，两条铁律
═══════════════════════════════════════════════════════════════════════

**上去（:func:`pseudonymize_messages`）—— 只覆盖「读来的」。**

    工具结果（文件 / 表 / 邮件 / 网页）是"用户从没打算交出去"的那一份，换。
    用户自己打的不换（他已经在对话里交出去了，而且不换才能让"帮我算算这个身份证"
    这类推理任务照常可用），按 role 跳掉。

**下来（:func:`restore_tool_args` / :func:`restore_json_arguments` / :func:`restore_value`）——
什么都要还原。**

    还原必须在 JSON 解析之后做，绝不能在 JSON 文本上做。

    `arguments` 是一段序列化过的字符串。句柄里的 `【】`(U+3010/U+3011)
    会被 `ensure_ascii=True` 写成 `\\u3010` / `\\u3011` ——
    在原始 JSON 串里，那个精确形态根本不存在（解析出来才有）。
在文本层做替换会静默不生效，而症状看起来像"保护没开"。
所以先 `json.loads`，对值做还原，再 `json.dumps` 回去。

    顺带一条不能用错的快路：判"有没有句柄"不能用 `HANDLE_RE.search(原始串)`
    —— 上面那条正是它失效的原因。用 `"PII:" in raw`（纯 ASCII，两种写法都在）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

# 带图的工具回执是块列表 —— 这个助手把
# 「文本那一半」与「图块」分开，好让伪名化只动文本（图块里没有可替换的东西，
# 而删掉它就是把图弄丢）。
from core.image_input import split_text_and_media

from core.ice.guard import ICEGuard
from core.ice.handles import HANDLE_RE

#: 审批载荷的指纹。它是框架 ↔ 模型之间的协议消息，不是"读来的内容"
#: （包裹/截断都会让它静默失效）。我们不碰它 ——
#: 否则一个"文件名里恰好有 18 位连号"的删除请求，会让用户在审批面板上看到两个句柄。
#: 判据取得很松（首字符 `{` + 前缀里有 `pending_`）是有意的：
#: 宁可漏掉一个真载荷（那个值本来也在本地、在出口会被扫），也不要把正文误判成协议。
_PENDING_MARK = "pending_"


def _looks_like_protocol_payload(text: str) -> bool:
    head = text.lstrip()[:256]
    return head.startswith("{") and _PENDING_MARK in head


def pseudonymize_messages(
    messages: Sequence[Mapping[str, Any]],
    session_id: str,
    *,
    guard: ICEGuard,
) -> List[Dict[str, Any]]:
    """做一份「上模型的那一份」消息表。返回副本，绝不改传进来的那张表。

    覆盖范围：role == "tool"（读来的）与 role == "assistant"。
    `user` 不动（用户自己打的 PII 不替换 —— 实际确认这是刻意的）。

    `assistant` 也必须扫 —— 跳过它是一条静默的缝。

    「模型从来没拿到过真值，所以它自己写不出真值来（它只会搬句柄）」
    这条理由不成立：「一切落盘是真值」，框架把模型回复里的句柄
    还原成真值才落库，下一轮装载回来的那条 assistant 消息里就是真值。

    （探针，不是推理）：

        history = [{role: tool, ...}, {role: assistant,
                   content: "第三个人的身份证号是 440524188001010014。"}]
        pseudonymize_messages(history, sid)  ->  那一句原样发上游

于是 ICE 只在"值第一次进来"那一刻护住了它，**从模型把它写进回复的那一刻
    起就永久失效** —— 而没有任何一行日志。

    这个洞的形状：若不拦，真值重新进模型，前面的保护全白做。
    修法是「持映射，扫真值本身（不只扫句柄）」，把这层从「按工具」
    变成「按值」 —— 只按 role 扫与按值扫，在"回复还原落库"这条路上分道扬镳。

现在两边都做：role 覆盖 assistant，而"按值"由 `detect_pii` 本身承担
    （真值只要还是 PII 形状，扫到就换回句柄 —— 不依赖它当初是从哪进来的）。

    幂等（前缀缓存要这个）：同一句扫两次逐字节相同，有断言钉着。

    消息表里除了 `content` 还有 `tool_call_id` 等字段，一律逐字保留 ——
    少一个字段就是一条悬空 tool 结果，上游直接 400。
    """
    from core.ice import reveal as _reveal

    out: List[Dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role not in ("tool", "assistant"):
            out.append(dict(m))
            continue
        content = m.get("content")
        # 带图的工具回执是块列表。
        # 从前这里一句 `not isinstance(content, str)` 就整条跳过，
        # 那段文字里的身份证/银行卡没被伪名化，而它照样上模型
        # （静默漏，不是拒）。现在：文本那一半走伪名化，
        # 图块原样留着（像素上没有可替换的东西，而删掉它 = 把图弄丢）。
        text, media = split_text_and_media(content)
        if not text:
            out.append(dict(m))
            continue
        if _looks_like_protocol_payload(text):
            out.append(dict(m))
            continue
        new_text, _handles = guard.pseudonymize(text, session_id)
        if media:
            out.append({**m, "content": [{"type": "text", "text": new_text},
                                         *media]})
        else:
            out.append({**m, "content": new_text})
    return out


def pseudonymize_block(text: str, session_id: str, *, guard: ICEGuard) -> str:
    """框架自己拼进 prompt 的块（记忆索引 / 会话笔记 / 历史检索）过一遍。

    判据：它们进替换入口，因为它们的内容来自模型写过的东西
    —— 而模型写的时候可能已经从一个工具结果里抄了真值进去。
    技能索引 / `<git_status>` 那类不进（白扫）。
    """
    if not text:
        return text
    return guard.pseudonymize(text, session_id)[0]


def _restore_str(text: str, session_id: str, guard: ICEGuard) -> str:
    """一段文本里的句柄换回真值。不看开关（`ICEGuard.restore` 同一条）。

    刻意不导出：:mod:`core.ice.pseudonym` 已经有一个同名的 `restore_text`
    （签名不同：那个吃 `store`，这个是内部用）。两个同名函数挂到包级命名空间上，
    后导入的会静默盖掉前一个 —— 测试一调就是 `unexpected keyword argument`。
    这一层不需要那个名字，就别占它。
    """
    if not text or HANDLE_RE.search(text) is None:
        return text                       # 没有句柄，一个字节都不动（前缀友好）
    return guard.restore(text, session_id)


def restore_value(value: Any, session_id: str, *, guard: ICEGuard) -> Any:
    """递归还原一个 JSON 值里的每一个字符串。

    覆盖 str / list / dict；其余类型原样返回。副本，不改入参。
    """
    if isinstance(value, str):
        return _restore_str(value, session_id, guard)
    if isinstance(value, list):
        return [restore_value(v, session_id, guard=guard) for v in value]
    if isinstance(value, dict):
        return {k: restore_value(v, session_id, guard=guard) for k, v in value.items()}
    return value


def restore_tool_args(
    args: Mapping[str, Any],
    session_id: str,
    *,
    guard: ICEGuard,
) -> Dict[str, Any]:
    """一个已解析的工具参数字典 → 真值。

    这是还原的唯一正确落点 —— 见模块 docstring 那条铁律。
    """
    if not args:
        return dict(args)
    return {k: restore_value(v, session_id, guard=guard) for k, v in args.items()}


def restore_json_arguments(
    raw: str, session_id: str, *, guard: ICEGuard
) -> Optional[str]:
    """把一条 `tool_calls[].function.arguments`（JSON 串）还原成真值串。

    返回 `None`，没命中，整条原样别动（前缀缓存友好，同
    `_redact_call_for_history` 的"没命中，整条不动"）。

    为什么必须解析再序列化、不能在文本上替换：见模块 docstring 那条铁律
    —— 句柄在串里可能是 `\\ue000` 形态，文本层看它是"转义/损坏"，
    那样替换会静默不生效（`test_ice_boundary.py` 有一条专门的红过这个）。

    为什么不能不换就存进历史：不变量②「落盘全是真值」。
    句柄写进会话库 = 跨会话永远解不开的怪字符（映射是会话级的）。
    重新序列化是确定性的，同一条参数每次都长一样，前缀稳定。
    """
    if not raw or not isinstance(raw, str):
        return None
    if "PII:" not in raw:
        # 快路：`PII:` 是纯 ASCII，序列化器不会转义它，没有它就一定没有句柄。
        # 不能用 `HANDLE_RE.search(raw)` 当快路 —— `ensure_ascii=True` 会把
        #    `【】` 写成 `【`/`】`，那样精确形态在原始 JSON 串里根本不存在
        #    （活见鬼的形状：解析出来才有）。`PII:` 那三个字母两种情况都在。
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None                       # 坏 JSON 不归这里管（参数校验有它自己的路）
    if not isinstance(parsed, dict):
        return None
    restored = restore_tool_args(parsed, session_id, guard=guard)
    if restored == parsed:
        return None                       # 值没变，不动原串
    return json.dumps(restored, ensure_ascii=False)


def restore_tool_call_arguments(tool_calls, session_id: str, *, guard: ICEGuard) -> int:
    """就地把每一条 tool_call 的 arguments 还原成真值。返回改了几条。

    就地改是刻意的：`tool_calls` 里那些 dict 是派发与写进历史共用的
    （`tool_calls = response.tool_calls`），改一份，两条路都拿到真值。
    """
    n = 0
    for tc in tool_calls or ():
        fn = tc.get("function") if isinstance(tc, dict) else None
        if not isinstance(fn, dict):
            continue
        fixed = restore_json_arguments(fn.get("arguments"), session_id, guard=guard)
        if fixed is not None:
            fn["arguments"] = fixed
            n += 1
    return n


def restore_response(response, session_id: str, *, guard: ICEGuard) -> None:
    """就地把一条 provider 响应里的句柄换回真值（`content` + 每条 tool_call）。

    为什么就地而不改调用点：`content` / `tool_calls` 在下游被读很多次
    —— token 预算分支 · 终局文本 · 派发 · 写进历史 · 返回给 `post_process`。
    逐个改调用点，每漏一处就是一个静默的洞（而且漏的那一处往往正好是落盘）。
    改一处，全都对。

    必须在派发之前调用 —— 否则 handler 拿到的是句柄，工具拿着怪字符干活。
    """
    if response is None:
        return
    content = getattr(response, "content", None)
    if isinstance(content, str) and content:
        restored = _restore_str(content, session_id, guard)
        if restored != content:
            response.content = restored
    restore_tool_call_arguments(getattr(response, "tool_calls", None),
                                session_id, guard=guard)
