#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""前端 i18n 运行时的读者（`packages/ui/src/i18n.js`）。

## 它钉什么

  * 纯模块的逐条断言：`packages/ui/tests/i18n.test.mjs`（node 真跑，零依赖）——
    查表 / 回落原文 / 首尾空白先摘掉再查 / 占位符按名字。
  * 侧栏「任务」那个标题接了 `t()`（`App.jsx` 的 `h3-left` 段）——
    据反馈截图：英文界面里「Group chat」翻了、「任务」没翻。

## 为什么这条值得单独一个文件

`i18n.js` 是所有界面文案的咽喉，而它此前一个 node 读者都没有。
`t()` 少做"摘首尾空白"这一步时，症状是十来处小半截一直是中文
（`· 当前` / `· 已暂停` / `　右键可删除` …），看起来像"漏翻了十几句"，
实际是一处。

## 跑法

    .venv/Scripts/python.exe tests/test_ui_i18n.py

输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "packages" / "ui" / "tests" / "i18n.test.mjs"
_APP = _ROOT / "packages" / "ui" / "src" / "App.jsx"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def _title_calls_t(seg: str) -> bool:
    """标题那一小段里有没有 `t("…")`（接上 i18n 的判据）。

    判据是代码形状，不是"这两个字有没有被翻译" —— 后者要跑界面才知道。
    """
    return re.search(r'\bt\("', seg) is not None


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


def main() -> int:
    print("=" * 62)
    print("UI i18n runtime (packages/ui/src/i18n.js)")
    print("=" * 62)

    # 0. 纯模块（node 真跑）
    r = subprocess.run(["node", str(_JS)], cwd=str(_ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    check("node i18n.test.mjs passes (rc=0)", r.returncode == 0, out[-400:])
    check("it actually ran (not zero assertions)",
          re.search(r"\b\d+ tests: \d+ passed", out) is not None, out[-200:])
    check("none of them failed",
          re.search(r"\b0 failed\b", out) is not None, out[-200:])

    # 1. 侧栏「任务」标题接的是 t()
    app = _APP.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<span className="h3-left">\s*(.*?)\n\s*\{batch \?', app, re.S)
    check("self-check: found the h3-left block", m is not None, "")
    seg = m.group(1) if m else ""
    # 失败详情只回显长度：那段是中文，一 print 就在 GBK 控制台上崩。
    check('the "tasks" section title goes through t()',
          _title_calls_t(seg), "len=%d" % len(seg))
    check("negative control: a bare title is caught",
          not _title_calls_t("  任务\n"), "")

    print()
    print("=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - " + f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
