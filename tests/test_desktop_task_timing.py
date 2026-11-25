#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""任务浮窗的计时（`task-timing.js` + `App.jsx` 接线）—— 接进全量 runner。

判据（用户 2026-11-24）：「后台任务 / 子代理两个面板，点开就不更新计时了」。
服务端 `/api/tasks` 的 `elapsed_ms` 是拉取那一刻的读数；面板只在打开与事件
到达时拉一次，所以开着的面板需要本地按秒推进。

形状与 `test_desktop_format.py` 相同：纯函数用 node 直接断言；
Python 这侧负责接线（写了就得有人用）与不静默跳过。

输出一律 ASCII（GBK 控制台）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JSTEST = _ROOT / "packages" / "ui" / "tests" / "task-timing.test.mjs"
_SRC = _ROOT / "packages" / "ui" / "src" / "task-timing.js"
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


def _between(text: str, start: str, end: str) -> str:
    """Take the slice from `start` to the next `end`; empty when missing
    (so the assertion fails loudly instead of passing on a bad slice)."""
    i = text.find(start)
    if i == -1:
        return ""
    j = text.find(end, i + len(start))
    return text[i:] if j == -1 else text[i:j]


def main() -> int:
    print("=" * 62)
    print("Task panel elapsed timing (task-timing.js)")
    print("=" * 62)

    print("\n-- 0. node available --")
    node = shutil.which("node")
    if not node:
        print("  [!] node NOT on PATH -- this guard CANNOT run (and must not pass silently).")
        return 1
    print("  [ok] node = %s" % node)

    print("\n-- 1. pure helper exists and App really uses it --")
    src = _SRC.read_text(encoding="utf-8", errors="replace")
    app = _APP.read_text(encoding="utf-8", errors="replace")
    check("task-timing.js exports liveElapsedMs", "export function liveElapsedMs" in src)
    check("App.jsx imports it", 'from "./task-timing.js"' in app)
    check("TaskRow renders liveElapsedMs(task, fetchedAt, now)",
          "liveElapsedMs(task, fetchedAt, now)" in app)
    check("no render still reads task.elapsed_ms directly",
          "task.elapsed_ms" not in app)

    print("\n-- 2. the panel ticks locally, and does not poll --")
    panel = _between(app, "function TaskPanel(", "function AppInner(")
    check("TaskPanel owns a 1s local interval", "setInterval" in panel and "1000" in panel)
    check("the interval is cleared on unmount", "clearInterval" in panel)
    check("TaskPanel does not call client.call (ticking must not become polling)",
          "client.call" not in panel)
    check("TaskPanel does not name the /api/tasks URL itself", '"/api/tasks"' not in panel)

    print("\n-- 3. loadTasks records the snapshot instant --")
    load = _between(app, "async function loadTasks(", "function toggleTasks(")
    check("loadTasks stores fetched_at after the response",
          "fetched_at" in load and "Date.now()" in load)
    check("TaskPanel hands the baseline to every row", "fetchedAt={fetchedAt}" in app)

    print("\n-- 4. node behavioural checks --")
    r = subprocess.run([node, str(_JSTEST)], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(_ROOT), timeout=60)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    check("node self-check passes (within / finished / clock going back)",
          r.returncode == 0, out[-400:])
    print("      [node] " + (out.splitlines()[0] if out else "(no output)"))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
