"""`run_program`：写一段 Python，在里面用 `tool()` 调只读工具与 MCP 工具。

## 形状

子进程（`python -I`）里只有三样东西：正常的 Python、一个
`tool(name, **kwargs)` 绑定、一个 `tools(pattern="")` 查询。每次调用变成一行 JSON
回父进程，父进程走现有的权限与执行路径，再把结果写回子进程。
只回程序打印的内容 + `result` 变量。

`tools()` 回答"脚本里此刻能调哪些名字" —— 清单与执行走同一个判定
（`_tool_allowed`），列出来却调不动、或调得动却没列出来，都是在对模型撒谎。
它按需查、不进 schema，每请求零成本。

## 为什么是子进程 + 桥，不是进程内 exec

`exec` 拿到的就是框架进程的权限；而桥接版本里每一次工具调用都经过父进程，
权限、审计、只读判定都还在原来那一处。

## 允许调什么

只读（权限档 `read` 且非破坏性）与 MCP 工具；写入/审批类一律拒绝并给路线。
理由：脚本里没有“回答变成下一轮”，审批在脚本内无处安放（同子 agent 那条判据）。

## 边界

- 子进程本身能碰系统（与 `run_command` 同级），它不是沙箱，只是编排层。
- 工具本身按 `admin` + 破坏性声明，默认要一次审批；这是诚实的口径。
- 超时杀整棵树；单次结果与总输出都有上限；调用次数有上限。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

__all__ = ["TOOL_NAME", "SCHEMA", "run_program", "callable_tool_names", "reset"]

TOOL_NAME = "run_program"

_DEFAULT_TIMEOUT_MS = 60_000
_MAX_TIMEOUT_MS = 300_000
_MAX_CODE_CHARS = 20_000
_MAX_OUTPUT_CHARS = 16_000
_MAX_CALL_RESULT = 8_000
_MAX_CALLS = 50
_MAX_LISTED = 500

_CHILD = Path(__file__).with_name("tool_program_child.py")

#: 只读白名单之外的判断走权限档（`read` 且非破坏性）。
_ALWAYS_READ = frozenset({
    "read_file", "grep", "glob", "code_intel", "memory_read",
    "memory_search", "history_search", "web_fetch", "web_search",
})


def reset() -> None:
    """测试用；当前没有进程级状态。"""


def _tool_exists(name: str) -> bool:
    """注册表里有没有这个名字。判在"能不能调"之前 —— 两句话不许合并：
    对不存在的工具说"不能在脚本里调"，等于告诉模型它存在。"""
    try:
        from bus.registry import get_tool_handler
        return get_tool_handler(name) is not None
    except Exception:                                  # noqa: BLE001
        return True            # 读不到注册表，不在这里拦，交给下面那条判据


def _owner_blocked(name: str) -> bool:
    """这个工具的归属被屏蔽（框架隔离 / 用户停用 MCP）了没有。

    脚本里的调用直接走 handler，不经过 `is_tool_active` 那道呈现闸
屏蔽与停用必须在这里再问一次，否则脚本成了绕过它们的后门。
    """
    try:
        from bus.registry import (get_tool_owner, is_owner_disabled,
                                  is_owner_quarantined)
        owner = get_tool_owner(name)
        return bool(owner) and (is_owner_quarantined(owner)
                                or is_owner_disabled(owner))
    except Exception:                                  # noqa: BLE001
        return False


def _tool_allowed(name: str) -> bool:
    if _owner_blocked(name):
        return False
    if name in _ALWAYS_READ:
        return True
    try:
        from bus.registry import get_tool_permission, get_tool_destructive
        return (get_tool_permission(name) == "read"
                and not get_tool_destructive(name))
    except Exception:                                  # noqa: BLE001
        return False


def _all_tool_names() -> list:
    """注册表里的全部名字（读不到，空表）。"""
    try:
        from bus.registry import list_tools
        return list(list_tools())
    except Exception:                                  # noqa: BLE001
        return []


def callable_tool_names(pattern: str = "") -> list:
    """脚本里此刻真能调的工具名，排序后返回；`pattern` 按子串过滤。

    与 `_tool_allowed` 是同一个判定（不是两段等价逻辑）。
    """
    try:
        names = _all_tool_names()
    except Exception:                                  # noqa: BLE001
        return []
    out = sorted(n for n in names if _tool_allowed(n))
    p = str(pattern or "").strip().lower()
    if p:
        out = [n for n in out if p in n.lower()]
    return out[:_MAX_LISTED]


def _call_tool(name: str, args: dict):
    """在父进程里真正执行一次工具调用。返回可 JSON 化的结果。"""
    from bus.registry import get_tool_handler
    from core.file_tools import as_text

    handler = get_tool_handler(name)
    if handler is None:
        raise RuntimeError("没有这个工具：%s" % name)
    result = handler(**args)
    text = as_text(result, name)
    if len(text) > _MAX_CALL_RESULT:
        text = text[:_MAX_CALL_RESULT] + "\n（结果被截断）"
    return text


def _fmt_done(msg: dict, calls: int, timed_out: bool) -> str:
    out = str(msg.get("output") or "")
    if len(out) > _MAX_OUTPUT_CHARS:
        out = out[:_MAX_OUTPUT_CHARS] + "\n（输出被截断）"
    parts = []
    if not msg.get("ok", True):
        parts.append("程序出错：%s" % (msg.get("error") or "未知错误"))
    if out:
        parts.append(out.rstrip("\n"))
    if msg.get("result") not in (None, "None"):
        parts.append("result = %s" % msg.get("result"))
    if timed_out:
        parts.append("⚠️ 超时，子进程已被终止。")
    parts.append("（工具调用 %d 次）" % calls)
    return "\n".join(parts) if parts else "程序没有输出。"


def run_program(code: str = "", timeout_ms: int = 0) -> str:
    """执行一段编排脚本。返回纯文本（错误也是文本）。"""
    code = str(code or "")
    if not code.strip():
        return "错误：code 是空的。写一段 Python；用 `tool(名字, **参数)` 调工具。"
    if len(code) > _MAX_CODE_CHARS:
        return "错误：脚本超过 %d 字符。" % _MAX_CODE_CHARS
    try:
        ms = int(timeout_ms or 0) or _DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        return "错误：timeout_ms 要是整数。"
    ms = max(1000, min(ms, _MAX_TIMEOUT_MS))

    #: 环境变量走白名单（`core/shell.clean_env`），不是继承。
    #:   `-I` 只是隔离运行选项，它不清洗环境：不传 `env=` 时子进程原样拿到
    #:   父进程的全部变量，而 `main.py` 用 `load_dotenv(override=True)` 把 `.env`
    #:   里的 provider key / 通道凭据装了进来 —— 脚本里 `print(os.environ)`
    #:   就能读到它们。`SECURITY.md` 写着凭据不进入子进程环境，这一句在此之前
    #:   只对 `run_command` 与钩子成立（它们走 `clean_env()`）。
    #:   子进程侧（`tool_program_child.py`）不读任何环境变量，协议只走 stdio。
    from core.shell import clean_env

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="crannia_prog_", suffix=".py")
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        proc = subprocess.Popen(
            [sys.executable, "-I", str(_CHILD), tmp],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=clean_env(),
        )
    except (OSError, ValueError) as exc:
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass
        return "错误：子进程起不来（%s）。" % exc

    timed_out = False

    def _kill() -> None:
        nonlocal timed_out
        timed_out = True
        try:
            proc.kill()
        except Exception:                              # noqa: BLE001
            pass

    timer = threading.Timer(ms / 1000.0, _kill)
    timer.daemon = True
    timer.start()

    def _reply(obj: dict) -> bool:
        try:
            proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            return True
        except Exception:                              # noqa: BLE001
            return False

    calls = 0
    done: dict = {}
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "call":
                calls += 1
                name = str(msg.get("name") or "")
                args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
                if calls > _MAX_CALLS:
                    reply = {"ok": False,
                             "error": "工具调用次数超过上限 %d" % _MAX_CALLS}
                elif not _tool_exists(name):
                    reply = {"ok": False,
                             "error": ("没有这个工具：`%s`。按你要做的事描述去 "
                                       "`tool('search_tools', query=...)` 里找名字。"
                                       % name)}
                elif not _tool_allowed(name):
                    reply = {"ok": False,
                             "error": ("工具 `%s` 不能在脚本里调 —— 脚本里只允许"
                                       "只读与 MCP 工具；写入请直接调用那个工具。"
                                       % name)}
                else:
                    try:
                        reply = {"ok": True, "result": _call_tool(name, args)}
                    except Exception as exc:               # noqa: BLE001
                        reply = {"ok": False,
                                 "error": "%s: %s" % (type(exc).__name__, exc)}
                if not _reply(reply):
                    break
            elif msg.get("type") == "list":
                # 查询不算一次工具调用（它不是"动作"），也不占调用次数上限。
                if not _reply({"ok": True,
                               "result": callable_tool_names(
                                   str(msg.get("pattern") or ""))}):
                    break
            elif msg.get("type") == "done":
                done = msg
                break
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except Exception:                              # noqa: BLE001
            pass
        try:
            proc.wait(timeout=3)
        except Exception:                              # noqa: BLE001
            proc.kill()
        err = ""
        try:
            err = (proc.stderr.read() or "").strip()
        except Exception:                              # noqa: BLE001
            pass
        if tmp:
            try:
                Path(tmp).unlink()
            except OSError:
                pass

    text = _fmt_done(done, calls, timed_out)
    if err and not done:
        text += "\nstderr: " + err[:2000]
    return text


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": TOOL_NAME,
    "description": (
        "写一段 Python 在子进程里执行，用 `tool(名字, **参数)` 调**只读工具与 MCP 工具**，"
        "对结果做循环/过滤/拼装。只回程序打印的内容和 `result` 变量的值。"
        "写入类工具不能在脚本里调（脚本里开不了审批）——要写就直接调那个工具。"
        "`tools('关键词')` 列出此刻可调的工具名。"
        "适合“对很多条结果跑同一套处理”，不适合替代一次普通调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "string",
                     "description": "Python 代码；用 tool('grep', pattern='x') 调工具"},
            "timeout_ms": {"type": "integer",
                           "description": "超时毫秒（默认 60000，上限 300000）"},
        },
        "required": ["code"],
    },
}
