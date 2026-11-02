#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`task` 工具（查 / 等 / 取消后台任务）的机械读者。

    .venv/Scripts/python.exe tests/test_task_tool.py

## 它钉住什么

1. 不存在的 id，说清没有，并列出已知任务（≠"在跑"）。
2. `status` / `output` / `wait` / `cancel` 四种 action 的真行为。
3. `wait` 超时，`retrieval_status="timeout"`（不是把"还在跑"说成"成功"）。
4. `cancel` 真的让 job 进入 `killed`，并置 `done_event`。

ASCII-only 输出。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import jobs                                      # noqa: E402
from core.task_tool import TASK_TOOL_SCHEMA, task_tool     # noqa: E402


def _job(task_id="c-probe", *, status="done", stdout="hello", event=None):
    out = jobs._Sink()
    if stdout:
        out.write(stdout.encode("utf-8"))
    j = jobs.Job(
        id=task_id, command="probe", pid=1, started=time.monotonic(),
        out=out, err=jobs._Sink(), kind=jobs.KIND_COMMAND,
        session_key="web:private:probe", status=status,
        exit_code=(0 if status == "done" else None),
        done_event=event,
    )
    jobs._register(j)
    return j


def test_schema_has_four_actions():
    props = TASK_TOOL_SCHEMA["inputSchema"]["properties"]
    assert set(props["action"]["enum"]) == {"status", "output", "wait", "cancel"}
    assert "task_id" in TASK_TOOL_SCHEMA["inputSchema"]["required"]
    print("  [ok] schema：四个 action + task_id 必填")


def test_unknown_id_says_so():
    jobs.clear()
    r = task_tool("c-deadbeef")
    assert r.get("success") is False, r
    assert "没有这个任务" in (r.get("error") or ""), r
    assert "known_tasks" in r, r
    print("  [ok] 不存在的 id 明说并列出已知")


def test_status_and_output():
    jobs.clear()
    _job("c-status", status="running")
    r = task_tool("c-status", action="status")
    assert r["success"] and r["retrieval_status"] == "running", r
    assert r.get("stdout") == "", "status 不该带输出正文"

    _job("c-output", status="done", stdout="line-one\nline-two")
    r = task_tool("c-output", action="output")
    assert r["success"] and "line-two" in r.get("stdout", ""), r
    print("  [ok] status 不带正文；output 带尾部")


def test_wait_returns_when_done():
    jobs.clear()
    ev = threading.Event()
    j = _job("c-wait", status="running", event=ev)

    def _finish():
        time.sleep(0.15)
        j.status = "done"
        j.exit_code = 0
        j.finished = time.monotonic()
        ev.set()

    threading.Thread(target=_finish, daemon=True).start()
    r = task_tool("c-wait", action="wait", timeout_ms=2000)
    assert r["success"] and r["retrieval_status"] == "success", r
    assert r["status"] == "done", r
    print("  [ok] wait：结束就返回 success")


def test_wait_timeout_is_not_success():
    jobs.clear()
    _job("c-slow", status="running", event=threading.Event())
    r = task_tool("c-slow", action="wait", timeout_ms=50)
    assert r["success"] is True, r
    assert r["retrieval_status"] == "timeout", r
    assert r["status"] == "running", r
    a = r.get("note") or ""
    assert "还在跑" in a, a
    print("  [ok] wait 超时 -> timeout（不冒充成功）")


def test_cancel_kills_and_releases_waiters():
    jobs.clear()
    ev = threading.Event()
    j = _job("c-cancel", status="running", event=ev)
    j.cancel_event = threading.Event()
    r = task_tool("c-cancel", action="cancel")
    assert r["success"] and r["cancelled"] is True, r
    assert jobs.get("c-cancel").status == "killed", jobs.get("c-cancel").status
    assert ev.is_set(), "done_event 没被置 -> wait 会一直挂着"
    print("  [ok] cancel：killed + 唤醒 wait")


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ok = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print("FAIL  %s: %s" % (name, exc))
        except Exception as exc:                       # noqa: BLE001
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("PASS  %s" % name)
            ok += 1
    print()
    print("%d/%d passed" % (ok, len(tests)))
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_main())
