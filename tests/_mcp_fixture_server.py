"""Minimal MCP server used as a FIXTURE for testing our external-MCP path.

Why this file exists
--------------------
Our external-MCP connection path (`mcpx/client.py::connect_stdio_server`) had
never been executed end to end: `data/mcp_servers.json` is empty, and the only
tests are AST-level source checks.  Before touching that path we need a server
we fully control -- so we can observe, not guess.

It exposes exactly two tools so the interesting distinctions are testable:

  * `echo`            -- read-only, carries `readOnlyHint=True`
  * `make_note`       -- write-like, carries `destructiveHint=True`

Those two annotations are the standard MCP hints that our registry currently
ignores (every external tool resolves to `('admin', 'default')`).

It also reports the environment it was spawned with, via `env_report`'s
arguments echoed back -- that is how we check what the SDK actually passes
down to a child process.

Run by the SDK over stdio; not meant to be executed by hand.
ASCII-only output: the Windows console is GBK and emoji crash it.
"""

import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("crannia-fixture")


@mcp.tool(annotations=ToolAnnotations(title="Echo", readOnlyHint=True))
def echo(text: str) -> str:
    """Echo the given text back. Read-only."""
    return "echo:" + text


@mcp.tool(annotations=ToolAnnotations(title="Make note", destructiveHint=True))
def make_note(name: str) -> str:
    """Pretend to write a note. Write-like."""
    return "noted:" + name


# 这两条 hint 显式两条都写，用来分开两个分支 ——
#   `readOnlyHint=False` + `destructiveHint=False`，我们这侧应当是
#   `("write", destructive=False)`：只做追加、确实会写。
#   缺了它，"只有明确 `False` 才降下来"那一支没有任何工具走到过。
@mcp.tool(annotations=ToolAnnotations(title="Append line",
                                      readOnlyHint=False, destructiveHint=False))
def append_line(text: str) -> str:
    """Append a line. Write-like but explicitly non-destructive."""
    return "appended:" + text


@mcp.tool(annotations=ToolAnnotations(title="Env report", readOnlyHint=True))
def env_report() -> str:
    """Report a few environment variables the child process actually sees."""
    keys = [
        "PATH",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_SESSION_ID",
    ]
    lines = []
    for k in keys:
        v = os.environ.get(k)
        if v is None:
            lines.append("%s=<unset>" % k)
        else:
            # Keep it short; we only care whether it is present and what shape.
            lines.append("%s=%s" % (k, v[:60]))
    lines.append("python=%s" % sys.executable)
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
