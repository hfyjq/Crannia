"""后台任务的完成通知 —— 回执之后的那一步。

## 它解决什么

`core/jobs.py` 只负责"框架持有一个跨轮执行体"；它不负责让模型知道它跑完了。
没有这一层，`run_command(background=true)` 的回执只能写"用 job_id 查" ——
模型要么死等轮询，要么忘了这件事。这一层把结果推回对话：

```
任务完成 → 排队（每会话一条待注入列表）
         → 空闲，起一轮；忙，等这一轮结束再起
```

载体是消息侧的一段文本（不是 system prompt、不是工具结果）——
模型看到的是一条"用户侧"的回报，
但第一行就写明它是框架转来的后台任务回报，不是用户的新要求。

## 三个不许省的判据

1. `notified` 原子标志 —— 一次任务只通知一次（`jobs.mark_notified`）。
2. 用户优先 —— 会话锁被占着时不插队；再加一个 1 秒静默窗，
   让"正在打字的用户"先走。这里没有排队优先级 ——
   只保证不主动抢正在发生的对话。
3. 命令被取消，不通知（取消不是完成）；子 agent 被取消，
   仍然通知（它的部分结论有价值）。

## 线程模型

一条守护线程（`_dispatcher`）+ 每会话一个"正在起轮"的标记。
起轮本身在单独线程里：`scheduler.execute()` 是 async 的，而本模块被
`jobs` 的看门狗线程调用（没有事件循环）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from channels.base import (ChannelMetadata, MessageEvent, MessageSession,
                           Message as ChannelMessage, MessageType)
from channels.message import MessageChain
from core import jobs
from core.content_sanitizer import sanitize_text
from core.session_key import group_id_of

#: 回报那条消息的开头（`notification_text` 拼它；旧行补标记也认它）。
#: 单独一格是为了不让同一个前缀在两个文件里各写一遍。
NOTIFICATION_PREFIX = "[后台任务回报"

#: 用户停止活动多久之后才允许起自动轮（秒）。
#: 不是"优先级队列"，只是不抢正在发生的对话（判据见模块 docstring）。
QUIET_SECONDS = 1.0

#: 每会话待注入的通知。
_pending: dict[str, list[dict]] = {}
#: 正在为哪个会话起轮（防同一会话并发两条）。
_dispatching: set[str] = set()

_lock = threading.Lock()
_wake = threading.Event()
_stop = threading.Event()

_scheduler = None          # 由 install() 注入
_deliver: Optional[Callable] = None
_thread: Optional[threading.Thread] = None


#: 任务类别与显示名。新增类别时在这里登记。
_KIND_LABELS = {
    jobs.KIND_COMMAND: "命令",
    jobs.KIND_SUBAGENT: "子 agent",
    jobs.KIND_SKILL: "技能任务",
}


def _kind_label(job) -> str:
    return _KIND_LABELS.get(job.kind, "后台任务")


def notification_text(job) -> str:
    """给模型看的那段话。第一行必须说明它是谁说的（否则模型会当用户提问）。"""
    desc = (job.description or job.command or "").strip()
    desc = sanitize_text(desc)[:200]
    status = {"done": "已完成", "failed": "失败",
              "timeout": "超时被收尾", "killed": "已取消"}.get(job.status, job.status)
    head = NOTIFICATION_PREFIX + " · 这是框架转来的任务结果，不是用户的新要求]"
    line = "任务 %s（%s：%s）%s" % (job.id, _kind_label(job), desc, status)
    if job.exit_code is not None:
        line += "，exit=%s" % job.exit_code
    line += "。"
    parts = [head, line]
    if job.output_path:
        parts.append("输出文件：%s" % job.output_path)
    if job.result:
        parts.append("结论：\n" + sanitize_text(job.result)[:4000])
    parts.append("要看细节就自己读输出文件；没读之前不要复述它的结论。")
    return "\n".join(parts)


def _session_for_key(key: str) -> MessageSession:
    """会话键 → 一条能还原成同一个键的 `MessageSession`。

    两个键空间不是一回事，别拿 `MessageSession.from_str` 去解析会话键
    （`workspace:private` 只有两段，`ValueError: not enough values
    to unpack`，后台任务跑完永远起不了通知轮）：
      · `MessageSession` 的串是 `platform:type:session_id`（通道身份）；
      · 会话键是 `workspace:private` / `workspace:group:<id>`
        （`core/session_key.py`，不含 platform —— 私聊跨通道共用一条）。
这里按 `session_key_for` 的逆造一个：群聊带群 id，私聊一律 `private`。

    `platform` 填 `web`：键里没有平台信息，而 `web` 是"落库即交付"
    的那一档（`_default_deliver` 见到它就返回）。IM 上的后台回报今天到不了
    投递层 —— 回复照样落进同一条会话，用户下次打开就在历史里。
    """
    gid = group_id_of(key)
    return MessageSession(
        platform="web",
        message_type=MessageType.GROUP if gid else MessageType.PRIVATE,
        session_id=gid,
    )


def build_event(session_key: str, text: str, task_id: str = "") -> MessageEvent:
    """手搓一条 `MessageEvent`（形状照 `core/cron/runner.py::_build_event`）。

    `origin="background"` 让 `im_routing` 立刻放过（否则"≥2 条活跃，
    问编号"会把一条后台回报扣在一个到点不在场的人身上）；
    `routed_session_key` 指定它进哪条会话。
    """
    session = _session_for_key(session_key)
    msg = ChannelMessage(
        sender_id="background",
        message_chain=MessageChain.from_text(text),
        sender_name="后台任务",
        message_type=session.message_type,
        session=session,
    )
    return MessageEvent(
        session=session,
        message=msg,
        platform_meta=ChannelMetadata(
            name="background", description="后台任务", platform_type=session.platform),
        extras={"origin": "background", "task_id": task_id,
                "routed_session_key": session_key},
    )


def _default_deliver(session: MessageSession, text: str) -> None:
    """把自动轮的回复投出去。Web 是落库即交付（同 cron）。"""
    if not text.strip() or session.platform == "web":
        return
    try:
        from core.cron.runner import resolve_channel
        ch = resolve_channel(session.platform)
        if ch is None:
            print("[tasks] 后台回报投不出去：%s 通道没在跑" % session.platform)
            return
        #: 回执契约：只有 `True` 才算送达（见 `channels/base.py::send`）。
        #:   同一条判据在 `cron/runner.py::_deliver`；两处都是"没声张的成功"的入口。
        if asyncio.run(ch.send(session, text[:4000])) is not True:
            print("[tasks] 后台回报投递失败：%s 通道报告未送达" % session.platform)
    except Exception as exc:                          # noqa: BLE001
        print("[tasks] 后台回报投递失败（%s: %s）"
              % (type(exc).__name__, exc))


def _on_finish(job) -> None:
    """`jobs` 的完成钩子（在它的看门狗线程里跑）。"""
    if job.status == "running":
        return
    # 前台那条不回报：它的结论父已经就地收下（`_dispatch_subagent`）。
    # 判据必须在创建时定（`Job.notify_on_finish`）——在完成钩子这里
    #    再问"父拿到没有"会与父那条线程抢时序（两个前台子 agent
    #    都多出一条"B 的后台回报刚刚到…"）。
    if not getattr(job, "notify_on_finish", True):
        return
    # 命令被取消，不通知（`jobs.cancel` 已把 notified 置 True；双保险）
    if job.kind == jobs.KIND_COMMAND and job.status == "killed":
        return
    key = str(job.session_key or "")
    if not key:
        # 没有会话，通知无处可投。不编一个 session 出来。
        return
    if not jobs.mark_notified(job.id):
        return
    item = {"task_id": job.id, "text": notification_text(job)}
    with _lock:
        _pending.setdefault(key, []).append(item)
    _wake.set()


def _ready_keys(scheduler, now: Optional[float] = None) -> list[str]:
    """哪些会话现在可以起通知轮（纯判定，测试直接调它）。"""
    now = time.monotonic() if now is None else now
    out: list[str] = []
    with _lock:
        keys = list(_pending.keys())
    for key in keys:
        with _lock:
            queue = _pending.get(key) or []
        if not queue:
            continue
        if key in _dispatching:
            continue
        try:
            if scheduler.is_busy(key):
                continue
            if scheduler.idle_seconds(key) < QUIET_SECONDS:
                continue
        except Exception:                              # noqa: BLE001
            # 判定不了就不动（宁可晚一轮，也不抢用户的对话）
            continue
        out.append(key)
    return out


def _run_turn(key: str, item: dict, scheduler, deliver) -> None:
    event = build_event(key, item["text"], item.get("task_id", ""))
    try:
        text = asyncio.run(scheduler.execute(event))
        try:
            deliver(event.session, text or "")
        except Exception as exc:                       # noqa: BLE001
            print("[tasks] 后台轮回复投递失败（%s: %s）"
                  % (type(exc).__name__, exc))
    except Exception as exc:                           # noqa: BLE001
        print("[tasks] 后台通知起轮失败（%s: %s）"
              % (type(exc).__name__, exc))
    finally:
        with _lock:
            _dispatching.discard(key)
        _wake.set()


def dispatch_once(scheduler=None, deliver=None, inline: bool = False) -> int:
    """把当前可起的通知轮发出去（返回起了几条）。

    *inline=True* 给测试用：同步跑完，不开线程。
    """
    scheduler = scheduler or _scheduler
    deliver = deliver or _deliver or _default_deliver
    if scheduler is None:
        return 0
    started = 0
    for key in _ready_keys(scheduler):
        with _lock:
            queue = _pending.get(key) or []
            if not queue:
                continue
            item = queue.pop(0)
            _dispatching.add(key)
        started += 1
        if inline:
            _run_turn(key, item, scheduler, deliver)
        else:
            threading.Thread(target=_run_turn,
                             args=(key, item, scheduler, deliver),
                             daemon=True).start()
    return started


def _dispatcher() -> None:
    while not _stop.is_set():
        _wake.wait(timeout=0.5)
        _wake.clear()
        try:
            dispatch_once()
        except Exception as exc:                       # noqa: BLE001
            print("[tasks] 通知调度失败（%s: %s）" % (type(exc).__name__, exc))


def install(scheduler, deliver: Optional[Callable] = None,
            start_thread: bool = True) -> None:
    """bootstrap 调这一处（`web/server.py` 与 cron 同批）。幂等。"""
    global _scheduler, _deliver, _thread
    _scheduler = scheduler
    if deliver is not None:
        _deliver = deliver
    jobs.on_finish(_on_finish)
    if start_thread and (_thread is None or not _thread.is_alive()):
        _stop.clear()
        _thread = threading.Thread(target=_dispatcher, daemon=True)
        _thread.start()


def reset() -> None:
    """给测试用：停线程、清待注入、清正在起轮的标记。"""
    global _scheduler, _deliver, _thread
    _stop.set()
    _wake.set()
    if _thread is not None:
        _thread.join(timeout=1.0)
    _thread = None
    _scheduler = None
    _deliver = None
    with _lock:
        _pending.clear()
        _dispatching.clear()
    _wake.clear()


def forget_task(task_id: str) -> bool:
    """丢掉某一条任务还没起轮的回报（返回是否真丢掉了）。

    `task(action="wait")` 用它：模型已经就地拿到结论，再来一条"任务跑完了"
    就是同一件事收到两遍（模型先 `wait` 到结果、几秒后回报又起了一轮，
    用户看到一条"…与前面的汇报一致，没有新信息"的空话）。
    """
    tid = str(task_id or "")
    if not tid:
        return False
    dropped = False
    with _lock:
        for key, queue in list(_pending.items()):
            kept = [it for it in queue if str(it.get("task_id")) != tid]
            if len(kept) != len(queue):
                dropped = True
                _pending[key] = kept
    return dropped


def forget(session_key: str) -> None:
    """`/clear` 用：丢掉这条会话的待注入回报与"正在起轮"标记。"""
    key = str(session_key or "")
    with _lock:
        _pending.pop(key, None)
        _dispatching.discard(key)


def is_installed() -> bool:
    """通知链真的接通了吗（回执文案据此决定敢不敢承诺"会收到回报"）。"""
    return _scheduler is not None


def pending_count() -> int:
    with _lock:
        return sum(len(v) for v in _pending.values())
