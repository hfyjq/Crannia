#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""编程纪律：改完要验（两层）。

## 钉住什么

1. 判据只认代码文件 —— 改文档/配置不该触发"去跑测试"；
2. 验证命令是探测出来的，不是猜的 —— 探测不到就一个字都不说；
3. 回合内状态：写过代码、跑过命令、提醒只给一次；
4. 接线：两条路（sync / stream）都调同一对钩子，注入的消息带
   `framework_note`（模型看得见、历史面板看不见）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_program_discipline.py
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def test_code_ext_whitelist():
    print("\n-- 1. 只认代码文件 --")
    from core.program_discipline import is_code_path

    for p in ("a.py", "src/a.ts", "x/y/z.gd", "main.c", "App.cs", "s.sh"):
        check("代码文件：%s" % p, is_code_path(p))
    for p in ("README.md", "config.yaml", "data.json", "style.css",
              "index.html", "pyproject.toml", "notes.txt", ""):
        check("不算：%r" % p, not is_code_path(p))
    check("None 不算", not is_code_path(None))


def test_verification_command_is_probed():
    print("\n-- 2. 验证命令：探测，不猜 --")
    from core.program_discipline import verification_command

    tmp = Path(tempfile.mkdtemp(prefix="progdisc-"))
    try:
        # 空目录 -> 不猜
        (tmp / "empty").mkdir()
        (tmp / "empty" / "a.py").write_text("x = 1\n", encoding="utf-8")
        check("什么都没有 -> 空串（不给空话）",
              verification_command(tmp / "empty" / "a.py") == "")

        # godot
        g = tmp / "game"
        g.mkdir()
        (g / "project.godot").write_text("[gd_project]\n", encoding="utf-8")
        (g / "scripts").mkdir()
        (g / "scripts" / "player.gd").write_text("extends Node\n",
                                                 encoding="utf-8")
        check("project.godot -> godot headless",
              verification_command(g / "scripts" / "player.gd")
              == "godot --headless --path . --quit")

        # cargo / go
        r = tmp / "rs"
        (r / "src").mkdir(parents=True)
        (r / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        check("Cargo.toml -> cargo test",
              verification_command(r / "src" / "main.rs") == "cargo test")

        # package.json：有 test 脚本才给
        n = tmp / "js"
        (n / "src").mkdir(parents=True)
        (n / "package.json").write_text(json.dumps({"scripts": {}}),
                                        encoding="utf-8")
        (n / "src" / "i.js").write_text("1\n", encoding="utf-8")
        check("package.json 没有 test 脚本 -> 空",
              verification_command(n / "src" / "i.js") == "")
        (n / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}),
                                        encoding="utf-8")
        check("package.json 有 test 脚本 -> npm test",
              verification_command(n / "src" / "i.js") == "npm test")

        # Python：要有 pytest 的痕迹
        py = tmp / "py"
        (py / "pkg").mkdir(parents=True)
        (py / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (py / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
        check("pyproject 但没有 tests -> 空",
              verification_command(py / "pkg" / "m.py") == "")
        (py / "tests").mkdir()
        check("pyproject + tests/ -> pytest",
              verification_command(py / "pkg" / "m.py")
              == "python -m pytest -q")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_turn_state():
    print("\n-- 3. 回合内状态 --")
    from core.program_discipline import ProgramTurn, NUDGE_TEXT

    t = ProgramTurn()
    check("一开始不需要提醒", not t.needs_verify())

    check("写 .md 不算写代码",
          t.observe("write_file", {"success": True, "path": "README.md"}) == ""
          and not t.wrote_code)

    t.observe("edit_file", {"success": True, "path": "core/a.py"})
    check("写 .py -> 写过代码", t.wrote_code)
    check("写过代码、没跑命令 -> 要提醒", t.needs_verify())

    t.observe("run_command", {"success": True, "status": "ended"})
    check("跑过命令 -> 不再提醒", t.ran_command and not t.needs_verify())

    # 挂起等审批没执行
    t2 = ProgramTurn()
    t2.observe("write_file", {"success": True, "path": "a.py"})
    t2.observe("run_command", {"status": "pending_approval"})
    check("挂起等审批不算跑过", not t2.ran_command and t2.needs_verify())

    # 失败的结果不算
    t3 = ProgramTurn()
    t3.observe("write_file", {"success": False, "error": "拒绝"})
    check("写失败不算写过代码", not t3.wrote_code)

    # 提醒只给一次
    t4 = ProgramTurn()
    t4.observe("write_file", {"success": True, "path": "a.py"})
    check("第一次拿到提醒", t4.take_verify_nudge() == NUDGE_TEXT)
    check("第二次不再提醒", t4.take_verify_nudge() is None)


def test_hint_given_once_per_turn():
    print("\n-- 4. 验证路线：一个回合只说一次 --")
    from core.program_discipline import ProgramTurn

    tmp = Path(tempfile.mkdtemp(prefix="progdisc2-"))
    try:
        (tmp / "project.godot").write_text("[gd_project]\n", encoding="utf-8")
        f1 = tmp / "a.gd"
        f2 = tmp / "b.gd"
        t = ProgramTurn()
        tip1 = t.observe("write_file", {"success": True, "path": str(f1)})
        tip2 = t.observe("edit_file", {"success": True, "path": str(f2)})
        check("第一次写代码给出路线", "godot --headless" in tip1, repr(tip1))
        check("第二次不再重复", tip2 == "", repr(tip2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_runner_wires_only_where_it_should():
    print("\n-- 5. 接线：两条路各一处，且不进常驻 --")
    src = (_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    bodies = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in ("agent_loop", "agent_loop_stream"):
            names = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    names.append(getattr(f, "id", None) or getattr(f, "attr", ""))
            bodies[node.name] = names

    for hook in ("observe", "take_verify_nudge"):
        for fn in ("agent_loop", "agent_loop_stream"):
            check("%s 在 %s 里" % (hook, fn), bodies.get(fn, []).count(hook) >= 1,
                  repr(bodies.get(fn)))

    check("sync 只调一次 observe（每批结果处一处）",
          bodies["agent_loop"].count("observe") == 1,
          repr(bodies["agent_loop"].count("observe")))
    check("stream 只调一次 observe",
          bodies["agent_loop_stream"].count("observe") == 1,
          repr(bodies["agent_loop_stream"].count("observe")))

    # 注入的消息必须是 framework_note（模型看得见、历史面板过滤掉）
    check("提醒挂在 framework_note 上", src.count('"kind": "framework_note"') >= 2,
          repr(src.count('framework_note')))

    # 零常驻：判据模块不许被 prompt_assembler 引入
    pa = (_ROOT / "core" / "prompt_assembler.py").read_text(encoding="utf-8")
    check("不进系统提示（零常驻 token）", "program_discipline" not in pa)


# ------------------------------------------------------------------
# 行为级：真的跑一遍 agent_loop（两条路各一遍）
# ------------------------------------------------------------------

class _RecProvider:
    """记下每次请求发出的 messages，按脚本回答。"""

    _model = "probe-model"

    def __init__(self, script):
        self._script, self._i, self.seen = script, 0, []

    def chat(self, messages, tools, system=None, thinking=None):
        import types
        self.seen.append([dict(m) for m in messages])
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return types.SimpleNamespace(content=step.get("content", ""),
                                     tool_calls=step.get("tool_calls"),
                                     total_tokens=1, reasoning_content=None)

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        from providers.base import StreamChunk
        self.seen.append([dict(m) for m in messages])
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step.get("content"):
            yield StreamChunk(content=step["content"], finish_reason="stop")
        for i, tc in enumerate(step.get("tool_calls") or []):
            yield StreamChunk(tool_call={"id": tc["id"], "type": "function",
                                         "function": tc["function"]},
                              tool_call_index=i)
        yield StreamChunk(finish_reason="tool_calls" if step.get("tool_calls") else "stop",
                          usage={"prompt_tokens": 1, "completion_tokens": 1})


def _setup_probe(sess, write_path, write_ret=None):
    import bus.registry as R
    from core.cancellation import clear

    R.reset()
    clear(sess)

    def _w(**kw):
        return (write_ret if write_ret is not None
                else {"success": True, "path": write_path})

    def _run(**kw):
        return {"success": True, "status": "ended", "stdout": "", "stderr": ""}

    R.register_tool("write_file", _w,
                    {"description": "probe", "inputSchema": {"type": "object"}})
    R.register_tool("run_command", _run,
                    {"description": "probe", "inputSchema": {"type": "object"}})
    R.note_discovered(sess, ["write_file", "run_command"])
    return R


def _script_write_then_stop(name="write_file"):
    return [
        {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": name, "arguments": json.dumps({"file_path": "a"})}}]},
        {"content": "改好了。"},
    ]


def test_sync_nudges_after_code_write_without_a_command():
    print("\n-- 7. 行为：sync 路 --")
    from core.agent_loop import agent_loop
    from core.program_discipline import NUDGE_TEXT

    sess = "progdisc-sync"
    _setup_probe(sess, "a.py")
    p = _RecProvider(_script_write_then_stop())
    agent_loop("改一下", p, session_id=sess, max_turns=15)

    check("写了 .py 却收工 -> 多发一轮", len(p.seen) == 3, repr(len(p.seen)))
    last = (p.seen[-1] or [{}])[-1]
    check("那一轮的末尾是提醒", last.get("kind") == "framework_note",
          repr(last)[:120])
    check("提醒的话对得上", NUDGE_TEXT in str(last.get("content") or ""))


def test_sync_stays_quiet_for_docs_and_after_commands():
    print("\n-- 8. 行为：不该提醒的两个反面 --")
    from core.agent_loop import agent_loop

    sess = "progdisc-md"
    _setup_probe(sess, "README.md")
    p = _RecProvider(_script_write_then_stop())
    agent_loop("改一下说明", p, session_id=sess, max_turns=15)
    check("写 .md -> 不提醒（少发一轮）", len(p.seen) == 2, repr(len(p.seen)))

    sess2 = "progdisc-ran"
    _setup_probe(sess2, "b.py")
    script = _script_write_then_stop()
    script.insert(1, {"content": "", "tool_calls": [{
        "id": "c2", "type": "function",
        "function": {"name": "run_command",
                     "arguments": json.dumps({"command": "pytest"})}}]})
    p2 = _RecProvider(script)
    # 假 provider 不是分类器能用的模型（它会带 `temperature` 等参数）；
    # 这里只关心"命令执行过没有"，所以把 run_command 的分类器投影声明成 none。
    from core import auto_review as AC
    _saved = AC._declared.get("run_command", "<absent>")
    AC.declare("run_command", "none")
    try:
        agent_loop("改一下", p2, session_id=sess2, max_turns=15)
    finally:
        if _saved == "<absent>":
            AC._declared.pop("run_command", None)
        else:
            AC._declared["run_command"] = _saved
    check("写了 .py 也跑过命令 -> 不提醒", len(p2.seen) == 3, repr(len(p2.seen)))


def test_stream_nudges_the_same_way():
    print("\n-- 9. 行为：stream 路（锁步）--")
    import asyncio
    from core.agent_loop import agent_loop_stream
    from core.program_discipline import NUDGE_TEXT

    sess = "progdisc-stream"
    _setup_probe(sess, "c.py")
    p = _RecProvider(_script_write_then_stop())

    async def _drain():
        async for _ in agent_loop_stream("改一下", p, session_id=sess,
                                         max_turns=15):
            pass

    asyncio.run(_drain())
    check("stream：写过 .py 没跑命令 -> 多发一轮", len(p.seen) == 3, repr(len(p.seen)))
    last = (p.seen[-1] or [{}])[-1]
    check("stream：末尾也是 framework_note", last.get("kind") == "framework_note",
          repr(last)[:120])
    check("stream：提醒的话对得上", NUDGE_TEXT in str(last.get("content") or ""))


def test_never_crashes_on_odd_input():
    print("\n-- 6. 怪输入不许把一轮弄挂 --")
    from core.program_discipline import ProgramTurn, verification_command

    t = ProgramTurn()
    for bad in (None, "", 123, [], {"success": True}, {"path": None},
                {"success": True, "path": 42}):
        try:
            t.observe("write_file", bad)
        except Exception as exc:                              # noqa: BLE001
            check("怪结果不抛：%r" % (bad,), False, "%s: %s" % (type(exc).__name__, exc))
            break
    else:
        check("怪结果不抛", True)

    check("怪路径不抛", verification_command(None) == "")
    check("怪路径不抛（数字）", verification_command(42) == "")


def main() -> int:
    print("=" * 62)
    print("program discipline -- verify after editing")
    print("=" * 62)
    for fn in (test_code_ext_whitelist, test_verification_command_is_probed,
               test_turn_state, test_hint_given_once_per_turn,
               test_runner_wires_only_where_it_should,
               test_sync_nudges_after_code_write_without_a_command,
               test_sync_stays_quiet_for_docs_and_after_commands,
               test_stream_nudges_the_same_way,
               test_never_crashes_on_odd_input):
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
