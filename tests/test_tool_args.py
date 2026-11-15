# -*- coding: utf-8 -*-
"""工具带了不认识的参数名，拒 + 列出它真有的参数名。

模型可能给 `grep` 传了 `path`，而它的参数叫 `root`。`handler(**tool_args)`
直接抛 `TypeError` 时，回给模型的是一句 Python 原文 —— JSON 合法、必填也在，
只是名字不认识。

判据（与 `_missing_required_args` 同源）：
  · 用 `inspect.signature`（执行侧的事实），不查 schema；
  · `**kwargs` / 签名读不出来，不拦（新增的闸，判不了就保持今天的行为）；
  · 拒的话术要列出真的参数名（拒绝要带路线）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_tool_args.py
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


# 假 handler（签名就是被测判据）
def _named(a, root=None):
    return "named"


def _kw(a, **kwargs):
    return "kw"


def _no_params():
    return "none"


def _only_varargs(*args):
    return "varargs"


# 1. 判定
def test_unknown_key_is_detected():
    import core.agent_loop as AL

    orig = AL.get_tool_handler
    try:
        AL.get_tool_handler = lambda n: _named if n == "probe_named" else None
        check("unknown key is rejected",
              AL._unknown_tool_args("probe_named", {"a": 1, "path": "x"})
              == ["path"],
              repr(AL._unknown_tool_args("probe_named",
                                         {"a": 1, "path": "x"})))
        check("known keys (incl. optional) are not touched",
              AL._unknown_tool_args("probe_named", {"a": 1, "root": "x"}) == [])
        check("several unknown keys are listed (sorted)",
              AL._unknown_tool_args("probe_named",
                                    {"b": 2, "a": 1, "z": 3}) == ["b", "z"],
              repr(AL._unknown_tool_args("probe_named",
                                         {"b": 2, "a": 1, "z": 3})))
        check("empty args are fine",
              AL._unknown_tool_args("probe_named", {}) == [])
    finally:
        AL.get_tool_handler = orig


def test_kwargs_and_unknown_handlers_are_never_blocked():
    import core.agent_loop as AL

    orig = AL.get_tool_handler
    try:
        AL.get_tool_handler = lambda n: _kw if n == "probe_kw" else None
        check("**kwargs handler accepts anything",
              AL._unknown_tool_args("probe_kw", {"whatever": 1, "path": 2}) == [])
        AL.get_tool_handler = lambda n: None
        check("handler not found -> not this gate's business",
              AL._unknown_tool_args("nope", {"path": 1}) == [])
    finally:
        AL.get_tool_handler = orig


def test_real_grep_path_vs_root():
    """`grep` 的参数叫 `root`，不叫 `path`。"""
    import core.agent_loop as AL
    from core import file_tools

    orig = AL.get_tool_handler
    try:
        AL.get_tool_handler = lambda n: file_tools.grep if n == "grep" else None
        check("real grep: path is unknown",
              AL._unknown_tool_args("grep", {"pattern": "x", "path": "core"})
              == ["path"])
        check("real grep: root is known",
              AL._unknown_tool_args("grep", {"pattern": "x", "root": "core"}) == [])
    finally:
        AL.get_tool_handler = orig


# 2. 话术
def test_message_has_the_route():
    from core.agent_loop import _unknown_args_message

    msg = _unknown_args_message("grep", ["path"], ["root", "pattern", "glob"])
    check("says nothing was executed", "没有执行" in msg, msg[:160])
    check("names the bad key", "path" in msg, msg[:160])
    check("lists the real parameter names",
          "root" in msg and "pattern" in msg and "glob" in msg, msg[:200])
    check("tells the model what to do next", "重发" in msg, msg[:200])


# 3. 锁步：三条派发路各有一处（sync / stream / 审批重放）
def test_three_dispatch_paths_are_covered():
    src = io.open(_ROOT / "core" / "agent_loop.py", encoding="utf-8").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == "_unknown_tool_args"]
    check("exactly 3 call sites (replay + sync + stream)",
          len(calls) == 3, "found %d" % len(calls))
    check("the function itself exists",
          any(isinstance(n, ast.FunctionDef) and n.name == "_unknown_tool_args"
              for n in ast.walk(tree)), "")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print("\n-- %s --" % t.__name__)
        try:
            t()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s did not raise" % t.__name__, False, str(exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
