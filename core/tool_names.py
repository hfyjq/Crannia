"""工具名归一：把常见写法映射到本框架的工具名。

`allowed-tools` 与子 agent 定义里的 `tools` 都用这一张表，避免各自维护一份。
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["normalise_tools", "ALIASES", "OWN_TOOLS"]

#: 左侧为小写写法，右侧为本框架的工具名。
ALIASES = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "notebookedit": "edit_file",
    "bash": "run_command",
    "powershell": "run_command",
    "webfetch": "web_fetch",
    "websearch": "web_search",
    "task": "run_subagent",
    "agent": "run_subagent",
    "skill": "search_tools",
    "toolsearch": "search_tools",
    "askuserquestion": "ask_user",
}

#: 本框架的工具名。未命中对照表时，按小写形式匹配这些名字。
OWN_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "glob", "grep", "run_command",
    "web_fetch", "web_search", "search_tools", "run_subagent", "run_workflow",
    "ask_user", "todo_write", "memory_read", "memory_search", "memory_write",
    "history_search", "deliver_file", "list_workspace_files",
})


def normalise_tools(raw) -> Optional[frozenset]:
    """把列表或逗号串归一成工具名集合。未声明时返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [x for x in re.split(r"[,\s]+", raw) if x]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(x).strip() for x in raw if str(x).strip()]
    else:
        return None
    if not items:
        return None
    out = set()
    for item in items:
        low = item.lower()
        if low in ALIASES:
            out.add(ALIASES[low])
        elif low in OWN_TOOLS:
            out.add(low)
        else:
            out.add(item)
    return frozenset(out)
