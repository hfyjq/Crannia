# -*- coding: utf-8 -*-
"""`pii_reveal` —— 显式放行那颗值。

## 为什么会有这个东西

因为先有一条静默的缝。

「一切落盘是真值」，框架把模型回复里的句柄还原成真值才落库
下一轮装载回来的那条 assistant 消息里就是真值。若
`boundary.pseudonymize_messages` 只扫 `role == "tool"`、assistant 整条跳过，
这条路上就有一道静默的缝：模型写进回复的真值（如
`第三个人的身份证号是 440524188001010014。`）原样发上游，
于是 ICE 只在"值第一次进来"那一刻护住它，从模型把它写进回复起永久失效，
而且一行日志都没有。所以 assistant 也按值扫
（见 `boundary.pseudonymize_messages` 的 docstring）。

## 显式放行：为什么要留一个门

  1. 缝堵上（`boundary` 那边，按值扫），默认状态下模型拿不到真值；
  2. 门在这里，真的需要时（比如"按 ISO 7064 帮我算它的校验位"——
     那件事框架插不上手，因为运算发生在模型内部），模型可以要，
     但要用户逐次点。

区别只在于：它现在是用户点出来的，不是静默漏出去的。

## 「这次」怎么落地

那条 tool 结果要落库，而改一条已落库的旧消息得整表重写
"用完就收回"没有干净的实现路径。落地是：

  · 放行只作用于本轮的出站变换（`_EXEMPT`，下一轮 `pre_process` 开头清）
  · 所以模型这一轮看得到，下一轮开始又只看到句柄
  · 而库里那份一直是真值

[!] 这不是"永久放行" —— 每要一次就得点一次（除非选第 2 项，那本来就是
"本会话关掉信息层"）。

## 会话级降级

「同意 + 本会话整个信息层关掉」，`_DOWNGRADED`。它的性质是授权不是数据：
`/clear` 对它做的是回收权限，不是删数据。
配套：界面必须一直看得见"保护已降级"（否则用户以为还护着）。
"""

from __future__ import annotations

from typing import Dict, Optional, Set

from core.i18n import t as tr

__all__ = [
    "TOOL_NAME", "TOOL_SCHEMA", "KIND",
    "note_exempt_by_handle", "clear_exempt", "exempt_values",
    "downgrade", "downgraded", "recover", "reset_all",
    "reveal_handler",
]

TOOL_NAME = "pii_reveal"

#: 审批 kind。三态见 `core/approval.py::ICE_REVEAL_OPTIONS`。
KIND = "ice_reveal"

TOOL_SCHEMA: dict = {
    "description": (
        "取出某个 `【PII:N】` 句柄背后的**真实值**，让**你自己**看得到它。"
        "只有当你要拿这个值**自己算/自己判断**时才用"
        "（比如算校验位）。"
        "[!] **要搬运它不需要这个工具** —— 把句柄原样交给别的工具，"
        "框架会在执行前换回真值。"
        "[!] 每次调用都会**弹窗问用户**（他可以拒绝），"
        "而且真值会进对话历史。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "句柄原文，例如 【PII:1】。**照抄**，不要自己编号。",
            },
        },
        "required": ["handle"],
    },
}

# 会话级状态
#
# [!] 两张表都按 `session_id` 键、且活过一轮，登记在 `core/state_registry.py`
#    （census 有读者；`/clear` 对它们做的是回收权限，不是删数据）。

#: 会话 → 本轮放行的真值（出站变换跳过它们）。
_EXEMPT: Dict[str, Set[str]] = {}

#: 会话级降级：本会话内关掉信息层。
_DOWNGRADED: Set[str] = set()


def note_exempt_by_handle(session_id: str, handle: str) -> Optional[str]:
    """用户批了，把这个句柄对应的真值记进本轮放行表。

    返回那个真值；句柄查不到，`None`（映射被清 / 换了会话）——
    调用方必须如实说，不许假装成功（读不到 ≠ 0）。
    """
    sid = str(session_id or "")
    h = str(handle or "").strip()
    if not sid or not h:
        return None
    try:
        from core.ice.store import get_store
        val = get_store().session(sid).value_for(h)
    except Exception:  # noqa: BLE001
        return None
    if not val:
        return None
    _EXEMPT.setdefault(sid, set()).add(val)
    return val


def exempt_values(session_id: str) -> Set[str]:
    """本会话本轮放行的那几个真值（出站变换读它）。只读快照。"""
    return set(_EXEMPT.get(str(session_id or ""), ()))


def clear_exempt(session_id: str) -> None:
    """轮首清上一轮的放行（`pre_process` 开头调）。

    这就是"这次"的落地：放行活在一轮里。清在轮首而不是轮末，
    是因为轮末那条路上有好几个出口（含异常与 `stop`），而轮首只有一处。
    """
    _EXEMPT.pop(str(session_id or ""), None)


def downgrade(session_id: str) -> None:
    """本会话关掉信息层。"""
    _DOWNGRADED.add(str(session_id or ""))


def downgraded(session_id: str) -> bool:
    return str(session_id or "") in _DOWNGRADED


def recover(session_id: str) -> None:
    """回收权限（`/clear` 调）—— 不是"删数据"。

    与 `clear_exempt` 分开两次调用是有意的：`/clear` 与"轮首"是两件事，
    合成一个函数就会有人只调一半。
    """
    sid = str(session_id or "")
    _DOWNGRADED.discard(sid)
    _EXEMPT.pop(sid, None)


def reset_all() -> None:
    """进程级清空（测试夹具用；生产没有调用点）。"""
    _EXEMPT.clear()
    _DOWNGRADED.clear()


# 工具本体

def _looks_like_handle(s: str) -> bool:
    from core.ice.handles import HANDLE_RE
    return bool(HANDLE_RE.fullmatch(s.strip()))


def reveal_handler(args=None) -> "str | dict":
    """`pii_reveal` 的 handler。四支，顺序承重：

    | # | 判据 | 为什么是这个顺序 |
    |---|---|---|
    | ① | 已经放行过（本轮 `_EXEMPT` 里有这个值） | 重放走这条 —— 不给它就会再挂起一次 = 死循环（同 `exit_plan_mode`） |
    | ② | 形状不对 | 带路线（不承诺兑现不了的） |
    | ③ | 本会话没这个句柄 | 一句实话。[!] 绝不"猜"一个值出来 |
    | ④ | 否则 | 发 pending（三态） |

    [!] ①必须最先：用户批了之后框架会重放这一次调用，而那一刻
    映射还没变 —— 没有这一支，重放会再挂一次，用户被迫点第二次。
    """
    session_id = ""
    try:
        from core.request_snapshot import current_session_id
        session_id = current_session_id()
    except Exception:  # noqa: BLE001
        session_id = ""

    raw = str((args or {}).get("handle") or "").strip()

    # ① 重放：这一轮已经放行过，直接把值给它
    if session_id:
        try:
            from core.ice.store import get_store
            smap = get_store().session(session_id)
            val = smap.value_for(raw)
        except Exception:  # noqa: BLE001
            val = None
        if val and val in exempt_values(session_id):
            return ("这是那个值的真实内容：\n\n%s\n\n"
                    "[!] 它**只在这一轮**对你可见（下一轮起你会重新看到句柄）。"
                    "用它把事做完，别把它抄进回复 —— 回复里的句柄框架会替你换回来。"
                    % val)

    # ② 形状不对 —— 带路线
    if not _looks_like_handle(raw):
        return ("`%s` 不是一个句柄。句柄长这样：`【PII:1】`（**照抄**原文，"
                "不要自己编号）。如果你手上只有一段描述，那说明**模型看不到那个值**，"
                "别猜 —— 告诉用户你要哪一个。" % raw)

    # ③ 本会话没有这个句柄 —— 一句实话
    from core.ice.store import get_store
    try:
        smap = get_store().session(session_id) if session_id else None
        val = smap.value_for(raw) if smap else None
    except Exception:  # noqa: BLE001
        val = None
    if not val:
        return ("这个句柄**不在本次对话里**（框架侧没有它的映射）。"
                "它可能是上一次对话留下的、也可能是拼错了 —— "
                "**我不会猜一个值给你。**")

    # ④ 发 pending（三态）
    from core.approval import pending_payload
    body = (tr("Crannia 想把这个句柄背后的**真实内容**读出来给它自己看：\n\n"
               "    %s\n\n"
               "[!] 同意之后，那个真实内容会**进入这段对话的历史**，"
               "并且**随之后每一次请求一起发给我所对接的模型服务**。")
            % raw)
    return pending_payload(
        KIND, body=body, footer=True, subject=raw,
        status="pending_ice_reveal",
    )
