"""Built-in MCP servers — run in-process, tools bridged to bus/registry."""

#: 内置 MCP 服务器的名字 —— 一处定义。
#:
#: 真值来源：`mcpx/servers/<名>.py` 里那个 `register_tools()` 会把它的工具挂在
#: owner `mcp:<名>` 上（`web_search.py` 那两处 `register_tool(..., owner=...)`）。
#:
#: 谁读它：
#:   · `mcpx/manager.py::load_builtin_servers` —— 按这份名单去 import
#:   · `GET /api/mcp/servers` —— 哪几条要打「内置」黄标、哪几条不能真删（只许停用）
#:   · `bus/mcp_disabled.py` —— 停用是按 owner 走的，这一份是它的展示名
#:
#: 不要再手抄这个数（例如在 `manager.get_status` 里写 `"builtin_servers": 1`）——
#:   多份手抄会各自漂移，而名单只有一条时看不出来。
#:   读者 `tests/test_mcp_servers.py` 两向钉着：名单里的每个都要真有一个
#:   `mcpx/servers/<名>.py`，且它注册出来的 owner 就是 `mcp:<名>`。
BUILTIN_SERVERS: tuple[str, ...] = ("web_search",)


def owner_of(server_name: str) -> str:
    """内置服务器名 → 归属键。与 `bus/quarantine.owner_for_mcp` 逐字同形。"""
    return "mcp:" + str(server_name)
