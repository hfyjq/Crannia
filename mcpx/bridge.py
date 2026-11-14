"""
MCP → bus/registry bridge.

Translates MCP Tool definitions (JSON Schema inputSchema) into
Crannia's register_tool format and creates wrapper handlers that
delegate to MCP's call_tool().
"""

from __future__ import annotations

from typing import Any


def mcp_tool_to_schema(tool_def: dict) -> dict:
    """
    Convert an MCP tool definition to Crannia's register_tool schema format.

    MCP tool format:
      {"name": "...", "description": "...", "inputSchema": {...}}

    Crannia format:
      {"description": "...", "inputSchema": {...}}

    The inputSchema is already JSON Schema 2020-12 compliant, so we
    pass it through unchanged.
    """
    return {
        "description": tool_def.get("description", ""),
        "inputSchema": tool_def.get("inputSchema", {"type": "object", "properties": {}}),
    }


def make_mcp_handler(
    tool_name: str,
    call_tool_fn,
) -> callable:
    """
    Create a Crannia-compatible handler function that delegates to an MCP
    tool call.

    Args:
        tool_name: The MCP tool name.
        call_tool_fn: Async or sync callable(name, arguments) -> CallToolResult.

    Returns:
        A sync handler function compatible with bus/registry register_tool.
    """
    def handler(**kwargs) -> dict:
        """
        Auto-generated handler for MCP tool '{tool_name}'.
        Delegates to MCP call_tool and normalises the result.
        """
        try:
            result = _run_async(call_tool_fn(tool_name, kwargs))
            return _normalise_mcp_result(result)
        except Exception as e:
            return {"success": False, "error": f"MCP tool '{tool_name}' failed: {e}"}

    handler.__name__ = tool_name
    handler.__doc__ = f"MCP bridge handler for '{tool_name}'."
    return handler


def _run_async(coro):
    """把协程投到常驻事件循环上跑并等结果。

    ## 必须避免的两种写法

    ```
    try:    loop = asyncio.get_running_loop()
    except: return asyncio.run(coro)                    # ← 每次调用一个新循环
    else:   pool.submit(asyncio.run, coro).result()     # ← 更糟：又一个新循环，还绕一层线程池
    ```

    两条路都新建循环，而 MCP 的 `ClientSession` 绑定在创建它的那个循环上
无论上面哪一支跑，`await session.call_tool(...)` 拿的都是别的循环上的 session。
    症状：`list_tools` 成功（所以"看起来接上了"）、`call_tool` 抛 `ClosedResourceError`。

    第二支看起来像是已经在处理"有循环"这种情况了，其实没有 ——
    它处理的是"我怎么把这个协程跑起来"，而要回答的问题从一开始就是
    「它得跑在哪个循环上」。换线程不换循环，等于没换。

    现在只有一条路（`submit`）：有循环没循环都投到同一个常驻循环上。
    错误路径只许有一条 —— 留两条会漂，而漂掉的那条
    看起来还在工作。
    """
    from core.async_loop import submit
    return submit(coro)


def annotations_to_axes(annotations) -> tuple:
    """MCP 的 `ToolAnnotations`，我们那两根轴 `(permission, destructive)`。

    ## 权限档不看注解：外部 MCP 的工具一律 `write`

    注解是服务器自己说的话，而 `read` 在本仓库是唯一有闸含义的一档
    （自动审查的只读跳过 · 并发预取 · 计划模式的只读闸 · 只读子 agent）。
    让被保护对象的一句话同时打开这四扇门，是把信任的方向弄反了。
    MCP 规范本身也写着这些提示不保证准确、不可信服务器的注解不应当被
    直接用来决定是否执行。

    所以 `readOnlyHint` 只作展示（工具描述里可以写"它自称只读"），
    它不影响任何判定。想要回"只读"带来的那几样，等信任开关做出来再说
    （先观察实际使用中的反馈，再定要不要给）。

    ## `destructive` 仍然看注解，方向是 fail-closed

    | hint | 值 | 我们 |
    |---|---|---|
    | `destructiveHint` | `False` | `destructive=False`（它自己声明"只做追加"） |
    | `destructiveHint` | `True` / 没给 | `destructive=True`（规范默认 true） |

    那一根轴的兜底本来就是 `false`（没声明 = 不算不可逆），所以这里保留
    "服务端说 false 就不算"不构成新的信任方向 —— 真正的承重是注册表的棘轮
    与兜底闸。`annotations` 是 `None`（最常见的正常情况）按"没给"处理。

    ## 另外两个 hint 今天没有对应的轴（如实记）

    * `idempotentHint`（默认 false）—— "重复调用没有额外影响"，不等于「可逆」。
    * `openWorldHint`（默认 true）—— 一个外部 MCP 工具可能是一条完整的出网通道；
      出网地板管得了 `web_fetch` 与 MCP 的 endpoint，管不了 server 内部干了什么
      （与 `run_command` 那条已知残余同形）。
    """
    # `is not False`（而不是 `is True`）：只有明确写出来的 `False` 才降下来。
    destructive = getattr(annotations, "destructiveHint", None) is not False
    return "write", destructive


def _normalise_mcp_result(result: Any) -> dict:
    """
    Normalise an MCP CallToolResult to Crannia's unified format.

    MCP returns: CallToolResult(content=[TextContent(type="text", text="...")], isError=False)
    We convert to: {"success": True, "data": "...", ...}
    """
    # Check if it's an MCP CallToolResult
    if hasattr(result, "content"):
        texts: list[str] = []
        is_error = getattr(result, "isError", False)
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(item.text)
            elif isinstance(item, dict) and "text" in item:
                texts.append(item["text"])
        return {
            "success": not is_error,
            "data": "\n".join(texts),
            "raw": str(result),
        }

    # Already a dict — pass through
    if isinstance(result, dict):
        return result

    # Fallback
    return {"success": True, "data": str(result)}
