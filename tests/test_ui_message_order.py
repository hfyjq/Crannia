#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""消息顺序的读者（`packages/ui/src/message-order.js`）。

## 它钉什么

  * 判据（`packages/ui/tests/message-order.test.mjs`，node 真跑）：
    回复的落点 / 跨通道去重 / 活动块（步骤流）的落点。
  * 接线（`App.jsx`）：落点用的是那个纯函数；「已打断」那句回执
    在发送时撤掉、且不抢"最后一条消息"的身份。

## 为什么值得一个文件

`message-order.test.mjs` 写了很久，但没有任何东西跑它（手写清单的 runner
一个都不认 `.mjs`）—— 一份没人跑的断言等于没有断言。这里把它挂进
`scripts/run_tests.py` 的扫描面：跑全量时它会真的执行一次。

② 的两个现象都是看不出来的那一类：步骤流落在上一轮里、回执垂在对话里、
底部多一条假的「正在处理」—— 功能"正常"、没有报错，只有用户真的看见才算。
判据要能红。

## 跑法

    .venv/Scripts/python.exe tests/test_ui_message_order.py

输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "packages" / "ui" / "tests" / "message-order.test.mjs"
_ORDER = _ROOT / "packages" / "ui" / "src" / "message-order.js"
_APP = _ROOT / "packages" / "ui" / "src" / "App.jsx"

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


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
    print("Message order: reply anchor / dedupe / activity anchor / receipt")
    print("=" * 62)

    r = subprocess.run(["node", str(_JS)], cwd=str(_ROOT),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    check("node message-order.test.mjs passes (rc=0)", r.returncode == 0,
          out[-400:])
    check("it actually ran", re.search(r"\d+ passed", out) is not None,
          out[-200:])

    order = _ORDER.read_text(encoding="utf-8", errors="replace")
    app = _APP.read_text(encoding="utf-8", errors="replace")

    # 活动块的落点：判据在纯函数里，App 只按下标插
    check("activityAnchorIndex is exported (pure, node-tested)",
          "export function activityAnchorIndex(" in order, "")
    check("App uses it instead of re-implementing the walk",
          "const at = activityAnchorIndex(x);" in app, "")
    #: 负对照：旧判据（"最后一条 user 消息之后"，不看 rowId）会被上面那组
    #:   用例抓住 —— 这里再钉一次"那个旧写法不许搬回 App"。
    check("negative control: App must not walk back over ALL trailing assistants",
          not re.search(r"while \(at > 0 && x\[at - 1\]\.role === \"assistant\"\) at -= 1;",
                        app), "")

    # 「已打断」回执：发送时撤掉 + 不抢"最后一条消息"
    check("receipt kind is one constant (CANCEL_RECEIPT)",
          app.count('const CANCEL_RECEIPT = "cancel_receipt";') == 1, "")
    check("both push sites mark it (task.end and /api/chat)",
          app.count("kind: CANCEL_RECEIPT") == 2,
          "count=%d" % app.count("kind: CANCEL_RECEIPT"))
    check("send() drops the previous receipt before appending the new user turn",
          "x.filter((m) => !isReceipt(m))" in app
          and "base.concat([user])" in app, "")
    check("liveActivity ignores receipts (no phantom 'processing')",
          "const _liveIdx = lastNonReceipt(messages);" in app, "")
    check("actRef ignores receipts too (delta path, same criterion)",
          "const i = lastNonReceipt(messages);" in app, "")
    #: 回执不是消息（不落库），不许被 `attachRows` 贴上行号 ——
    #: 贴上就等于说"那一轮的回复是它"，收藏 / 行号引用都会指错。
    check("attachRows never attaches a row id to a receipt",
          "!isReceipt(next[i])" in app, "")

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
