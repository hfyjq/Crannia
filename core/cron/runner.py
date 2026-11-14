"""周期任务的 runner —— 守护线程 + 执行 + 投递。

三个模块的分工：

    expr.py   规则 -> 下一次什么时候跑（纯函数）
    store.py  两张表：认领 / 结算 / 派生统计
    runner.py 把到点的那条跑起来，再把结果投回去（本模块）

## 一条不许动的判据：走同一条 `execute()`

到点自动执行的前提是「围栏一条没少」
（`deny` 档 / 审批浮窗 / 内容隔离 / 自动审查器）。
这里必须调真正的管线（`PipelineScheduler.execute`），
不许为了"跑得干净"另开一条捷径 —— 那等于偷偷把围栏摘了。

连带的两件事都是白拿的（不要重复实现）：
  · 并发名额：`execute()` 自己就 `_acquire_turn_slot`（每会话锁 + 上限 3），
    所以定时任务与用户轮次共用同一本账；
  · 审批 / 隔离 / 台账：全在阶段里，一条不少。

## `reply_callback` 不设

投递只留一个地方（本模块，用 `execute()` 的返回值）。
不设回调是为了与电报/CLI 那两个通道同一个形状 —— 它们也是拿返回值发的。

## 已知风险

  · 各通道的 `send()` 在定时任务这条路上调用。失败一律落
    `cron_runs.status=error` + `reason`，绝不静默。
  · 通道没在跑，投不出去（`send()` 依赖已建立的连接）—— 同样是"记下来、别装没事"。
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from channels.base import (ChannelMetadata, MessageEvent, MessageSession,
                            Message as ChannelMessage, MessageType)
from channels.message import MessageChain
from core.cron import expr
from core.cron.store import CronStore, get_cron_store
from core.events import (EVT_CRON_END, EVT_CRON_START, EVT_TOOL_END,
                            EVT_TOOL_START, get_event_bus)
from core.session_key import session_key_for

#: 轮询间隔（秒）。
POLL_SECONDS = 20.0

#: 错过多久就不补了。
#:
#: 判据：逾期很久的任务不补跑。
#:   · 常见做法是用 5 分钟宽限（`MISFIRE_GRACE_MS`），超过就跳过并把原因
#:     持久化，原因写的是「电脑睡了 / 应用没在跑」；
#:   · 也有启动后先静默 60 秒再恢复的；
#:   · 还有把逾期的往前推的（跳过、不补）。
#:
#: 为什么"不补"是对的：一条「每天早上八点半总结新闻」的任务，你下午三点才开机，
#: 补跑那份早上八点半的新闻意义不大 —— 而且它会在你最不该被打扰的时候
#: 把一份过期的结果塞给你。
#: 宽限内的照跑（机器抖一下、应用重启几秒，那条该跑还得跑）。
MISFIRE_GRACE_SECONDS = 300.0

#: 跳过时记在台账上的原因（`reason`）。跳过必须留下可查的说明。
R_MISSED = "missed_late"

#: 失败退避：30 秒起步、每失败一次翻倍、封顶 15 分钟。
BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 900.0

#: 连续失败到这里就停用（并投一条通知，别静默死掉）。
MAX_FAIL_STREAK = 5

#: 投递文本封顶 —— 与 `core/events.py::_TEXT_CAPS["response"]` 同一个量级。
RESULT_MAX_CHARS = 4000

#: "落库即算交付"的通道。
#:
#: `web` 没有通道对象 —— `channels/` 下只有飞书/微信/电报/钉钉/CLI，
#: 所以 `resolve_channel("web")` 恒为 `None`，而它不是故障：
#: Web 会话的交付本来就是"消息落库 + 事件流"（Web 会话没有 push 通道）
#: —— 管线跑完已经写进了那一段历史，界面开着就经 `task.end` 看得见、
#: 关着下次打开也看得见。
#:不调 `send()`、也不算失败。不加这一条的话，面板建的任务
#: 每跑一次都会记一行 `channel_not_running` 的假失败。
PERSIST_ONLY_PLATFORMS = frozenset({"web"})

#: 台账要记的那两类事件。
#:
#: 判据："到点时没人在场"那个洞，唯一的补法是"它做了什么"看得见。
#: 而"做了什么"只有 `tool.*` 两件事说得清 —— `turn.*` 是噪音、`delta` 是碎片。
#:
#: 必须按类型订阅，不能全收：订阅队列是有界的（满了丢最旧），
#: 而一轮 agent 会长出成百上千个 `delta` 分片，全收的订阅者会把 `tool.*`
#: 挤掉，于是"这一次跑了哪些工具"数不全（而它看起来像"就跑了两步"）。
_ACTION_TYPES = frozenset({EVT_TOOL_START, EVT_TOOL_END})

#: 台账里最多记几个动作。超了要说出来（`"n"` 是真数：
#: 报数，不报字符 —— 只说"给你这么多"而总数另说是撒谎）。
MAX_ACTIONS_RECORDED = 30


def _collect_actions(sub) -> str:
    """把这一次的工具调用折成一行 JSON。绝不抛（观测不阻断）。"""
    if sub is None:
        return ""
    import json as _json
    import queue as _queue

    started, order, failed = {}, [], set()
    try:
        while True:
            ev = sub.q.get_nowait()
            if ev.type == EVT_TOOL_START:
                cid = str(ev.data.get("call_id") or "")
                started[cid] = ev.data
                order.append(cid)
            elif ev.type == EVT_TOOL_END:
                cid = str(ev.data.get("call_id") or "")
                if not ev.data.get("ok", True):
                    failed.add(cid)
                # 没有 start 的 end（队列丢过一条）也要留痕 —— 见下面那段。
                if cid not in started:
                    started[cid] = ev.data
                    order.append(cid)
    except _queue.Empty:
        pass
    except Exception:                                # noqa: BLE001
        pass

    items = []
    for cid in order[:MAX_ACTIONS_RECORDED]:
        d = started.get(cid) or {}
        items.append({
            "a": str(d.get("activity") or d.get("name") or "?"),
            "ok": cid not in failed,
        })
    if not items and not order:
        return ""
    return _json.dumps({"n": len(order), "list": items}, ensure_ascii=False)


#: 投递失败原因里记的短标签（落台账，给人看）。
#: 跑完了、但一个字都没产出。
#: 它不是失败（工具没报错、管线跑完了），但也绝不能是无声的 `ok` ——
#: 否则台账写 ok、用户那边却一片空白。这是兜底：任何"跑完没话说"
#: 都要在台账上看得见。
R_EMPTY_REPLY = "empty_reply"

R_DELIVER_FAILED = "deliver_failed"

#: 同一批到点超过几条就"另起一段"。
#:
#: 判据：超过这个条数就新开一个对话，每条结果独立输出进去，
#: 并告知用户这是累计响应。
#:
#: 它的靶子是同一时刻到点的多条（比如 5 条都是「每天 08:30」）。
#: 错过很久的任务本来就不补跑，所以不会形成"累计错过"那种爆发。
#: 判据：一次在对话里堆 5 段结果，比把它们放进一个看得见的地方更坏。
BURST_THRESHOLD = 3

#: 投进对话里的那句框架文本。
#:
#: ## 它解决什么
#:
#: 若把任务 prompt 原文直接当用户消息投进去，一个字框都没有，模型看见的是
#: 「用户刚跟我说：每天晚上总结当日5条科技新闻」，没法知道这是自己那条任务
#: 到点了、该去做它，而不是去安排它。
#:
#: 典型走偏：模型 `cron_list` 看到库里那条「晚报」是每天 08:30，
#: 而 prompt 原文说的是「晚上」，它判定排期不对、该改；而它手上没有
#: "改"（`cron_update` 不提供），只能先删后建，撞上兜底闸，挂起，
#: 冻住整页。
#: 它的每一步都是对的 —— 是输入在骗它。
#:
#: ## 一律加框
#:
#: 框架文本要连同正文塞回同一会话，并明确标注这是框架注入的。
#:
#: ## 两条判据
#:
#: · 进正文（不是只进 system prompt）：它是这一轮的输入，而且用户该在
#:   历史里看见"这条为什么冒出来"（否则第二天早上他会以为自己梦游发了句话）。
#: · 要写"不要再建一条 / 不要再改排期"：那正是模型容易走偏的方向，而
#:   `cron_update` 不存在这件事它看不见（工具表里没有负空间）。
_FRAME_TAIL = (
    "不要再建一条同样的任务、也不要改它的排期："
    "要改排期得等用户在对话里说（你手上没有'改'这个工具）。\n"
    "----\n"
)

SCHEDULE_FRAME = (
    "[定时任务] 这是**你自己**给用户定的一条定时任务，现在到点了 —— "
    "照下面这件事去做，然后把结果告诉用户。\n" + _FRAME_TAIL
)


def schedule_frame(burst=None) -> str:
    """`SCHEDULE_FRAME` 的成批版。

    *burst* = `(第几条, 共几条)`；`None` = 单条（逐字等于 `SCHEDULE_FRAME`）。

    成批那句要说清三件：这是同一时刻到点的 N 条之一（不是用户刚说的话）·
    这是第几条（用户看得出进度）· 以及"这是累计响应"。
    尾巴（不要再建 / 不要改排期）两版都有 —— 那是同一条判据，不是成批才有的。
    """
    if not burst:
        return SCHEDULE_FRAME
    i, n = burst
    return (
        "[定时任务] 这是**同一时刻到点的 %d 条任务**里的第 %d 条（**累计响应**）——"
        "不是你刚说的话，是**你自己**给用户定的任务到点了。\n"
        "照下面这件事去做，然后把结果告诉用户；"
        "这是**一批里的一条**，只做这一条，别去碰同批的其他任务。\n" % (n, i)
    ) + _FRAME_TAIL
R_CHANNEL_ABSENT = "channel_not_running"
R_RULE_BROKEN = "rule_unparsable"
R_EXEC_FAILED = "execute_failed"
R_ALREADY_RUNNING = "already_running"


class CronError(RuntimeError):
    """跑不起来 / 投不出去。消息要能直接给人看。"""


# ------------------------------------------------------------------
# 通道解析（投递用）
# ------------------------------------------------------------------

def resolve_channel(platform: str):
    """按 `platform` 找活的通道实例。找不到给 `None`。

    判据是"名字对不上不算找不到"：注册名来自各通道自己的 manifest
    （`web` / `cli` / `feishu` … 有的带 `_channel` 后缀），而
    `MessageSession.platform` 是另一处写的。按三种写法各试一次，
    最后再按通道自己报的名字/类型扫一遍 —— 都比"硬编码一个后缀"稳。
    """
    from bus.registry import _registry
    channels = _registry.get("channels") or {}
    if not platform:
        return None
    for cand in (platform, platform + "_channel", platform.replace("_channel", "")):
        ch = channels.get(cand)
        if ch is not None:
            return ch
    for ch in channels.values():
        try:
            meta = ch.meta()
        except Exception:                            # noqa: BLE001
            continue
        if platform in (getattr(meta, "name", ""), getattr(meta, "platform_type", "")):
            return ch
    return None


# ------------------------------------------------------------------
# runner
# ------------------------------------------------------------------

class CronRunner:
    """守护线程。一个进程一个（见 :func:`get_cron_runner`）。"""

    def __init__(self, *, scheduler: Any = None, store: Optional[CronStore] = None,
                 deliverer: Optional[Callable[[MessageSession, str], None]] = None,
                 interval: float = POLL_SECONDS,
                 grace: float = MISFIRE_GRACE_SECONDS) -> None:
        self._scheduler = scheduler
        self._store = store or get_cron_store()
        self._deliverer = deliverer          # 测试注入；默认走真通道（见 _deliver）
        self._interval = interval
        self._grace = float(grace)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._inflight: set = set()          # 正在跑/正在测试的 job_id
        #: 我为"试跑"造出来的段。
        #: 全仓唯一允许"整个删掉一段对话"的地方用的白名单 —— 见
        #: :meth:`discard_test_segment`。只在这个进程里记（重启后浮窗也早关了）。
        self._test_segments: set = set()
        self._inflight_lock = threading.Lock()
        self._wake = threading.Event()       # `run_now` 要它立刻醒一次

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="cron-runner")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_test_segment(self, storage_id: str) -> bool:
        """这个 `storage_id` 是不是我为一次试跑造的。

        它是"能不能删"的唯一判据 —— 端点上收到的 id 是客户端给的，
        没有这道闸，那行接口就等于"凭一个字符串删掉用户的一段对话"。
        读不到，不认，不删（fail-closed）。
        """
        return bool(storage_id) and str(storage_id) in self._test_segments

    def forget_test_segment(self, storage_id: str) -> None:
        """删掉之后从白名单里摘掉（幂等：不在了也不报错）。"""
        self._test_segments.discard(str(storage_id or ""))

    def job_count(self) -> int:
        """库里现在有几条任务（bootstrap 那行自检要打印它）。"""
        return self._store.count()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:                 # noqa: BLE001
                # 守护线程绝不因为一次 tick 崩掉而整个消失（静默 = 任务再也不跑了）
                print("[cron] tick 出错（继续跑）：%s: %s" % (type(exc).__name__, exc))
            self._wake.wait(self._interval)
            self._wake.clear()

    # ---- 一轮 ----

    def tick(self, now: Optional[datetime] = None) -> list:
        """回收僵尸 -> 认领到点的 -> 每条派一个线程去跑。返回这一轮派了哪些 id。

        不在这里等它跑完：一轮 agent 可能跑十几分钟，等它会把整个轮询
        拖住（后面的任务全部迟到）。
        """
        now = now or datetime.now().astimezone()
        self._store.reap_stale(now)

        # 先看逾期多久，再决定认不认领 ——
        #   ≤ 宽限，照跑；> 宽限，不跑，只记一笔。
        ready, claimed_ids = [], []
        for job in self._store.due_jobs(now):
            late = self._lateness_seconds(job, now)
            if late is None or late <= self._grace:
                got = self._store.claim_one(job["id"])
                if got is not None:                  # 别人先认领了就算了（原子认领）
                    ready.append(got)
                    claimed_ids.append(got["id"])
            else:
                self._skip_missed(job, now, late)
        # 这一批超过门槛就整体改道（另起一段 + 成批话术）。
        if len(ready) > BURST_THRESHOLD:
            batch = "cron-batch:%s:%s" % (
                now.strftime("%Y%m%dT%H%M"), ",".join(j["id"] for j in ready))
            for i, job in enumerate(ready, 1):
                job["_burst"] = (i, len(ready))
                job["_batch"] = batch
            print("[cron] 这一批有 %d 条（>%d）—— 改投到一个新对话里"
                  % (len(ready), BURST_THRESHOLD))
        for job in ready:
            self._spawn(job, trigger="schedule")
        return claimed_ids

    @staticmethod
    def _lateness_seconds(job: dict, now: datetime):
        """这条逾期了多少秒（`next_run` 读不出来，`None`）。"""
        raw = job.get("next_run")
        if not raw:
            return None
        try:
            due = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        if due.tzinfo is None:
            due = due.astimezone()
        return (now - due).total_seconds()

    def _skip_missed(self, job: dict, now: datetime, late: float) -> None:
        """不补了，但要记清楚。

        一次性任务在这里终结（`next_run=None` + `enabled=0`，见下面那段注释）——
           "提醒我明天九点开会"的三天后开机补跑，是没有意义的。
        循环任务从"现在"算下一次（不追积压）—— 判据同 `expr.next_after`：
           它给的是"严格晚于 now 的下一次"，所以跳过的那些不会在下一轮冒出来。
        全程不认领（不置 `running`）：这条任务压根没跑，锁碰都不该碰。
        """
        jid = job["id"]
        hours = late / 3600.0
        detail = "逾期 %.1f 小时（超过 %.0f 分钟的宽限，不补跑）" % (
            hours, self._grace / 60.0)
        try:
            rule = expr.parse_rule(json.loads(job.get("rule_json") or "{}"))
        except Exception:                            # noqa: BLE001
            # 规则坏了，这条永远跑不起来，停用它（与 `_execute` 那边同一条判据）
            self._store.finish(jid, status="missed", trigger="schedule",
                               reason=R_RULE_BROKEN, error=detail, enabled=0,
                               started_at=now.isoformat())
            return
        # 这里没有 `if rule.kind == "once"` 那一条 —— 它不是省了，是观察不到：
        #   一次性任务的 `when` 一旦过去，`next_after` 本来就返回 `None`
        #走这条通用路照样得到 `next_run=None, enabled=0`。
        #    留一条测不到的守卫比没有它更坏 —— 读者会以为它在承重。
        nxt = expr.next_after(rule, now)
        next_run = nxt.isoformat() if nxt else None
        enabled = 0 if nxt is None else None
        self._store.finish(jid, status="missed", trigger="schedule",
                           reason=R_MISSED, error=detail,
                           next_run=next_run, enabled=enabled,
                           started_at=now.isoformat())
        print("[cron] 跳过 '%s'：%s" % (job.get("title") or jid, detail))

    def _spawn(self, job: dict, trigger: str) -> None:
        with self._inflight_lock:
            self._inflight.add(job["id"])
        t = threading.Thread(target=self._run_guarded, args=(job, trigger),
                             daemon=True, name="cron-job-%s" % job["id"][-6:])
        t.start()

    def _run_guarded(self, job: dict, trigger: str) -> None:
        try:
            self._execute(job, trigger)
        except Exception as exc:                     # noqa: BLE001
            # 兜底：任何没被下面接住的异常都要落账，否则这条任务会一直是
            # `running=1`（要等僵尸回收线才放出来）。
            print("[cron] %s 执行时抛了 %s: %s" % (job["id"], type(exc).__name__, exc))
            try:
                self._store.finish(job["id"], status="error", trigger=trigger,
                                   reason=R_EXEC_FAILED, error=str(exc))
            except Exception:                        # noqa: BLE001
                pass
        finally:
            with self._inflight_lock:
                self._inflight.discard(job["id"])

    # ---- 执行一条 ----

    def _execute(self, job: dict, trigger: str) -> dict:
        job_id = job["id"]
        started = datetime.now().astimezone()
        route_key = self._route_key(job)

        # 规则坏了，这条任务永远跑不起来。停用它并记清楚（别让它每 20 秒重试到天荒地老）。
        try:
            rule = expr.parse_rule(json.loads(job["rule_json"]))
        except Exception as exc:                     # noqa: BLE001
            self._store.finish(job_id, status="error", trigger=trigger,
                               reason=R_RULE_BROKEN, error=str(exc),
                               enabled=0, started_at=started.isoformat())
            self._emit(EVT_CRON_END, route_key, job, trigger, "error", R_RULE_BROKEN)
            return {"ok": False, "reason": R_RULE_BROKEN, "error": str(exc)}

        self._emit(EVT_CRON_START, route_key, job, trigger, "running", "")

        text, err, sid, acts = self._execute_prompt(job, route_key)
        if err:
            self._settle_failure(job, rule, trigger, started, R_EXEC_FAILED, err,
                                 route_key, actions=acts, storage_id=sid)
            return {"ok": False, "reason": R_EXEC_FAILED, "error": err}

        # 投递：先投后落账没意义（账要记"投出去没有"），所以结果一并写进去
        deliver_err = self._deliver(job, text)
        if deliver_err:
            self._settle_failure(job, rule, trigger, started, R_DELIVER_FAILED,
                                 deliver_err, route_key, actions=acts, storage_id=sid)
            return {"ok": False, "reason": R_DELIVER_FAILED, "error": deliver_err,
                    "text": text}

        self._settle_success(job, rule, trigger, started, route_key,
                             storage_id=sid, empty=not text.strip(), actions=acts)
        return {"ok": True, "text": text}

    def _execute_prompt(self, job: dict, route_key: str):
        """合成事件 -> 真管线。返回 `(回复文本, 错误文本, storage_id, actions)`。

        `storage_id` 由这一轮自己写回 `event.extras`（`pre_process` 那个装载点）
        —— 台账里记它，是为了回答"这一次落在哪一段"（`/clear` 之后尤其要看得出）。
        别拿 `bound_storage_id(channel)` 去推：`deliver="new"` 那一路故意不重绑，
        推出来的会是用户当前那段，而不是这一轮真正写进去的那段。
        """
        if self._scheduler is None:
            return "", "框架还没起来（没有 scheduler）—— 定时任务跑不了", "", ""
        try:
            session = MessageSession.from_str(job["channel"])
        except Exception as exc:                     # noqa: BLE001
            return "", "投递目标记坏了：%r（%s）" % (job.get("channel"), exc), "", ""

        event = self._build_event(job, session, route_key)
        # 台账：跑之前先订上（`_ACTION_TYPES` 那两类），跑完再取。
        #   顺序反了就什么都收不到。
        sub = None
        try:
            sub = get_event_bus().subscribe(session_key=route_key,
                                            types=_ACTION_TYPES)
        except Exception:                            # noqa: BLE001
            sub = None                               # 订阅不上也照跑（观测不阻断）
        try:
            text = asyncio.run(self._scheduler.execute(event))
        except Exception as exc:                     # noqa: BLE001
            return "", "%s: %s" % (type(exc).__name__, exc), "", _collect_actions(sub)
        finally:
            if sub is not None:
                try:
                    get_event_bus().unsubscribe(sub)
                except Exception:                    # noqa: BLE001
                    pass
        sid = str((getattr(event, "extras", None) or {}).get("storage_id") or "")
        return (text or ""), "", sid, _collect_actions(sub)

    def _build_event(self, job: dict, session: MessageSession, route_key: str) -> MessageEvent:
        """手搓一个 `MessageEvent` —— 形状照 Web UI 那份现成的
        （`web/server.py::_run_chat_turn`，它不是通道调用者却照样能跑一轮）。

        `extras` 里那两格是承重的：
          · `origin="cron"` —— 让 `core/im_routing.route()` 立刻放过
            （不然"≥2 条活跃，问编号"会把任务扣在一个到点时不在场的人身上）；
          · `routed_session_key` —— 这一次该进哪条会话（`execute()` 直接取它）。
        """
        msg = ChannelMessage(
            sender_id="cron",
            # 加框，不是原样投（判据见 `SCHEDULE_FRAME`）。
            message_chain=MessageChain.from_text(
                schedule_frame(job.get("_burst")) + (job.get("prompt") or "")),
            sender_name="定时任务",
            message_type=session.message_type,
            session=session,
        )
        extras: dict = {"origin": "cron", "job_id": job["id"]}
        if route_key:
            extras["routed_session_key"] = route_key
        burst = job.get("_burst")
        test_seed = job.get("_test_seed")
        if test_seed:
            extras["segment"] = {"detached": True, "seed": str(test_seed)}
        elif (job.get("deliver") or "here") == "new" or burst:
            # 成批时一律另起一段（新开一个对话，结果给用户看）。
            #   同批各条带同一个 seed => 算出同一个段（它们并行，无法互相协商）。
            seg = {"detached": True}
            if burst:
                seg["seed"] = str(job.get("_batch") or "")
            extras["segment"] = seg
        return MessageEvent(
            session=session,
            message=msg,
            platform_meta=ChannelMetadata(
                name="cron", description="周期任务", platform_type=session.platform),
            extras=extras,
        )

    def _route_key(self, job: dict) -> str:
        """这一次跑进哪条会话。

        通道绑过就跟着绑（用户在界面上把这条通道接到别处了）——
        这与"最新活跃"无关，所以用 `bound_session_key()` 而不是 `route()`。
        """
        try:
            from core import im_routing
            bound = im_routing.bound_session_key(job.get("channel") or "")
        except Exception:                            # noqa: BLE001
            bound = None
        if bound:
            return bound
        try:
            return session_key_for(MessageSession.from_str(job["channel"]))
        except Exception:                            # noqa: BLE001
            return ""

    # ---- 投递 ----

    def _deliver(self, job: dict, text: str, override=None) -> str:
        """投出去。返回 `""` = 成功；否则返回给人看的原因。

        *override* = 这一次换一个投递器（HTTP 端点那条：把结果原样返回给
        调用方，而不是发进任务的通道 —— 判据见 :meth:`run_now`）。
        """
        if not text.strip():
            return ""                                # 空回复不算投递失败（没东西可发）
        try:
            session = MessageSession.from_str(job["channel"])
        except Exception as exc:                     # noqa: BLE001
            return "投递目标记坏了：%r（%s）" % (job.get("channel"), exc)

        picker = override if override is not None else self._deliverer
        if picker is not None:
            try:
                picker(session, text[:RESULT_MAX_CHARS])
                return ""
            except Exception as exc:                 # noqa: BLE001
                return "%s: %s" % (type(exc).__name__, exc)

        if session.platform in PERSIST_ONLY_PLATFORMS:
            # 落库即算交付（见 `PERSIST_ONLY_PLATFORMS` 那段）。
            return ""

        channel = resolve_channel(session.platform)
        if channel is None:
            return "%s（这条通道现在没在跑）" % R_CHANNEL_ABSENT
        try:
            delivered = asyncio.run(channel.send(session, text[:RESULT_MAX_CHARS]))
        except Exception as exc:                     # noqa: BLE001
            return "%s: %s" % (type(exc).__name__, exc)
        #: 不抛异常 ≠ 送达。
        #:   钉钉那时只打印、飞书吞错、Telegram 没 Bot 时什么都不做 ——
        #:   而这里看见"没抛"就把任务记成了 `ok`。判据换成通道自己的回执：
        #:   只有 `True` 才算送达；`False` / `None` 一律按失败记账（fail-closed）。
        #:   通道那一侧不许"忘了 `return True`"—— 忘了的代价是这条任务永远失败，
        #:   看得见、也不会静默变成一个假的成功。
        if delivered is not True:
            return "通道报告未送达（%s）" % session.platform
        return ""

    # ---- 结算 ----

    def _settle_success(self, job, rule, trigger, started, route_key,
                        storage_id: str = "", empty: bool = False,
                        actions: str = "") -> None:
        prev = self._parse_dt(job.get("next_run"))
        now = datetime.now().astimezone()
        nxt = expr.next_after(rule, now, prev=prev)
        enabled = 0 if nxt is None else None      # 没有下一次（一次性跑完），停用
        self._store.finish(job["id"], status="ok", trigger=trigger,
                           next_run=(nxt.isoformat() if nxt else None),
                           enabled=enabled, started_at=started.isoformat(),
                           storage_id=storage_id, actions=actions,
                           reason=(R_EMPTY_REPLY if empty else ""))
        self._emit(EVT_CRON_END, route_key, job, trigger, "ok", "",
                   next_run=(nxt.isoformat() if nxt else None))

    def _settle_failure(self, job, rule, trigger, started, reason, error, route_key,
                        actions: str = "", storage_id: str = "") -> None:
        st = self._store.stats(job["id"])
        streak = st.get("fail_streak", 0) + (1 if trigger == "schedule" else 0)
        secs = min(BASE_BACKOFF_SECONDS * (2 ** max(0, streak - 1)), MAX_BACKOFF_SECONDS)
        nxt = (datetime.now().astimezone() + timedelta(seconds=secs)).isoformat()
        # 这里不需要再判 `trigger == "schedule"` —— `streak` 本身已经只数
        #   自动那本账（`store.stats` 的 `fail_streak` 按 `trigger` 过滤）。
        #   多余的 `and trigger == "schedule"` 观察不到差别，因此不保留：
        #     留一条测不到的守卫，比没有它更坏（读者会以为它在承重）。
        enabled = 0 if streak >= MAX_FAIL_STREAK else None
        # 一次性任务失败了不该永远重试：它没有"下一次"，直接停用并留着这条错。
        if rule.kind == "once":
            enabled, nxt = 0, None
        self._store.finish(job["id"], status="error", trigger=trigger, reason=reason,
                           error=error, next_run=nxt, enabled=enabled,
                           actions=actions, storage_id=storage_id,
                           started_at=started.isoformat())
        self._emit(EVT_CRON_END, route_key, job, trigger, "error", reason)

    @staticmethod
    def _parse_dt(raw) -> Optional[datetime]:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.astimezone()

    # ---- 事件 ----

    def _emit(self, evt: str, route_key: str, job: dict, trigger: str,
              status: str, reason: str, next_run=None) -> None:
        """发一条事件。发不出去不许影响跑本身（同 `memory.saved` 的判据：
        观测绝不阻断）。"""
        try:
            get_event_bus().emit(evt, session_key=route_key or "",
                                 job_id=job["id"], title=job.get("title") or "",
                                 trigger=trigger, status=status, reason=reason,
                                 next_run=next_run)
        except Exception:                            # noqa: BLE001
            pass

    # ---- 手动「立即执行一次」 ----

    def run_now(self, job_id: str, deliverer=None, isolate: bool = False) -> dict:
        """测试运行：立刻跑一次，结果投给发起测试的那一处。

        四条判据（环环都是为了"别让测试污染真账"）：

          · 投递目标 = 调用方指定的那处（`deliverer`），不是任务配置的目标
            —— 否则 `deliver="new"` 的任务点下去什么都看不见，测试就白点了；
          · 写 `cron_runs` 但标 `trigger="manual"`（台账能分开数）；
          · 不推 `next_run`、不计退避、不计「连续失败，停用」
            —— 手点一下就把下次真跑的时间挪了，是两件事写进一格；
          · 同一任务同时只允许一次运行（`claim_one` 就是那把锁）。
        """
        job = self._store.get(job_id)
        if job is None:
            return {"ok": False, "reason": "not_found", "error": "没有这条任务"}
        claimed = self._store.claim_one(job_id)
        if claimed is None:
            return {"ok": False, "reason": R_ALREADY_RUNNING,
                    "error": "这条任务正在运行中 —— 等它跑完再试"}

        with self._inflight_lock:
            self._inflight.add(job_id)
        started = datetime.now().astimezone()
        route_key = self._route_key(claimed)
        if isolate:
            # 试跑不许碰用户的对话（那是隔离试跑的全部意义）。
            #   带一个每次运行都不同的 seed，`pre_process` 铸一段独立的，
            #   而且不重绑通道（`open_detached_thread` 那一族）。
            claimed["_test_seed"] = "cron-test:%s:%s" % (
                job_id, started.isoformat())
        self._emit(EVT_CRON_START, route_key, claimed, "manual", "running", "")
        try:
            try:
                rule = expr.parse_rule(json.loads(claimed["rule_json"]))
            except Exception as exc:                 # noqa: BLE001
                self._store.finish(job_id, status="error", trigger="manual",
                                   reason=R_RULE_BROKEN, error=str(exc),
                                   started_at=started.isoformat())
                self._emit(EVT_CRON_END, route_key, claimed, "manual", "error", R_RULE_BROKEN)
                return {"ok": False, "reason": R_RULE_BROKEN, "error": str(exc)}

            text, err, sid, acts = self._execute_prompt(claimed, route_key)
            if err:
                self._store.finish(job_id, status="error", trigger="manual",
                                   reason=R_EXEC_FAILED, error=err,
                                   actions=acts, storage_id=sid,
                                   started_at=started.isoformat())
                self._emit(EVT_CRON_END, route_key, claimed, "manual", "error", R_EXEC_FAILED)
                return {"ok": False, "reason": R_EXEC_FAILED, "error": err}

            deliver_err = self._deliver(claimed, text, override=deliverer)
            reason = (R_DELIVER_FAILED if deliver_err
                      else (R_EMPTY_REPLY if not text.strip() else ""))
            self._store.finish(job_id, status=("error" if deliver_err else "ok"),
                               trigger="manual", reason=reason,
                               error=deliver_err or "", storage_id=sid,
                               actions=acts,
                               started_at=started.isoformat())
            self._emit(EVT_CRON_END, route_key, claimed, "manual",
                       "error" if deliver_err else "ok",
                       R_DELIVER_FAILED if deliver_err else "")
            if isolate and sid:
                self._test_segments.add(sid)      # 关浮窗时只有这里面的 id 能被删
            out = {"ok": not deliver_err, "text": text, "trigger": "manual",
                   "storage_id": sid}
            if deliver_err:
                out.update({"reason": R_DELIVER_FAILED, "error": deliver_err})
            return out
        finally:
            with self._inflight_lock:
                self._inflight.discard(job_id)


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------

_runner: Optional[CronRunner] = None
_runner_lock = threading.Lock()


def get_cron_runner() -> CronRunner:
    """进程内单例。 `scheduler` 必须由 bootstrap 显式注入（见 :func:`configure`）。"""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = CronRunner()
        return _runner


def configure(scheduler: Any, **kw) -> CronRunner:
    """bootstrap 调这一处：把 scheduler 交进来并起线程。幂等。"""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = CronRunner(scheduler=scheduler, **kw)
        else:
            _runner._scheduler = scheduler          # noqa: SLF001 - 同一模块内的私有约定
    _runner.start()
    return _runner


def reset_cron_runner() -> None:
    """给测试用。"""
    global _runner
    with _runner_lock:
        if _runner is not None:
            _runner.stop()
        _runner = None
