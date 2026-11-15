#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后台任务回报探针：模型就地读到 done 之后，还会不会再起一轮。

    .venv/Scripts/python.exe scripts/probe_task_notify.py

真进程、真任务表、真完成钩子、真派发路 —— 只有"调度器"是假的
（真调度器会跑起一整轮 agent，那一步与本条判据无关）。

两条臂：

    读了 —— 起一条后台命令，用 `run_command(job_id=...)` 查到它结束，
            再走一次派发：应当什么都不发。
    没读 —— 起一条、不查，等它自己收尾，再走一次派发：回报应当照常到。

两条都要对：只有前者对，说明回报被误杀了；只有后者对，说明抑制没接上。

跑完会把这次建的两条任务清掉（含 `data/runtime/tasks/probe/` 下的小日志）。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import jobs                                  # noqa: E402
from core import tasks_notify as TN                    # noqa: E402
from core.shell import run_command                     # noqa: E402


class _Sched:
    """假调度器：永远空闲、直接收下事件。"""

    def __init__(self) -> None:
        self.events: list = []

    def is_busy(self, key) -> bool:
        return False

    def idle_seconds(self, key) -> float:
        return 99.0

    async def execute(self, event):
        self.events.append(event)
        return "reply-from-background"


def _spawn(label: str):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(1.2); print(%r)" % label],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = jobs._Sink(), jobs._Sink()
    return jobs.start(proc, "probe-" + label, str(_ROOT), out, err,
                      session_key="workspace:private", storage_id="probe")


def _wait_job(job_id: str, timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job is not None and job.status != "running":
            return job.status
        time.sleep(0.2)
    return "timeout"


def main() -> int:
    sched = _Sched()
    delivered: list = []

    def collect(session, text):
        delivered.append(text)

    TN.install(sched, deliver=collect)
    rc = 0
    try:
        print("-- 1. read it in place (should stay silent) --")
        jobs.clear()
        TN.reset()
        j = _spawn("a")
        snap = {}
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            snap = run_command(job_id=j.id)
            if snap.get("status") != "running":
                break
            time.sleep(0.2)
        print("   run_command(job_id) -> status=%s exit=%s"
              % (snap.get("status"), snap.get("exit_code")))
        time.sleep(1.0)                      # 给完成钩子那条线程时间
        n = TN.dispatch_once(scheduler=sched, deliver=collect, inline=True)
        print("   %s dispatch_once=%d delivered=%d pending=%d"
              % ("[ok] " if n == 0 and not delivered else "[!!] ",
                 n, len(delivered), TN.pending_count()))
        if n != 0 or delivered:
            rc = 1

        print("\n-- 2. did not read it (report should arrive) --")
        jobs.clear()
        TN.reset()
        delivered.clear()
        j2 = _spawn("b")
        _wait_job(j2.id)
        time.sleep(1.0)
        n2 = TN.dispatch_once(scheduler=sched, deliver=collect, inline=True)
        print("   %s dispatch_once=%d delivered=%d"
              % ("[ok] " if n2 == 1 and delivered else "[!!] ",
                 n2, len(delivered)))
        if n2 != 1 or not delivered:
            rc = 1
    finally:
        TN.reset()
        jobs.clear()
        shutil.rmtree(_ROOT / "data" / "runtime" / "tasks" / "probe",
                      ignore_errors=True)

    print("\nresult: %s" % ("both arms agree" if rc == 0 else "one arm is wrong"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
