"""`task` 工具 —— 查 / 等 / 取消一个后台任务。

## 它为什么存在

后台任务产生时，模型拿到的回执只有 task id + 输出文件。没有这个工具，
模型要么反复调 `run_command(job_id=...)` 轮询，要么干脆忘了这件事。
它把三件事收成一处：看状态、阻塞等到结束、取消。

## 按需发现（不是常驻）

它不在 `_CORE_TOOLS` 里 —— 只有这个会话真的产生过后台任务时才值得
付它的 schema 税。接法：`core/shell.py::_to_background` 创建任务时调
`bus.registry.note_discovered(session_key, "task")`（只追加、不改前缀，
`K5` 的判据）。回执里也直接点名这个工具，模型不用猜。

## 判据

- `status` 与 `output` 只读；`wait` 会占住这一轮（最多 600 秒 —— 再长模型
  就该先说话，而不是让整轮卡在一个等上）；`cancel` 会杀掉进程 / 让子 agent
  协作退出。
- 权限档是 `write`（整条工具有取消能力；`read` 是唯一有闸含义的那一档，
  写错成 `read` 会让它被并发预取与只读子 agent 放行 —— 拿不准就写 `write`）。
- `destructive: false`：取消是调用者点名要的，不算"没人要求的损失"
  （同 `write_file` 覆盖点名路径的判据）。
"""

from __future__ import annotations

from core import jobs

#: 最多阻塞多久（毫秒）—— 600 秒：再长模型就该先说话，
#: 而不是让整轮卡在一个等上。
MAX_WAIT_MS = 600_000


TASK_TOOL_SCHEMA: dict = {
    "name": "task",
    "description": (
        "查 / 等 / 取消一个后台任务（命令或子 agent）。"
        "只在回执或后台任务回报里出现过 task_id 时用；别拿它查普通工具的结果。"
        "action：status（默认，看一眼）· output（带输出尾部）· "
        "wait（阻塞到结束，最多 600 秒）· cancel（取消）。"
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务 ID（回执里的 task_id / job_id，形如 c1a2b3c4）",
            },
            "action": {
                "type": "string",
                "enum": ["status", "output", "wait", "cancel"],
                "description": "要做什么（默认 status）",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "wait 最多等多久（毫秒，默认 30000，上限 600000）",
            },
        },
        "required": ["task_id"],
    },
}


def _snap(task_id: str, tail: int = 0) -> dict:
    job = jobs.get(task_id)
    if job is None:
        return {
            "success": False,
            "error": "没有这个任务：%r。任务表只记转过后台的命令 / 子 agent；"
                     "正常完成的普通工具调用不进表。" % task_id,
            "known_tasks": [j["task_id"] for j in jobs.list_jobs()[:10]],
        }
    return {"success": True, **job.snapshot(tail=tail)}


def _suppress_notification(task_id: str) -> None:
    """这条任务已经有人就地拿到结论，它的完成回报不再发。

    实现只有一处（`core/jobs.py::suppress_notification`）——
    `run_command(job_id=...)` 那条路也要它，抄两份会各自漂。
    """
    jobs.suppress_notification(task_id)


def task_tool(task_id: str = "", action: str = "status",
              timeout_ms: int = 30000) -> dict:
    """工具入口。异常一律回报成失败（与其它框架工具同形）。"""
    tid = str(task_id or "").strip()
    if not tid:
        return {"success": False,
                "error": "`task_id` 是必填的（回执里那一串，形如 c1a2b3c4）。"}

    act = str(action or "status").strip().lower()
    if act not in ("status", "output", "wait", "cancel"):
        return {"success": False,
                "error": "不认识的 action：%r（只认 status / output / wait / cancel）"
                         % act}

    job = jobs.get(tid)
    if job is None:
        return _snap(tid)

    if act == "cancel":
        did = jobs.cancel(tid)
        snap = _snap(tid, tail=200)
        snap["cancelled"] = bool(did)
        snap["note"] = ("已发出取消。" if did else
                        "这条任务已经结束，没有可取消的东西。")
        return snap

    if act == "wait":
        ms = int(timeout_ms or 30000)
        ms = max(0, min(ms, MAX_WAIT_MS))
        snap = jobs.wait_for(tid, ms / 1000.0)
        if snap is None:
            return _snap(tid)
        out = {"success": True, **snap}
        if snap.get("status") == "running":
            out["retrieval_status"] = "timeout"
            out["note"] = ("等了 %d 毫秒还没结束 —— 它还在跑。可以再等，"
                           "或者先做别的，完成时会收到后台任务回报。" % ms)
        else:
            out["retrieval_status"] = "success"
            out["note"] = "已经结束。要细节就看 output_file（或 action=output）。"
            # 结论已经就地交给模型，不再发"后台任务回报"。
            #   两半都要：`mark_notified` 挡"之后再入队"，`forget_task` 丢掉
            #   已经入队、还没起轮的那条 —— 完成钩子在另一条线程上，
            #   它可能刚把回报塞进去（先 `wait` 到结果，几秒后又来一轮
            #   "…与前面的汇报一致"的空话）。
            _suppress_notification(tid)
        return out

    tail = 4000 if act == "output" else 0
    out = _snap(tid, tail=tail)
    out["retrieval_status"] = ("running" if out.get("status") == "running"
                               else "success")
    if act == "status" and out.get("status") == "running":
        out["note"] = "还在跑。要等它结束用 action=wait；要取消用 action=cancel。"
    else:
        # 已经结束：结论就地交给了模型，不再发"后台任务回报"。
        #   只有状态没看正文也算取走 —— 回报的头条就是"已完成"，模型已经
        #   知道了；细节它还能按 `output_file` 去读。
        #   在跑的那一支不许抑制：那时模型没有结论，回报是唯一的路。
        _suppress_notification(tid)
    return out
