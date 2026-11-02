"""给模型看的任务清单 —— 一个工具 + 一份会话态。

在它之前只有给用户看的活动行（`core/events.py`），没有给模型的计划载体 ——
多步任务里「它现在做到哪一步」全靠上下文自己拼。

三条判据（写下来是因为每一条都能被踩回去）：

① 载体是工具回执，不是 system prompt。
   清单每变一次，system 就是新前缀，那一段缓存全部作废（前缀只许增长；
   档位进缓存键是同一个理由）。工具回执是追加在 messages
   末尾的，前缀只增不改。不许把清单接进 `assemble_system_prompt` 的
   `memory_context`，也不许框架自己往 messages 里插一条 ——
   写者只有一个，就是 `todo_write` 自己。

② 一份当前清单，不是事件流。
   常见实现里「整表替换」是它事件模型的产物（每次写 = 一个 `todo/write` 事件）。
   我们不做事件溯源：写的只有 `todo_write`，读的只有它自己和 `/clear` 那次清。

③ 状态按 `session_id` 键，`/clear` 清。
   与 `bus.registry._discovered` 逐字同族（按一个马上要被复用的 id 键的内存态）
purge 也挂在同一个地方（`MemoryManager.new_session`）。
   那句不变量现在多一条：新会话没有行、没有笔记、没有已发现工具、没有清单。

并发：本工具必然串行 —— 权限档不是 `read`（`_WRITE_PREFIXES` 里的 `todo_`）
并发规则「整批全安全才并发」直接把它那一批退回串行。它不在
`_LOOP_SIDE_EFFECT_TOOLS` 里：那张名单的定义是「写发生在 handler 外面」，
而本工具的写就在 handler 里面 —— 两处都会把它排除，理由不同。

不落盘：重启丢掉清单是可以接受的 —— 清单本来就活在工具回执里，
而回执随会话行一起落库、重启装载时原样回来。
（`/clear` 之后两者一起没，正是要的。）

不隔离、不包裹：回执里那段字是模型自己写的，不是读来的第三方内容
（同 `search_tools` 在 `_TRUSTED_OUTPUT_TOOLS` 里的理由：框架自己算出来的）。
渲染只做空白归一，不改写别的 —— 见 `_norm()`。
"""

from __future__ import annotations

#: 清单上限 —— 定这条是因为回执也要有预算：渲染出来的清单会进 messages，
#: 而档表里的 `tool_result_max_chars`（档 3 = 6000）会把超出的部分
#: 截断 + 落盘成指针。20 x 120 字符 + 头部 ≈ 2.6k，离那道闸门还有一倍余量。
#:超限一律拒（不是静默砍）：模型少写几条比"清单被切掉一半"好。
MAX_ITEMS = 20
MAX_ITEM_CHARS = 120

STATUSES = ("pending", "in_progress", "completed")

#: 渲染用的三个记号 —— ASCII。GBK 控制台/日志里 emoji 会炸
#: （`scripts/check_gbk_safe.py` 有基线），而这三个字符与 `[ ]`/`[x]` 的传统
#: checklist 写法一致，模型一眼认得。
STATUS_MARKS = {
    "pending": "[ ]",
    "in_progress": "[~]",
    "completed": "[x]",
}

#: session_id -> 当前清单。键是马上会被 `/clear` 复用的那个 id（判据 ③）。
_todos: dict[str, list[dict]] = {}


def _norm(text) -> str:
    """空白归一：去掉首尾、把内部连续空白（含换行）折成一个空格。

    只做这一件事，因为清单的一项本来就该是一句话 —— 而它换行进来会让
    渲染出的清单串行、`[~]`/`[x]` 的记号与内容对不上。
    刻意不做别的：不截断（超长是拒，见 `_validate`）、不转义、不改词。
    """
    return " ".join(str(text).split())


def get(session_id: str) -> list[dict]:
    """某会话的当前清单。返回副本 —— 调用方改它不该能改到状态。"""
    return [dict(item) for item in _todos.get(session_id or "", ())]


def forget(session_id: str) -> None:
    """丢掉某会话的清单。唯一生产调用者在 `MemoryManager.new_session`
    （`/clear`）—— 与 `forget_discovered` 并排（判据 ③）。"""
    _todos.pop(session_id or "", None)


def render(items) -> str:
    """把清单渲染成模型读的那段文本（工具回执里的 `list` 字段）。

    带件数头部（一眼看见还剩几件）+ 逐项记号。多于一项目标态 `in_progress` 时
    只加一行提示、不改内容 —— 同一条规矩：检测可以错，打码不能错
    （这里是"提示可以加，清单不能替你改"）。
    """
    if not items:
        return "[任务清单] 空 —— 还没有事项（传 todos 建立一份）"
    counts = {"pending": 0, "in_progress": 0, "completed": 0}
    for item in items:
        counts[item["status"]] += 1
    lines = [
        "[任务清单] %d 项（%d 进行中 · %d 待办 · %d 完成）" % (
            len(items), counts["in_progress"], counts["pending"],
            counts["completed"],
        )
    ]
    for idx, item in enumerate(items, 1):
        lines.append("%s %d. %s" % (STATUS_MARKS[item["status"]], idx,
                                    item["content"]))
    if counts["in_progress"] > 1:
        lines.append(
            "提示：有 %d 项同时是 in_progress —— 建议同一时刻只留一项，"
            "否则「做到哪了」自己都看不清。" % counts["in_progress"]
        )
    return "\n".join(lines)


def _validate(todos):
    """校验并归一。返回 `(items, error)`；`error is not None`，整表不写。

    一处坏就全拒，不做"跳过坏的那条" —— 清单是整表替换语义，
    悄悄少一条等于让模型以为它写进去了（"静默空参调用"的形状）。
    """
    if not isinstance(todos, list):
        return None, ("todos 必须是数组（每项一个对象），收到的是 %s"
                      % type(todos).__name__)
    if len(todos) > MAX_ITEMS:
        return None, ("%d 条超过上限 %d 条 —— 请把清单收短（合并同类的步骤）再传"
                      % (len(todos), MAX_ITEMS))
    items = []
    for idx, raw in enumerate(todos, 1):
        if not isinstance(raw, dict):
            return None, ("第 %d 项不是对象（%s）—— 每项要 content + status 两个字段"
                          % (idx, type(raw).__name__))
        content = _norm(raw.get("content") or "")
        if not content:
            return None, "第 %d 项没有 content（一句话说明要做什么）" % idx
        if len(content) > MAX_ITEM_CHARS:
            return None, ("第 %d 项的 content 有 %d 个字符，超过 %d —— 请压成一句话"
                          % (idx, len(content), MAX_ITEM_CHARS))
        status = raw.get("status")
        if status not in STATUSES:
            return None, ("第 %d 项的 status 是 %r，只认 %s"
                          % (idx, status, list(STATUSES)))
        items.append({"content": content, "status": status})
    return items, None


def _session_id() -> str:
    """从请求快照上取会话。判据与理由都在 `request_snapshot.current_session_id` ——
    这里只留一行转发（它是唯一定义）。"""
    from core.request_snapshot import current_session_id

    return current_session_id()


def todo_write(todos=None) -> dict:
    """`todo_write` 的 handler。

    两种调用形状：
      · 传 `todos`（数组，可以是空数组 = 清空），整表替换
      · 不传 / `null`，只读回当前清单，不做任何修改
        （给模型一条"清单被压缩吃掉了、我再看一眼"的路 —— 见模块 docstring
        判据 ①：这条路不能靠往 system prompt 里塞来解决）
    """
    session_id = _session_id()

    if todos is None:
        current = get(session_id)
        return {
            "success": True,
            "changed": False,
            "count": len(current),
            "list": render(current),
            "note": "这是当前清单，本次没有修改。",
        }

    items, error = _validate(todos)
    if error is not None:
        return {
            "success": False,
            "error": error,
            "hint": ("整表替换：把**完整**的清单再传一次（上一版没被改动）。"
                     "不传 todos 则只读回当前清单。"),
        }

    _todos[session_id] = items
    in_progress = sum(1 for it in items if it["status"] == "in_progress")
    print("[todo] %d 项（%d 进行中）session=%s"
          % (len(items), in_progress, session_id or "-"))
    return {
        "success": True,
        "changed": True,
        "count": len(items),
        "list": render(items),
    }


#: 工具 schema。与其它常驻工具同一条规矩：
#: 它是每请求的永久税，改措辞必须重量一次（`scripts/measure_request.py`）。
#: 第二段那两句是反向判据（同 `run_subagent` 的描述先例）：只说"什么时候用"
#: 会让模型对每一步小事都写一张清单，而清单是每一轮都要重新读的历史。
TODO_TOOL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "todo_write",
    "description": (
        "建立/更新本会话的任务清单 —— 多步任务（三步以上）用，做完一步就当场改状态，"
        "免得跑到一半忘了还剩什么。传 todos = 整表替换；不传 = 只看当前清单（不改）。"
        "⚠️ 只有一步的活、纯问答不要用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": ("完整的新清单（整表替换，最多 %d 项、每项一句话）。"
                                "不传则只读回当前清单。" % MAX_ITEMS),
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "一句话说明做什么，别写成段落",
                        },
                        "status": {
                            "type": "string",
                            "description": "pending 待办 / in_progress 正在做 / completed 已完成",
                            "enum": list(STATUSES),
                        },
                    },
                    "required": ["content", "status"],
                },
            },
        },
    },
}
