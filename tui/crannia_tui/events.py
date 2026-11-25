"""事件模型。

crannia 的 SSE 推三十多种事件，这里只把**用得到的**点出来；
其余字段原样留在 `data` 里，渲染层不认识就忽略。
不认识的类型忽略而不是报错 —— 后端加新事件不该让 TUI 崩掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

#: 事件类型。写成常量而不是字面量：拼错一个字母时，
#: 问题是"这个名字不存在"而不是"这个分支永远不触发"。
TASK_START = "task.start"
TASK_END = "task.end"
TURN_START = "turn.start"
TURN_END = "turn.end"
SAY = "say"
DELTA = "delta"
THINKING = "thinking"
TOOL_START = "tool.start"
TOOL_END = "tool.end"
ERROR = "error"
CONTEXT_STATE = "context.state"
APPROVAL_REQUEST = "approval.request"
APPROVAL_RESOLVED = "approval.resolved"
COMPACT_PROGRESS = "compact.progress"
STEER_DELIVERED = "steer.delivered"
LLM_RETRY = "llm.retry"
LLM_RETRY_OK = "llm.retry_ok"
RECAP = "recap"
#: 后台记忆动过（抽取写入 / 巩固改动）。两条说的是同一件事：
#: 用户要知道的是"记忆更新过"，不是"哪一道工序"。
MEMORY_SAVED = "memory.saved"
MEMORY_ORGANIZED = "memory.organized"
#: ICE 出站金丝雀被拦下（安全事实）。只在 TUI 画红标 —— GUI 那边不做。
ICE_CANARY = "ice.canary"


@dataclass(frozen=True)
class Event:
    """一条事件。字段与 SSE 信封一一对应。"""

    type: str
    seq: int = 0
    ts: float = 0.0
    session: str = ""
    data: dict = field(default_factory=dict)

    # -- 取值助手 -------------------------------------------------
    # data 是 JSON 解出来的，字段类型不受控。取值时给默认值，
    # 免得渲染层被一个缺字段的事件打断 —— 少显示一行比整轮崩掉好。

    def s(self, key: str, default: str = "") -> str:
        v = self.data.get(key)
        return v if isinstance(v, str) else default

    def i(self, key: str, default: int = 0) -> int:
        v = self.data.get(key)
        return v if isinstance(v, int) else default

    def b(self, key: str, default: bool = False) -> bool:
        v = self.data.get(key)
        return v if isinstance(v, bool) else default


def parse(payload: str) -> Event | None:
    """把 SSE 的 `data:` 正文解成 `Event`。

    解不出来返回 None 而不是抛异常：SSE 里混进一行非 JSON
    不该终止整个流（后端 `to_sse` 里也做了同样的兜底）。
    """
    try:
        raw = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    t = raw.get("type")
    if not isinstance(t, str) or not t:
        return None
    data = raw.get("data")
    return Event(
        type=t,
        seq=raw.get("seq") if isinstance(raw.get("seq"), int) else 0,
        ts=raw.get("ts") if isinstance(raw.get("ts"), (int, float)) else 0.0,
        session=raw.get("session") if isinstance(raw.get("session"), str) else "",
        data=data if isinstance(data, dict) else {},
    )
