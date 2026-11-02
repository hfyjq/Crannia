# -*- coding: utf-8 -*-
"""通道的「收」与「跑」解耦—— IM 能打断、能 steer 的地基。

## 为什么要有它

六个通道今天都是 `await scheduler.execute(event)`，而管线的五个阶段文件里
一个 `await` 都没有（`core/pipeline/stages/*.py`，当场数的），那一轮里
通道线程的事件循环不回头：下一条消息连"收"都排不进来，`/stop` 同理。
Web 能打断是因为 waitress 多线程（`/api/stop` 走另一条连接），而 IM 是被动
推送，没有侧信道。

把"谁等这一轮"换掉：接收路径只入队并立刻返回，真正跑轮的是
每会话一条 worker 线程（自带常驻事件循环 + FIFO 队列）。

## 三条承重的判据

① worker 的循环必须常驻（`run_forever` + 消费协程），不许用
   `asyncio.run(...)` / `run_until_complete(...)` 一轮一个循环：
   `_sync_approval_timer` 把审批倒计时挂在当时正在跑的那个 loop 上
   （`core/pipeline/scheduler.py:737` + `core/approval_timeout.py:204`），
   而到点那一手是 `asyncio.ensure_future(self.execute(evt))`
   （`scheduler.py:773`）。loop 在轮末就关，IM 的审批超时静默失效
   （症状：一直等，不结算）。消费协程用 `await` 等新活，loop 一直活着。

② 每会话 FIFO，不靠锁：`_session_lock_for` 是一把普通
   `threading.Lock`，没有公平性，解耦之后"谁先抢到锁"就是用户看得见的
   乱序（同一会话里两句话被倒着做）。

③ 队列键用自然键（`core/session_key.py::session_key_for`）：
   `im_routing` 今天在 `execute` 里面才决定"进哪条会话"。私聊恰好就是
   `workspace:private`（与绑定键同一条）、群聊是 `workspace:group:<id>`
常见情形完全对得上；真的被绑定改派到别的会话时只是过度串行
   （不会错，只是不必要地排队）。

## 分流（`intake`，顺序承重）

| 判据 | 结果 |
|---|---|
| 文本是 `/stop`（只认这一条，`.strip().lower()`，与 `pre_process` 同形） | 立刻 `request_cancel` + 丢弃队列与收件箱 + 回一句；不入队 |
| 带图 / 文件 / 语音 | 排队（v1 不做附件 steer，同 Web） |
| `/` 开头 | 排队（命令在 `pre_process` 解析；注入进循环里没人执行它） |
| 该会话有待审批 | 排队（那句话是审批回答， 的原现场） |
| 该会话正忙且目标唯一 | 进 `core/steer.py` 收件箱 + ack |
| 其余 | 排队（前面有活时才 ack —— 没活还 ack 是纯噪声） |

## 轮末残留（`steer_leftover`）

这一轮没赶上的补话由这里接管（Web 那边是前端用同一口气泡接着跑）。
两条不许自动接管：被打断的那一轮（用户按了停，别再自己开一轮）、
停在待审批的那一轮（下一条用户消息是审批回答，不能被一句补话顶掉）。

## 残余（写进 ，别读成"做完了"）

· CLI 不走这里（`input()` 阻塞是它自己的语义，Ctrl+C 就是它的打断）；
· 补话不进 dedup（`pre_process` 那道闸在"轮"里）：平台重投一条补话会让
  同一句在上下文里出现两遍 —— 代价是一句话，不是坏一轮；
· `/clear` 不动 worker（只丢队列）：停掉线程等于让挂在上面的审批定时器
  静默失效。线程的回收在 `_evict_idle_locked` —— 条数满了才收，
  且只收"队列空、没在跑、没有审批定时器"的那些；
· 通道被停掉时排队的那几条还会跑，回话会撞已经关掉的循环，计数 + 出声；
· 消息里"媒体下载慢"仍可能插队（入队发生在下载之后）；纯文字不受影响。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
import uuid
from collections import deque
from typing import Awaitable, Callable, Deque, Dict, Optional

from core.i18n import t as tr
from core.session_key import session_key_for

__all__ = ["submit", "intake", "pending", "drop", "purge", "stop", "is_serving",
           "MAX_QUEUE", "MAX_WORKERS"]

#: 通道回话的文案（中文原文 = 键，`locales/*.json` 按原文索引）。
#: 取值处一律 `tr(_COPY[...])`：这几种语言不在这里判，`t()` 一个口子。
_COPY = {
    # `/stop`：队列里真的还有东西要丢
    "stopped": "已中断，剩余消息已取消",
    # `/stop`：有轮次在跑，但没有排队的
    "stopped_bare": "已中断",
    # `/stop`：什么都没在跑（说"已中断"就是撒谎）
    "idle_stop": "无任务",
    # 补话被收件箱接住（它会在下一次工具批之后进上下文）
    "steer_ok": "正在处理，已收到消息",
    # 排队：`/` 开头的命令
    "queued": "命令信息，等待本轮任务结束后处理",
    # 排队：带附件 / 待审批 / 目标不唯一 —— 都是"收到了，但要等"
    "queued_media": "信息已收到，等待本轮任务结束后处理",
    # 队列满了：这一条没收下（不说就是"发了没反应"）
    "queue_full": "消息太多，这条没有排队，请稍后重发",
    # 这一轮抛了（与电报/钉钉原来那句同一份台词，收拢到一处）
    "error": "内部处理异常，请稍后重试。",
}

#: 会话键 → worker。会话级状态，已登记在 `core/state_registry.py`。
_workers: Dict[str, "_Worker"] = {}
_lock = threading.Lock()

#: 一条会话最多排多少条（不含正在跑的那一条）。
#: 上限存在的理由不是"怕用户话多"：没有上限的队列就是一条内存泄漏 ——
#: 一轮跑得很久、或者通道被灌时，队列会一直长，而没有任何一处会缩它。
MAX_QUEUE = 50

#: 进程里最多同时养多少条 worker 线程。超了就在建新的之前先收掉最闲的那些。
#: 它是收敛值，不是硬闸：收不掉的那几条（正在跑）不会被硬等，
#:   宁可多开一条线程也不丢用户一条消息。下一次新建时会再收一遍。
MAX_WORKERS = 64

#: 收 idle worker 的门槛（秒）。只在"已经到 MAX_WORKERS"时才看它 ——
#: 没满的时候一条都不收：留着不花钱，收错一次（把挂着审批定时器的那条停了）
#: 的代价是超时静默失效。
IDLE_EVICT_S = 1800.0

#: `_Worker.push` 的三种结果。"没排上"与"排上了"必须分得开。
_PUSH_OK = 0
_PUSH_FULL = 1
_PUSH_STOPPED = 2


# worker：一条会话的执行体

class _Worker:
    """常驻 loop + FIFO 队列。每条会话一个（同会话串行、跨会话并行）。"""

    def __init__(self, session_key: str) -> None:
        self.key = session_key
        self._q: Deque[Callable[[], Awaitable[None]]] = deque()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake: Optional[asyncio.Event] = None
        self._started = threading.Event()
        self._stopped = False
        #: 上一次开始干活 / 入队的时刻（收 idle worker 用）。
        self.last_active = time.monotonic()
        self.running = False
        self._thread = threading.Thread(
            target=self._run, name="turn-%s" % _short_key(session_key),
            daemon=True)
        self._thread.start()
        #: 5 秒是防死等：起不来时 `submit` 仍能入队，loop 起来之后自己会消费。
        self._started.wait(5.0)

    # ---- 任意线程 ----
    def push(self, job: Callable[[], Awaitable[None]]) -> int:
        """入队。返回 `_PUSH_OK` / `_PUSH_FULL` / `_PUSH_STOPPED`。

        "没排上"必须是一种能看见的结果：静默丢一条，用户看到的是
        "发了没反应"，与"消息没进来"同形。
        """
        with self._lock:
            if self._stopped:
                return _PUSH_STOPPED
            if len(self._q) >= MAX_QUEUE:
                return _PUSH_FULL
            self._q.append(job)
        self.last_active = time.monotonic()
        self._kick()
        return _PUSH_OK

    def clear(self) -> int:
        """丢掉还没开始跑的那几条，返回条数（正在跑的那轮不动）。"""
        with self._lock:
            n = len(self._q)
            self._q.clear()
        return n

    def depth(self) -> int:
        with self._lock:
            return len(self._q)

    def stopped(self) -> bool:
        return self._stopped

    def idle_for(self) -> float:
        """闲了多久（秒）；有活在跑或排着就是 0。"""
        if self.running or self.depth():
            return 0.0
        return time.monotonic() - self.last_active

    def stop(self) -> bool:
        """停掉这条 worker。队列空、没在跑才停（返回 False 表示还忙着）。

        停的是它那条常驻循环 -> 挂在上面的审批定时器一起没了。
        所以调用方只有两个：`_evict_idle_locked`（已确认没有待审批）
        与 `stop()`（显式销毁）。
        """
        with self._lock:
            if self._stopped:
                return True
            if self._q or self.running:
                return False
            self._stopped = True
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass                         # 循环已经关了
        return True

    def _kick(self) -> None:
        loop = self._loop
        if loop is None:
            return                       # 还没起来：起来之后 `_consume` 自己会看队列
        try:
            loop.call_soon_threadsafe(self._signal)
        except RuntimeError:
            pass                         # 循环已关（进程收尾）

    def _signal(self) -> None:
        if self._wake is not None:
            self._wake.set()

    # ---- 本线程（常驻 loop）----
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        #: 先建 `Event` 再起消费协程 —— 反过来的话，`_started` 之后那一小段里
        #:   推来的活会撞上 `_wake is None`（信号被丢掉，队列要靠下一次推才动）。
        self._wake = asyncio.Event()
        self._loop = loop
        loop.create_task(self._consume())
        self._started.set()
        try:
            loop.run_forever()
        finally:
            #: 停的时候把还挂在循环上的协程收掉再关 —— 直接 `close()` 会留一条
            #:   "Task was destroyed but it is pending" 的噪声（它长得像故障）。
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:                     # noqa: BLE001
                pass
            loop.close()

    async def _consume(self) -> None:
        """就在这里 `await` 每一条活：轮次因此跑在这个常驻循环上
        （判据 ①：审批倒计时挂的正是它）。"""
        wake = self._wake
        if wake is None:                              # 到不了：`_run` 先建它
            wake = self._wake = asyncio.Event()
        while True:
            await wake.wait()
            wake.clear()
            while True:
                with self._lock:
                    job = self._q.popleft() if self._q else None
                if job is None:
                    break
                self.running = True
                self.last_active = time.monotonic()
                try:
                    await job()
                except Exception:                     # noqa: BLE001
                    #: 一条活炸了不许把消费协程带走（那会让整条会话永久卡死，
                    #: 而症状是"发消息没反应" —— 与"没收到"同形）。
                    traceback.print_exc()
                finally:
                    self.running = False


def _short_key(session_key: str) -> str:
    """线程名用的短键（ASCII，同会话一眼能认出来）。"""
    return (session_key.rsplit(":", 1)[-1] or "x")[:24]


def _has_pending_approval(key: str) -> bool:
    """这条会话还有审批倒计时挂着吗（挂着的 worker 不许收）。"""
    try:
        from core import approval_timeout as _AT
        return _AT.event_of(key) is not None
    except Exception:                                 # noqa: BLE001
        return True          # 读不到就当有 —— 宁可多留一条线程


def _evict_idle_locked() -> int:
    """收掉最闲的几条 worker（调用方持 `_lock`）。返回收掉的条数。

    三条判据缺一不可：队列空、没在跑、没有审批定时器。
    第三条是"worker 不回收"那条老规矩剩下的部分 —— 定时器挂在 worker 的
    常驻循环上，停了它就是超时静默失效。
    """
    idle = []
    for key, w in list(_workers.items()):
        if w.stopped():
            idle.append((float("inf"), key))
            continue
        gap = w.idle_for()
        if gap < IDLE_EVICT_S or _has_pending_approval(key):
            continue
        idle.append((gap, key))
    idle.sort(reverse=True)                  # 最闲的在前
    n = 0
    for _, key in idle:
        w = _workers.get(key)
        if w is None:
            continue
        if w.stop() and w.stopped():
            _workers.pop(key, None)
            n += 1
    return n


def _worker_for(session_key: str) -> "_Worker":
    with _lock:
        w = _workers.get(session_key)
        if w is not None and not w.stopped():
            return w
        if w is not None:
            _workers.pop(session_key, None)          # 已停的那条换掉
        if len(_workers) >= MAX_WORKERS:
            _evict_idle_locked()
        w = _Worker(session_key)
        _workers[session_key] = w
        return w


# 公共入口

def submit(session_key: str, job: Callable[[], Awaitable[None]]) -> bool:
    """把一条活排到这条会话的队尾。任意线程可调，立刻返回。

    返回"排上了吗"：队列满时是 `False`。调用方必须把它说出来 ——
    静默丢一条与"消息没进来"在用户眼里完全一样。
    """
    if not session_key or job is None:
        return False
    key = str(session_key)
    for _ in range(3):
        w = _worker_for(key)
        state = w.push(job)
        if state == _PUSH_OK:
            return True
        if state == _PUSH_FULL:
            return False
        # 已停（正好被收掉）：换一条再来
    return False


def pending(session_key: str) -> int:
    """队列里还没开始跑的条数（不含正在跑的那条）。"""
    with _lock:
        w = _workers.get(session_key)
    return w.depth() if w is not None else 0


def is_serving(session_key: str) -> bool:
    """这条会话有 worker 吗（自检 / 日志用；不是"有活"）。"""
    with _lock:
        return session_key in _workers


def drop(session_key: str) -> int:
    """丢掉这条会话排队的那几条，返回条数。"""
    with _lock:
        w = _workers.get(session_key)
    return w.clear() if w is not None else 0


def purge(session_key: str) -> None:
    """`/clear` 走这里：把这条会话排队的那几条丢掉，worker 留着。

    不停线程、也不动正在跑的那一轮（`/clear` 本来就是那一轮自己）。
    停掉线程会让挂在它常驻循环上的审批倒计时静默失效 —— 回收是
    `_evict_idle_locked` 的事：条数满了才收，且只收闲着的。
    """
    with _lock:
        w = _workers.get(str(session_key or ""))
    if w is not None:
        w.clear()


def stop(session_key: str) -> bool:
    """销毁一条 worker（显式停通道 / 进程收尾用）。`/clear` 不走这里。

    返回"停掉了吗"：队列里还有活、或者正在跑，就返回 `False`（那条活该跑完）。
    """
    key = str(session_key or "")
    with _lock:
        w = _workers.get(key)
        if w is None:
            return True
        if not w.stop() or not w.stopped():
            return False
        _workers.pop(key, None)
        return True


# 接收路径：分流

async def intake(channel, event, *, on_reply=None) -> None:
    """接收路径的唯一入口：分流 + 入队 + 必要时回一句，然后立刻返回。

    *on_reply* 是"这一轮的回答怎么发回去"的协程函数（`str -> awaitable`）：
    电报 / 微信走它（它们的回答是 `execute` 的返回值），飞书 / 钉钉给 `None`
    （它们自己的 `reply_callback` 已经把回话发掉了）。
    """
    session = getattr(event, "session", None)
    key = session_key_for(session)
    sch = getattr(channel, "_scheduler", None)
    if sch is None or not key:
        print("[im] 没有 scheduler 或没有会话键，这条消息丢掉")
        return
    loop = asyncio.get_running_loop()
    text = str(getattr(getattr(event, "message", None), "content", "") or "").strip()

    # ① `/stop`：在拿锁之前分流
    if text.lower() == "/stop":
        if _handle_stop(key):
            await _note(channel, session, tr(_COPY["stopped"]), event)
        elif _queue_ahead(sch, key):
            await _note(channel, session, tr(_COPY["stopped_bare"]), event)
        else:
            await _note(channel, session, tr(_COPY["idle_stop"]), event)
        return

    ahead = _queue_ahead(sch, key)

    # ② 可 steer 吗
    if ahead and text and _steerable(event, sch, key, text):
        item_id = _msg_id(event)
        if _push_steer(key, item_id, text):
            #: 推完再确认一次在不在跑：轮末与这一推之间有一道缝，
            #:   错过它这条会一直躺在收件箱里（没人再取）。自己摘回来当普通消息跑。
            if not _busy(sch, key):
                from core import steer as _steer
                if _steer.drop(key, item_id):
                    if not submit(key, _turn_job(channel, event, on_reply, loop)):
                        await _note(channel, session,
                                    tr(_COPY["queue_full"]), event)
                        return
            #: 两条出口都回一句 —— 这条已经被收下了，不回就成了"发完没反应"。
            await _note(channel, session, tr(_COPY["steer_ok"]), event)
            return

    # ③ 排队
    if not submit(key, _turn_job(channel, event, on_reply, loop)):
        #: 队列满了：这一条没收下。不说就是"发了没反应"。
        await _note(channel, session, tr(_COPY["queue_full"]), event)
        return
    if ahead:
        note = (_COPY["queued"] if text.startswith("/")
                else _COPY["queued_media"])
        await _note(channel, session, tr(note), event)


def _busy(sch, key: str) -> bool:
    try:
        return bool(sch.is_busy(key))
    except Exception:                                 # noqa: BLE001
        return False


def _queue_ahead(sch, key: str) -> bool:
    """前面还有活吗（正在跑的一轮，或者队里排着的）。"""
    return _busy(sch, key) or pending(key) > 0


def _handle_stop(key: str) -> bool:
    """`/stop`：打断本轮 + 丢弃队列与收件箱。

    返回"这次真的丢了东西吗"（决定回哪一句 —— 一句"剩余消息已取消"在
    什么都没取消的时候就是撒谎，这一类）。
    """
    from core.cancellation import request_cancel
    request_cancel(key)
    #: 在正在跑的那一轮的事件上打个标记：这一轮收尾时框架那句固定的
    #:   「已打断，需要 … 怎么做？」不再发一遍 —— `intake` 已经当场回过
    #:   「已中断」了，两句说一件事。
    #:   不新增任何状态：`approval_timeout` 记的就是那条真事件（同一个对象），
    #:   标记挂在它身上，随这一轮一起消失。
    #:   Web 那头按的打断不打这个标记，那句话照发（那是 IM 用户唯一的信息）。
    try:
        from core import approval_timeout as _AT
        _ev = _AT.event_of(key)
        if _ev is not None and getattr(_ev, "extras", None) is not None:
            _ev.extras["im_stop_ack"] = True
    except Exception as exc:                          # noqa: BLE001
        print("[im] 打 /stop 标记失败（%s: %s）" % (type(exc).__name__, exc))
    dropped = drop(key)
    try:
        from core import steer as _steer
        dropped += _steer.pending(key)
        _steer.clear(key)
    except Exception as exc:                          # noqa: BLE001
        print("[im] 清收件箱失败（%s: %s）" % (type(exc).__name__, exc))
    return bool(dropped)


def _steerable(event, sch, key: str, text: str) -> bool:
    """能不能把这句话塞进正在跑的那一轮（五条判据，与 `/api/steer` 同源）。"""
    if text.startswith("/"):
        return False                                  # 命令：注入进循环没人执行
    chain = getattr(getattr(event, "message", None), "message_chain", None)
    try:
        from channels.message import File, Image, Voice
        if any(isinstance(c, (Image, File, Voice))
               for c in (getattr(chain, "components", None) or [])):
            return False                              # 附件：v1 不注入
    except Exception:                                 # noqa: BLE001
        return False
    try:
        if sch.pending_approval(key) is not None:
            return False                              # 那句话是审批回答
    except Exception:                                 # noqa: BLE001
        return False
    return _target_is_unambiguous(event, sch)


def _target_is_unambiguous(event, sch) -> bool:
    """这条消息要进哪条会话 —— 判不出来就不 steer（fail-closed）。

    `im_routing` 有多条活跃会话又没有认领时会问编号；那时把补话塞进
      "碰巧在忙的那条"就是替用户猜。判据全在 `core/im_routing.py`，这里只读。
    """
    try:
        from core import im_routing
        chan = im_routing.channel_key_of(getattr(event, "session", None))
        if im_routing.bound_session_key(chan):
            return True
        snap = sch.activity_snapshot() if hasattr(sch, "activity_snapshot") else {}
        return len(im_routing.hot_sessions(snap, time.monotonic())) <= 1
    except Exception as exc:                          # noqa: BLE001
        print("[im] 目标会话判不出来（%s: %s）—— 这条不 steer"
              % (type(exc).__name__, exc))
        return False


def _push_steer(key: str, item_id: str, text: str) -> bool:
    try:
        from core import steer as _steer
        _steer.push(key, {"id": item_id, "text": text})
        return True
    except Exception as exc:                          # noqa: BLE001
        print("[im] 补话没塞进收件箱（%s: %s）—— 这条按普通消息排队"
              % (type(exc).__name__, exc))
        return False


def _msg_id(event) -> str:
    """补话的 id：优先用平台消息号（重投时对得上），没有就现造一个。"""
    mid = str(getattr(getattr(event, "message", None), "message_id", "") or "")
    return mid or ("im-" + uuid.uuid4().hex[:12])


# 执行体：跑一轮 + 发回答 + 接管轮末残留

def _is_cancel_line(text: str) -> bool:
    """这句是不是框架自己的取消收尾（不是模型说的话）。"""
    try:
        from core.agent_loop import CANCEL_REPLY
    except Exception:                                     # noqa: BLE001
        return False
    t = str(text or "").strip()
    return bool(t) and t == CANCEL_REPLY


def _suppress_cancel_line(ev, text: str) -> bool:
    """IM 按的 `/stop`：框架那句固定的收尾不再发一遍。

    判据是这句话本身 + 事件上的标记（不读 `extras["cancelled"]`：
      管线调 `reply_callback` 的时刻早于它写那一格 —— 读它永远是 False）。
    """
    extras = getattr(ev, "extras", None) or {}
    return bool(extras.get("im_stop_ack")) and _is_cancel_line(text)


def _wrap_reply_for_stop(ev) -> None:
    """给 `reply_callback`（飞书 / 钉钉那条路）套一层：只丢那句取消收尾，文件照发。"""
    orig = getattr(ev, "reply_callback", None)
    if orig is None or getattr(orig, "_im_stop_wrapped", False):
        return

    def _cb(e):
        try:
            if _suppress_cancel_line(e, (getattr(e, "extras", None) or {}).get("response")):
                e.extras["response"] = ""
        except Exception:                                 # noqa: BLE001
            pass
        orig(e)

    _cb._im_stop_wrapped = True
    ev.reply_callback = _cb


def _turn_job(channel, event, on_reply, loop) -> Callable[[], Awaitable[None]]:
    async def _job() -> None:
        await _run_turn(channel, event, on_reply, loop)
    return _job


async def _run_turn(channel, event, on_reply, loop) -> None:
    """在这个会话的 worker 循环上跑一轮；没赶上的补话接着跑。"""
    sch = getattr(channel, "_scheduler", None)
    if sch is None:
        return
    ev = event
    while True:
        _wrap_reply_for_stop(ev)
        try:
            resp = await sch.execute(ev)
        except Exception as exc:                      # noqa: BLE001
            print("[im] 这一轮抛了（%s: %s）" % (type(exc).__name__, exc))
            traceback.print_exc()
            err = tr(_COPY["error"])
            if on_reply is not None:
                await _hop(loop, on_reply(err))
            else:
                #: 飞书 / 钉钉没有 `on_reply`（回答走 `reply_callback`，而异常不
                #: 走那条路），这里补一句人话，仍然走通道自己的回调
                #: （钉钉要靠它拿 `session_webhook`，换成 `send()` 会发不出去）。
                _cb = getattr(ev, "reply_callback", None)
                if _cb is not None:
                    try:
                        ev.extras["response"] = err
                        _cb(ev)
                    except Exception:                 # noqa: BLE001
                        pass
                else:
                    await _hop(loop, _send_note(channel, ev.session, err, ev))
            return
        text = str(resp or "")
        if _suppress_cancel_line(ev, text):
            #: 当场已经回过「已中断」了 —— 这句只丢文字，文件照发。
            text = ""
        if on_reply is not None:
            await _hop(loop, on_reply(text))
        elif (ev.extras or {}).get("steer_leftover_turn"):
            #: 合成事件没有 `reply_callback`（那是给原渠道那条消息用的），
            #: 它的回话走"主动投递"这条路 —— 与定时任务 / 后台回报同一格。
            await _hop(loop, _send_note(channel, ev.session, text, ev))
        nxt = _leftover_event(ev)
        if nxt is None:
            return
        ev = nxt


#: 把回话投到通道循环上的等待上限（秒）。
#:
#: 必须有上限：通道被停掉时（`/api/channel/stop` → `loop.stop()` + `close()`）
#:   一个已经提交上去、却永远轮不到的协程会让这条会话的 worker 永久卡住
#:   （同会话后面每一条消息都排在一个死掉的 hop 后面）。实际症状：`_hop` 一直
#:   `await`，而通道那边什么都不发生 —— "发消息没反应"。
#: 60 秒远大于任何一次正常回话（上传附件＋发文字）。
_HOP_TIMEOUT_S = 60.0


async def _hop(loop, coro) -> None:
    """把 *coro* 投到通道自己的循环上跑完（客户端绑在那上面，别新造循环）。"""
    if loop is None or loop.is_closed():
        try:
            coro.close()
        except Exception:                             # noqa: BLE001
            pass
        print("[im] 通道的循环已经关了，这条回话发不出去")
        return
    try:
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:                          # noqa: BLE001
        try:
            coro.close()
        except Exception:                             # noqa: BLE001
            pass
        print("[im] 回话投递失败（%s: %s）" % (type(exc).__name__, exc))
        return
    try:
        await asyncio.wait_for(asyncio.wrap_future(fut), timeout=_HOP_TIMEOUT_S)
    except asyncio.TimeoutError:
        #: 取消不一定能取消对面那个协程（它在另一个循环上），但至少
        #:   这一条 worker 不会永远卡住 —— 卡住比"少发一句话"贵得多。
        fut.cancel()
        print("[im] 回话投递超时（%.0fs）—— 通道可能已经停了" % _HOP_TIMEOUT_S)
    except Exception as exc:                          # noqa: BLE001
        print("[im] 回话投递失败（%s: %s）" % (type(exc).__name__, exc))


async def _send_note(channel, session, text: str, event=None) -> bool:
    fn = getattr(channel, "send_note", None) or getattr(channel, "send", None)
    if fn is None or not str(text or "").strip():
        return False
    try:
        return bool(await fn(session, text, event=event))
    except Exception as exc:                          # noqa: BLE001
        print("[im] 旁白没发出去（%s: %s）" % (type(exc).__name__, exc))
        return False


async def _note(channel, session, text: str, event=None) -> None:
    """接收路径上的旁白（ack / 已中断）—— 不许弄挂收消息。

    *event* 是触发它的那条消息：钉钉要靠它的 `session_webhook` 才发得出去。
    """
    ok = await _send_note(channel, session, text, event=event)
    if not ok:
        #: 不静默：旁白发不出去时用户以为"它没理我"，而这与"消息没进来"同形。
        print("[im] 旁白未送达（%s）" % _short_key(str(getattr(session, "session_id", "") or "")))


def _leftover_event(ev):
    """这一轮没赶上的补话，一条合成事件（接着跑一轮）；没有就 `None`。"""
    extras = getattr(ev, "extras", None) or {}
    if extras.get("cancelled") or extras.get("awaiting_approval"):
        return None
    items = list(extras.get("steer_leftover") or [])
    key = str(extras.get("routed_session_key") or "") or session_key_for(
        getattr(ev, "session", None))
    #: 再取一次：轮末的 drain 与这里之间还有一道缝（推在 drain 之后的那条）。
    try:
        from core import steer as _steer
        items += _steer.drain(key)
    except Exception as exc:                          # noqa: BLE001
        print("[im] 取残留补话失败（%s: %s）" % (type(exc).__name__, exc))
    text = "\n".join(str(i.get("text") or "").strip()
                     for i in items if str(i.get("text") or "").strip())
    if not text.strip():
        return None
    return _synthetic_event(ev, key, text)


def _synthetic_event(ev, key: str, text: str):
    """照 `core/tasks_notify.py` 的形状造一条用户口吻的事件。

    不带 `origin`（`background` / `cron` 会被落成 `kind=framework_note`）——
      这几句是用户自己说的，界面上该长成用户气泡。
    带 `routed_session_key`，`im_routing.route()` 直接放过（不再问一句"进哪条会话"）。
    """
    from channels.base import ChannelMetadata, Message as ChannelMessage, MessageEvent
    from channels.message import MessageChain

    session = ev.session
    msg = ChannelMessage(
        sender_id=str(getattr(getattr(ev, "message", None), "sender_id", "") or ""),
        message_chain=MessageChain.from_text(text),
        sender_name=str(getattr(getattr(ev, "message", None), "sender_name", "") or ""),
        message_type=session.message_type,
        session=session,
    )
    return MessageEvent(
        session=session,
        message=msg,
        platform_meta=ChannelMetadata(
            name=str(getattr(getattr(ev, "platform_meta", None), "name", "") or "im"),
            description="steer leftover",
            platform_type=str(getattr(session, "platform", "") or ""),
        ),
        extras={"routed_session_key": key, "steer_leftover_turn": True},
    )
