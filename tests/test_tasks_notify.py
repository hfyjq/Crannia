#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""后台任务完成通知的机械读者。

    .venv/Scripts/python.exe tests/test_tasks_notify.py

## 它钉住什么

1. 通知文本第一行就说明"这是框架转来的后台任务回报，不是用户的新要求"
   —— 否则模型会把它当成一条新的用户指令去执行。
2. `notified` 只放行一次（同一条任务不重复通知）。
3. 命令被取消，不通知；子 agent 被取消，通知（部分结论有价值）。
4. 忙时不抢：会话锁被占着 / 刚活动过 1 秒内，不起轮。
5. 起轮用的 `MessageEvent` 带 `origin="background"` 与 `routed_session_key`。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""
#@exclusive: prod-fingerprint
# ^ 上面这篇 docstring 的解释同 test_delivery.py：要写真目录 / 拍真目录指纹
#   的文件进同一个独占组（并行那批跑完之后才串行跑）。

from __future__ import annotations

import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core import jobs                                            # noqa: E402
from core import tasks_notify as TN                              # noqa: E402


class FakeScheduler:
    def __init__(self, busy: bool = False, idle: float = 99.0):
        self.busy = busy
        self.idle = idle
        self.events = []

    def is_busy(self, key):
        return self.busy

    def idle_seconds(self, key):
        return self.idle

    async def execute(self, event):
        self.events.append(event)
        return "reply-from-background"


def _job(task_id="c-probe", *, kind=jobs.KIND_COMMAND, status="done",
         notified=False, session="workspace:private", result=""):
    j = jobs.Job(
        id=task_id, command="echo probe", pid=1, started=time.monotonic(),
        out=jobs._Sink(), err=jobs._Sink(), kind=kind,
        session_key=session, storage_id="probe", description="probe command",
        status=status, exit_code=0, notified=notified,
        output_path="data/runtime/tasks/probe/%s.log" % task_id, result=result,
    )
    jobs._register(j)
    return j


def test_notification_text_says_it_is_a_framework_report():
    j = _job()
    text = TN.notification_text(j)
    assert "不是用户的新要求" in text.splitlines()[0], text
    assert j.id in text and "已完成" in text, text
    assert j.output_path in text, text
    assert "没读之前不要复述" in text, text
    print("  [ok] 通知文本：第一行标明来源、带 task id / 状态 / 输出文件")


def test_on_finish_notifies_once():
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-once")
    TN._on_finish(j)
    TN._on_finish(j)
    assert TN.pending_count() == 1, TN.pending_count()
    print("  [ok] 同一条任务只入队一次")


def test_killed_command_is_silent():
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-killed", status="killed", notified=True)
    TN._on_finish(j)
    assert TN.pending_count() == 0, "被取消的命令不该发通知"
    print("  [ok] 命令被取消 -> 静默")


def test_killed_subagent_still_notifies():
    jobs.clear()
    TN.reset()
    j = _job(task_id="s-killed", kind=jobs.KIND_SUBAGENT,
             status="killed", result="部分结论")
    TN._on_finish(j)
    assert TN.pending_count() == 1, "子 agent 被取消仍应通知（带部分结论）"
    print("  [ok] 子 agent 被取消 -> 仍然通知")


def test_a_job_marked_no_notify_is_silent():
    """前台那条不发回报（判据在创建时定，不是完成时）。

    现场反馈：两个前台子 agent 跑完，用户先拿到正经回复，
    紧接着又冒出两条「B 的后台回报刚刚到…」「A 的任务报告也回来了…」。
    根因是"父拿到结论之后再 `mark_notified`"输给了完成钩子那条线程 ——
    `done_event.set()` 之后两条线程谁先跑是运气。
`Job.notify_on_finish` 在建表那一刻定，完成钩子直接跳过。
    """
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-silent")
    j.notify_on_finish = False
    TN._on_finish(j)
    assert TN.pending_count() == 0, "前台那条不该入队完成回报"
    print("  [ok] notify_on_finish=False -> 不入队")


def test_task_wait_suppresses_the_completion_report():
    """`task(action="wait")` 已经就地拿到结论，丢掉还没起轮的那条回报。

    现场反馈：前台 `sleep 15` 超时转后台，模型 `task wait` 等到结果、
    答完；几秒后回报又起了一轮，用户看到一条"…与前面的汇报一致，没有新信息"。
    """
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-wait", status="done")
    TN._on_finish(j)
    assert TN.pending_count() == 1, "前提：这条回报已经入队"

    from core.task_tool import task_tool
    out = task_tool(task_id="c-wait", action="wait")
    assert out.get("retrieval_status") == "success", out
    assert TN.pending_count() == 0, "wait 拿到结论之后还留着一条回报"
    assert j.notified is True
    print("  [ok] wait 之后就地把回报丢掉了（含已入队的那条）")


def test_task_status_after_finish_suppresses_the_completion_report():
    """`task(action="status")` 看到它已经结束 —— 同样算"结论就地取走"。

    只有状态、没看正文也算：回报的头条就是"已完成"，模型已经知道了；
    细节它还能按 `output_file` 去读。留着回报 = 同一件事收到两遍。
    """
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-status", status="done")
    TN._on_finish(j)
    assert TN.pending_count() == 1, "前提：这条回报已经入队"

    from core.task_tool import task_tool
    out = task_tool(task_id="c-status", action="status")
    assert out.get("retrieval_status") == "success", out
    assert TN.pending_count() == 0, "status 看到已结束之后还留着一条回报"
    assert j.notified is True
    print("  [ok] status 看到已结束 -> 就地丢掉回报")


def test_task_status_while_running_keeps_the_report():
    """在跑的那一支不许抑制：那时模型没有结论，回报是唯一的路。

    判据看结果不看过程：在跑时查一次之后，它结束时的回报仍然到得了。
    """
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-running", status="running")

    from core.task_tool import task_tool
    out = task_tool(task_id="c-running", action="status")
    assert out.get("retrieval_status") == "running", out
    assert j.notified is False, "还在跑却被标成已通知 —— 完成时的回报永远不会到"

    j.status = "done"
    TN._on_finish(j)
    assert TN.pending_count() == 1, "在跑时抑制过，结束时的回报就丢了"
    print("  [ok] 还在跑 -> 不抑制，结束时的回报照旧到")


def test_run_command_job_id_after_finish_suppresses_the_completion_report():
    """`run_command(job_id=...)` 那条路与 `task` 同一条判据。

    它是模型最常用的那条（回执文案就写着"用同一个工具带 `job_id` 回来查"），
    而它此前只看快照、不抑制 —— 模型照文案做，回报还是会来。
    """
    jobs.clear()
    TN.reset()
    j = _job(task_id="c-poll", status="done")
    TN._on_finish(j)
    assert TN.pending_count() == 1, "前提：这条回报已经入队"

    from core.shell import run_command
    out = run_command(job_id="c-poll")
    assert out.get("success") is True, out
    assert out.get("status") == "done", out
    assert TN.pending_count() == 0, "run_command(job_id) 查到已结束之后还留着回报"
    assert j.notified is True
    print("  [ok] run_command(job_id) 查到已结束 -> 就地丢掉回报")


def test_ready_respects_busy_and_quiet_window():
    jobs.clear()
    TN.reset()
    _job(task_id="c-ready")
    TN._on_finish(jobs.get("c-ready"))
    busy = FakeScheduler(busy=True)
    calm = FakeScheduler(busy=False, idle=99.0)
    recent = FakeScheduler(busy=False, idle=0.1)
    assert TN._ready_keys(busy) == [], "忙的时候不该起轮"
    assert TN._ready_keys(recent) == [], "刚活动过 1 秒内不该起轮"
    assert TN._ready_keys(calm) == ["workspace:private"], TN._ready_keys(calm)
    print("  [ok] 忙 / 静默窗 / 可起三种状态")


def test_dispatch_builds_background_event_and_delivers():
    jobs.clear()
    TN.reset()
    _job(task_id="c-dispatch")
    TN._on_finish(jobs.get("c-dispatch"))
    sched = FakeScheduler()
    delivered = []
    n = TN.dispatch_once(scheduler=sched,
                         deliver=lambda session, text: delivered.append(text),
                         inline=True)
    assert n == 1, n
    assert len(sched.events) == 1, len(sched.events)
    ev = sched.events[0]
    assert ev.extras.get("origin") == "background", ev.extras
    assert ev.extras.get("routed_session_key") == "workspace:private", ev.extras
    assert delivered == ["reply-from-background"], delivered
    assert TN.pending_count() == 0
    print("  [ok] 起轮：origin / routed_session_key / 回复投递")


def test_group_session_key_maps_to_a_group_session():
    """群聊键 `workspace:group:<id>`，`MessageSession(group, <id>)`。

    两个键空间不是一回事（`platform:type:id` vs `workspace:...`）。
    夹具过去用的是 `web:private:probe`（通道串）—— 生产给的是会话键，
    于是"两段串解析不了"这个真 bug 在测试里永远造不出来（现场：
    后台任务跑完 `ValueError`，通知轮一次都起不来）。
    """
    jobs.clear()
    TN.reset()
    _job(task_id="c-group", session="workspace:group:g-9")
    TN._on_finish(jobs.get("c-group"))
    sched = FakeScheduler()
    n = TN.dispatch_once(scheduler=sched, deliver=lambda s, t: None, inline=True)
    assert n == 1, n
    ev = sched.events[0]
    assert ev.extras.get("routed_session_key") == "workspace:group:g-9"
    assert ev.session.session_id == "g-9", ev.session
    assert ev.session.message_type.value == "group", ev.session
    print("  [ok] 群聊键 -> 群 MessageSession（群 id 原样带上）")


def test_end_to_end_background_command_notifies():
    """真跑一条后台命令 → 完成钩子 → 排队 → 起轮（同步版）。"""
    import shutil

    from core import request_snapshot
    from core.shell import _run

    jobs.clear()
    TN.reset()
    request_snapshot.set_current(
        request_snapshot.capture([], [], "", "m", "workspace:private"))
    try:
        # 必须先装钩子、再起任务 —— 反了的话完成那一刻没人听见，
        #   测试会看起来像"通知没工作"（其实是安装晚了）。
        sched = FakeScheduler()
        TN.install(sched, deliver=lambda s, t: None, start_thread=False)
        r = _run("echo bg-e2e", Path.cwd(), 5000,
                 background=True, description="e2e command")
        tid = r["task_id"]
        for _ in range(50):
            snap = jobs.poll(tid)
            if snap and snap["status"] != "running":
                break
            time.sleep(0.1)
        assert snap and snap["status"] == "done", snap
        # `status` 是先置的，完成钩子在看门狗的同一段里稍后才跑 ——
        #   并行满负载时"刚看到 done 就立刻 dispatch"会抢在入队之前
        #   （单独跑几乎不会输，全量跑会）。等入队这个信号，不等 status。
        for _ in range(100):
            if TN.pending_count() >= 1:
                break
            time.sleep(0.02)
        assert TN.pending_count() == 1, (
            "完成钩子没有把任务入队（pending=%d）" % TN.pending_count())
        n = TN.dispatch_once(scheduler=sched,
                             deliver=lambda s, t: None, inline=True)
        assert n == 1, n
        text = sched.events[0].message.content
        assert "后台任务回报" in text and tid in text, text
        assert r["output_file"] in text, text
    finally:
        TN.reset()
        jobs.clear()
        shutil.rmtree(Path("data/runtime/tasks/workspace_private"),
                      ignore_errors=True)
    print("  [ok] 端到端：background 命令 → 完成钩子 → 自动起轮")


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ok = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print("FAIL  %s: %s" % (name, exc))
        except Exception as exc:                     # noqa: BLE001
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("PASS  %s" % name)
            ok += 1
    print()
    print("%d/%d passed" % (ok, len(tests)))
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_main())
