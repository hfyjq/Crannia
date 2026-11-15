#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令级工具作用域（`allowed-tools`）。

    .venv/Scripts/python.exe tests/test_command_scope.py

覆盖：

1. `commands/*.md` 的 `allowed-tools` 被读出来并归一为框架工具名。
2. `SKILL.md` 的 `allowed-tools` 同样生效。
3. 命令表里的条目带着这个白名单。
4. 接线：派发命令时写进上下文，AgentLoopStage 两条路都把它传给循环。

测试在 sandbox() 中运行，只读临时目录。
输出不含 emoji，供 Windows GBK 控制台使用。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from _sandbox import sandbox, prod_snapshot, prod_diff   # noqa: E402

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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


_CMD = """\
---
description: 只读检查
allowed-tools: Read, Grep, Bash
---
检查这些文件，不要修改。
"""

_SKILL = """\
---
name: scope-demo
description: 带工具白名单的技能
allowed-tools: [Read, Glob]
---
按正文做。
"""


def main() -> int:
    before = prod_snapshot()

    with sandbox():
        from core.tool_names import normalise_tools
        from bus.loaders.skill_md import (discover_instruction_skills,
                                          discover_plugin_commands,
                                          instruction_command_map)

        _write(Path("skills") / "scope-demo" / "SKILL.md", _SKILL)
        _write(Path("skills") / "cmd-demo" / "commands" / "check.md", _CMD)

        # 1. 归一
        got = normalise_tools("Read, Grep, Bash")
        check("常见写法映射为框架工具名",
              got == frozenset({"read_file", "grep", "run_command"}), repr(got))
        check("未声明返回 None", normalise_tools(None) is None)

        # 2. 命令与技能各自的 allowed-tools
        #    命令目录在 `<根>/<包>/commands`，用深度 2 的根规格扫。
        from core.skill_sources import SkillRoot
        specs2 = [SkillRoot(Path("skills"), 2, "explicit", "explicit")]
        cmds = {c.name: c for c in discover_plugin_commands(specs2)}
        check("命令的 allowed-tools 被读出来",
              "check" in cmds
              and cmds["check"].allowed_tools == frozenset(
                  {"read_file", "grep", "run_command"}),
              repr(getattr(cmds.get("check"), "allowed_tools", None)))

        skills = {s.name: s for s in discover_instruction_skills(Path("skills"))}
        check("技能的 allowed-tools 被读出来",
              "scope-demo" in skills
              and skills["scope-demo"].allowed_tools == frozenset(
                  {"read_file", "glob"}),
              repr(getattr(skills.get("scope-demo"), "allowed_tools", None)))

        # 3. 命令表里的条目带着它
        visible, _rej = instruction_command_map(Path("skills"))
        check("命令表条目带白名单",
              "/scope-demo" in visible
              and visible["/scope-demo"].allowed_tools == frozenset(
                  {"read_file", "glob"}),
              repr(visible.get("/scope-demo")))

        # 4. 接线（源级：这条链路没有可调用的接缝）
        pre = (_ROOT / "core" / "pipeline" / "stages" / "pre_process.py").read_text(
            encoding="utf-8")
        check("派发命令时写进上下文",
              "ctx.tool_allowlist = getattr(_skill, \"allowed_tools\", None)" in pre)
        stage = (_ROOT / "core" / "pipeline" / "stages" / "agent_loop.py").read_text(
            encoding="utf-8")
        check("两条路都传给循环",
              stage.count("_tool_allowlist=ctx.tool_allowlist") == 2,
              repr(stage.count("_tool_allowlist=ctx.tool_allowlist")))
        from core.pipeline.stage import StageContext
        check("上下文有这一格且默认不限制",
              getattr(StageContext, "__annotations__", {}).get("tool_allowlist")
              is not None)

    leaked = prod_diff(before, prod_snapshot())
    check("生产目录未被写入", not leaked, str(leaked))

    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    if _FAILED:
        print("FAILED: " + " | ".join(_FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
