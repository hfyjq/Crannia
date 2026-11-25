#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端事件接线：订阅的名字必须在 `EVENT_TYPES` 里。

## 它治什么

`to_sse()` 发的是命名事件（帧里带 `event: <type>`），浏览器只派发给
注册了该类型名的监听器。所以一条事件要两处登记：`api.js::EVENT_TYPES`
（挂监听）与组件里的 `client.subscribe`（有人接）。

只加订阅、不加名单 = 监听器永远不会被调用。这不是理论风险：
`recap` 在 v0.11.1003 就是这么漏的 —— `App.jsx` 写了订阅与渲染块，
`api.js` 没登记，于是自动回顾一年多没出现过，而当时的读者只断言了
「订阅在」（它确实在）。

## 判据

收集 `App.jsx` / `Composer.jsx` / `TestRunModal.jsx` 里字面量形式的
`client.subscribe("X")`，断言每个 X 都在 `EVENT_TYPES` 里。
`stream.state` 不在名单里是对的：它不是后端事件，由 `api.js` 的
`connect()` 就地 `dispatch`（断线 / 连上各一次）。

## 跑法

    .venv/Scripts/python.exe tests/test_ui_event_wiring.py

输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_UI = _ROOT / "packages" / "ui" / "src"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []

#: 由 `api.js::connect()` 就地 `dispatch`，不走后端命名事件。
_LOCAL_ONLY = {"stream.state"}

_SUBSCRIBERS = ("App.jsx", "Composer.jsx", "TestRunModal.jsx")


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def event_types(src: str) -> set[str]:
    """从 `api.js` 的 `EVENT_TYPES = [...]` 里取字面量。"""
    m = re.search(r"export const EVENT_TYPES = \[(.*?)\n\];", src, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def subscribed(src: str) -> set[str]:
    """字面量形式的 `client.subscribe("X"`。"""
    return set(re.findall(r'client\.subscribe\(\s*"([^"]+)"', src))


def missing_registrations(subs: set[str], types: set[str]) -> list[str]:
    """订阅了但名单里没有的（本地合成的除外）。纯函数，好喂合成靶子。"""
    return sorted(subs - types - _LOCAL_ONLY)


def main() -> int:
    print("=" * 62)
    print("UI event wiring: subscribe <= EVENT_TYPES")
    print("=" * 62)

    api = (_UI / "api.js").read_text(encoding="utf-8")
    types = event_types(api)
    subs: set[str] = set()
    for name in _SUBSCRIBERS:
        subs |= subscribed((_UI / name).read_text(encoding="utf-8"))

    # 自检：两个集合都得真读到东西，否则下面的"空集"是瞎子的空集
    check("EVENT_TYPES 读到了（至少 25 条）", len(types) >= 25,
          "只读到 %d 条" % len(types))
    check("订阅读到了（至少 15 个）", len(subs) >= 15,
          "只读到 %d 个" % len(subs))
    check("订阅是 `EVENT_TYPES` 的子集", not missing_registrations(subs, types),
          "这些订阅了但名单没有：%s" % ", ".join(
              missing_registrations(subs, types)))

    # 负对照：合成一对，缺登记必须被判出来
    fake = missing_registrations({"recap", "never.registered"}, {"recap"})
    check("负对照：合成靶子里 `never.registered` 被判出来了",
          fake == ["never.registered"], repr(fake))

    # 本次现场：recap 两处都在
    check("`recap` 在 `EVENT_TYPES` 里", "recap" in types)
    check("`recap` 有订阅方", "recap" in subs)

    print("=" * 62)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    print("=" * 62)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
