#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""事件常量不许“声明了、从没发过”（接线复核的 C 组）。

## 它治什么

`core/events.py` 里每一个 `EVT_* = "…"` 都是一句承诺：这条事件会从某处发出去。
`retry` 那次反着来 —— 常量写下了，从 `v0.11.x` 早期到 2026-11-24 零 emit 点，
`DURABLE_TYPES` 与 `__all__` 还都挂着它。读事件流的人会以为“这条路上有东西”，
而实际上永远不会来。

判据：每个 `EVT_*` 常量必须在树里至少被当作名字用过一次（`Name(Load)` 或
`E.EVT_X` 属性访问）。定义语句是 `Store`、`__all__` / `DURABLE_TYPES` 里是字符串
字面量 —— 两者都不算“用过”，这正是本读者能把死常量照出来的原因。

## 为什么必须有负对照

扫描器返回空集有两种含义：「没有死常量」与「扫描器根本没看见任何东西」。
两条必须分开 —— 所以下面拿一份合成源码把真该红的喂给它。

## 跑法

    .venv/Scripts/python.exe tests/test_event_constants.py

输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []

#: 扫这些目录 —— 与常量可能被 import / emit 的位置对齐。
_SCAN_ROOTS = ["core", "web", "channels", "bus", "skills",
               "providers", "mcp", "scripts", "tui"]


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _parse(text: str):
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def scan(sources: dict[str, str]) -> tuple[dict[str, str], set[str]]:
    """返回（定义表 `名字 → 值`，被当作名字用过的集合）。

    只看 `Name(Load)` 与 `Attribute` —— 定义本身（`Store`）、`__all__` /
    `DURABLE_TYPES` 里的字符串字面量都不算“用过”。
    """
    defs: dict[str, str] = {}
    used: set[str] = set()
    for text in sources.values():
        tree = _parse(text)
        if tree is None:
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                name = node.targets[0].id
                if (name.startswith("EVT_")
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    defs[name] = node.value.value
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                    and node.id.startswith("EVT_")):
                used.add(node.id)
            elif isinstance(node, ast.Attribute) and node.attr.startswith("EVT_"):
                used.add(node.attr)
    return defs, used


def _real_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for root in _SCAN_ROOTS:
        base = _ROOT / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                out[path.relative_to(_ROOT).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
    return out


def main() -> int:
    print("=" * 62)
    print("event constants: declared => emitted")
    print("=" * 62)

    # 1. 负对照：合成源码里那颗死常量必须被照出来（否则扫描器是瞎的）
    synthetic = {
        "a.py": 'EVT_LIVE = "live"\nEVT_DEAD = "dead"\n',
        "b.py": 'from a import EVT_LIVE, EVT_DEAD\nhandlers = {EVT_LIVE: 1}\n',
    }
    s_defs, s_used = scan(synthetic)
    check("负对照：合成源码里两颗常量都被定义",
          set(s_defs) == {"EVT_LIVE", "EVT_DEAD"}, repr(s_defs))
    check("负对照：`EVT_DEAD` 只有 import、没被当名字用过 => 必须判死",
          "EVT_DEAD" not in s_used, repr(s_used))
    check("正对照：`EVT_LIVE` 被用过 => 不许判死",
          "EVT_LIVE" in s_used, repr(s_used))

    # 2. 真树
    sources = _real_sources()
    defs, used = scan(sources)
    check("真树里读到了 `core/events.py` 的常量表（至少 20 个）",
          len(defs) >= 20, "读到 %d 个" % len(defs))
    dead = sorted(set(defs) - used)
    check("★ 没有『声明了、从没发过』的常量",
          not dead,
          "这些常量只出现在定义里：%s" % ", ".join(dead))
    missing = sorted(used - set(defs))
    check("没有引用到未定义的 `EVT_*`（拼错也在这里红）",
          not missing, ", ".join(missing))

    # 3. 具体到这次清理：`retry` 这一条已删
    from core import events as E
    check("`retry` 常量已删（唯一重试通道是 llm.retry）",
          not hasattr(E, "EVT_RETRY"), "`EVT_RETRY` 又回来了")

    print("=" * 62)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    print("=" * 62)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
