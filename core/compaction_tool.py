"""`request_compaction` —— 模型自己开口说「现在压吧」的那一个工具。

## 为什么要有它

上下文到 80% 时框架会提醒模型收尾。提醒只是话；这句话要能落地，得有动作。
本工具就是那个动作：模型调用它，框架在本步结束之后压缩，压完继续。

## 判据

1. 只在 80%~95% 那个窗口里发（`bus/registry._session_extra_tools`）——
   常态不发，别人不用为它付 schema 税。
2. 处理器只置一个标记，压缩本身由循环在下一次请求之前做。
   不能在处理器里直接压：那时正处在一次工具批的派发里，
   压缩会换掉 `messages` 而这一批的回填还没写完。
3. 不在窗口里被调用，明确回一句"还不用压"，不静默。
"""

from __future__ import annotations

from typing import Any, Dict

from core import context_pressure

__all__ = ["REQUEST_COMPACTION_SCHEMA", "request_compaction"]

REQUEST_COMPACTION_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_compaction",
        "description": (
            "Request immediate compaction of this conversation: earlier turns are "
            "summarised into one block and the original text stays searchable with "
            "history_search. Use it when a context notice asks you to wrap up. "
            "Compaction runs after the current step and the turn then continues."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def request_compaction(**_kwargs) -> Dict[str, Any]:
    """置一个标记；真正的压缩由循环在下一步请求前执行。"""
    from bus.registry import _current_session_id

    sid = _current_session_id()
    if not sid:
        return {"success": False,
                "error": "拿不到会话号 —— 这一步无法请求压缩，请直接继续。"}
    if not context_pressure.request_compaction(sid):
        return {"success": False,
                "error": ("现在不在可压缩窗口（上下文还没到 80%）—— "
                          "不用压缩，直接继续即可。")}
    return {"success": True,
            "message": "已请求压缩：本步结束后执行，压缩完成后这一轮继续。"}
