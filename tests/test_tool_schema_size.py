# -*- coding: utf-8 -*-
"""单条工具 schema 的字节报警线（`bus/registry.TOOL_SCHEMA_BYTE_CAP`）。

    .venv/Scripts/python.exe tests/test_tool_schema_size.py

覆盖：

1. 过线的 schema 在注册那一刻出声（不是每请求量）。
2. 过线只出声：schema 原样进注册表，一个字节都不改。
3. 正常的 schema 不出声（否则这条线会变成一个天天响的东西）。
4. 那条线本身的数值钉死（改它要连注释里的判据一起改）。

为什么要这条线：有函数调用提供方按单条 schema 的序列化大小硬拒整条请求，
而症状看起来像「模型今天不听话」—— 整轮 400 而不是那个工具调不动。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bus.registry as R  # noqa: E402

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


def _save() -> dict:
    return {
        "tools": dict(R._registry["tools"]),
        "tool_schemas": list(R._registry["tool_schemas"]),
        "tool_perms": dict(R._registry["tool_perms"]),
        "tool_perm_sources": dict(R._registry["tool_perm_sources"]),
        "tool_owners": dict(R._registry["tool_owners"]),
        "tool_destructive": dict(R._registry["tool_destructive"]),
        "tool_destructive_sources": dict(R._registry["tool_destructive_sources"]),
    }


def _restore(saved: dict) -> None:
    for key, value in saved.items():
        R._registry[key].clear()
        if isinstance(value, list):
            R._registry[key].extend(value)
        else:
            R._registry[key].update(value)


def _register_capturing(name: str, desc: str) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = R.register_tool(name, lambda **kw: {"ok": True},
                             {"description": desc,
                              "inputSchema": {"type": "object", "properties": {}}})
    return ok, buf.getvalue()


def main() -> int:
    saved = _save()
    try:
        check("报警线是 13,312 字节", R.TOOL_SCHEMA_BYTE_CAP == 13312,
              repr(R.TOOL_SCHEMA_BYTE_CAP))

        # 1+3. 正常大小不出声
        ok, out = _register_capturing("size_probe_small", "a small tool")
        check("小 schema 登记成功", ok is True, repr(ok))
        check("小 schema 不出声", "[schema]" not in out, out[:200])

        # 2. 过线出声
        big = "x" * (R.TOOL_SCHEMA_BYTE_CAP + 500)
        ok, out = _register_capturing("size_probe_big", big)
        check("过线 schema 仍然登记成功（不是拒装）", ok is True, repr(ok))
        check("过线 schema 出声，且带名字", "[schema]" in out and "size_probe_big" in out,
              out[:200])

        # 2b. 过线不改 schema —— 注册表里那份仍是完整长度
        wire = None
        for td in R._registry["tool_schemas"]:
            if td["function"]["name"] == "size_probe_big":
                wire = td
                break
        check("过线 schema 还在注册表里", wire is not None)
        if wire is not None:
            got = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
            check("过线 schema 没有被截断（一个字节都没改）",
                  got > R.TOOL_SCHEMA_BYTE_CAP
                  and big in wire["function"]["description"],
                  "size=%d" % got)
    finally:
        _restore(saved)

    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    if _FAILED:
        print("FAILED: " + " | ".join(_FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
