"""任务的四种事件：`task.started` / `finished` / `failed` / `cancelled`。

## 它们是什么 / 不是什么

是：UI 那两格计数（「X 个后台任务」/「X 个子代理运行中」）与两个浮窗的信号。
不是：状态本身 —— 真值在 `core/jobs.py` 那张表里（面板打开时 `GET /api/tasks` 现拉）。

判据：信号与状态分开（同 `plugin.blocked` 那一条）—— 事件只是"叫你去读"，
前端拿到之后再拉一次，两边算出来的东西一定一致。

## 三条不许踩的

1. 不进 `DURABLE_TYPES` —— 它们是瞬时状态（任务跑完就没有"正在运行"），
   落盘只会在回放时重放出一堆早已结束的任务。可数的记录在 `data/runtime/tasks/`。
2. 前端那三处都只更新计数，不新开活动块 —— 这些事件可能落在 `task.end`
   之后（通知轮 / 上一条任务晚一步收尾），开块就没有人会去收口。
3. 成对由 `jobs` 的钩子保证：`start()` / `start_thread()` 建表时发 start，
   看门狗收尾时发 end —— 不在各家调用点各发一次（那会漂）。

## 为什么单独一个模块

`core/jobs.py` 是纯数据（不 import 事件总线，测试可以直接驱动）；
`core/tasks_notify.py` 管"完成之后要不要起一轮"。这一份只管广播。
三件事各自一个落点，各自可以单独装/拆。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:                                   # pragma: no cover
    from core.jobs import Job

#: 结束事件按状态分派。`timeout` 是失败那一档（跑了但没跑完）；
#: `killed` 是用户/模型取消，不是失败 —— 两者在界面上说的话完全不同。
_END_TYPE = {
    "done": "task.finished",
    "killed": "task.cancelled",
    "failed": "task.failed",
    "timeout": "task.failed",
}

#: 描述进事件时截到多少字。UI 每条只显示几行，长文本没有信息量。
_DESC_CAP = 120

_installed = False


def _emit(type_: str, job: "Job") -> None:
    from core.events import get_event_bus

    get_event_bus().emit(
        type_, session_key=job.session_key,
        task_id=job.id, kind=job.kind, status=job.status,
        description=(job.description or job.command or "")[:_DESC_CAP],
        elapsed_ms=job.elapsed_ms(),
    )


def started(job: "Job") -> None:
    _emit("task.started", job)


def finished(job: "Job") -> None:
    _emit(_END_TYPE.get(job.status, "task.failed"), job)


def install() -> None:
    """接上两个钩子（幂等）。

    起始钩子在调用方线程里跑（建表那一刻），所以它必须够快 ——
       这里只做一次 `emit`（`EventBus.emit` 自己保证不阻塞、不抛）。
    """
    global _installed
    if _installed:
        return
    from core import jobs
    jobs.on_start(started)
    jobs.on_finish(finished)
    _installed = True


def is_installed() -> bool:
    return _installed


def reset() -> None:
    """仅供测试：把钩子摘掉（`jobs` 里那两个 list 也清）。"""
    global _installed
    from core import jobs
    for cb in (started, finished):
        for bucket in (jobs._start_hooks, jobs._finish_hooks):
            while cb in bucket:
                bucket.remove(cb)
    _installed = False
