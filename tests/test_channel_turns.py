# -*- coding: utf-8 -*-
"""通道「收/跑」解耦—— 队列语义 / 入口分流 / 轮末接管。

## 钉住的六件事（缺一件就"看起来做了"）

  1. 队列语义：`submit` 立刻返回、同会话严格 FIFO、异常不中断消费。
  2. worker 的循环常驻：轮次跑完之后 `call_later` 还能响 —— 这是
     审批倒计时（`approval_timeout.arm` 挂的就是"当时正在跑的那个 loop"）
     活着的唯一证据。用 `asyncio.run` 一轮一个循环会让它静默失效。
  3. `/stop` 三种口（有排队 / 只有轮次在跑 / 什么都没跑）各自一句，
     没取消东西时不许说"剩余消息已取消"（标签在说谎）。
  4. 四条不 steer 的判据：命令 / 附件 / 待审批 / 目标不唯一。
  5. 轮末残留接管：没赶上的补话自动接着跑一轮；被打断的与
     停在待审批的那两轮不许接管。
  6. 文案同步翻译：`_COPY` 里每一句都得在 `locales/en.json` 里有译文
     （要求"这些消息都注意同步翻译"）。
  7. `/clear` 只丢队列、不动 worker（停线程会让挂在上面的审批
     定时器静默失效）；队列有上限、满了要回一句实话；worker 条数有上限，
     满了先收最闲的（有审批定时器挂着的不收）。

ASCII-only 输出（Windows 控制台 GBK）。

用法：
    .venv/Scripts/python.exe tests/test_channel_turns.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.channel_turns as CT                                        # noqa: E402
from channels.base import (ChannelMetadata, Message as ChMessage,      # noqa: E402
                           MessageEvent, MessageSession, MessageType)
from channels.message import Image, MessageChain                       # noqa: E402
from core.session_key import session_key_for                           # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(desc: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    ok = bool(ok)
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + detail) if detail and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


def _wait_until(pred, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return bool(pred())


# 夹具：一条只属于这个测试的会话 / 一条假通道 / 一个假调度器

def _session(tag: str) -> MessageSession:
    """群聊 + 随机尾巴：别撞上真会话（`workspace:private` 是共用的）。"""
    return MessageSession(platform="testch", message_type=MessageType.GROUP,
                          session_id="ct-%s-%s" % (tag, uuid.uuid4().hex[:6]))


def _event(text: str, session: MessageSession, *, media: bool = False) -> MessageEvent:
    chain = MessageChain.from_text(text)
    if media:
        chain.components.append(Image(url="http://example.invalid/x.png"))
    msg = ChMessage(sender_id="u1", sender_name="u1", message_chain=chain,
                    message_type=session.message_type, session=session,
                    message_id="m-" + uuid.uuid4().hex[:8])
    return MessageEvent(
        session=session, message=msg,
        platform_meta=ChannelMetadata(name="testch", description="testch",
                                      platform_type="testch"))


class _Chan:
    """假通道：只记旁白。"""

    def __init__(self, sch) -> None:
        self._scheduler = sch
        self.notes: list = []
        self._lock = threading.Lock()

    async def send_note(self, session, text: str, event=None) -> bool:
        with self._lock:
            self.notes.append(str(text))
        return True

    async def send(self, session, content) -> bool:
        return await self.send_note(session, content)


class _Sch:
    """假调度器：`execute` 只记账（`on_execute` 可改形状）。"""

    def __init__(self, *, busy: bool = False, pending=None, snap=None) -> None:
        self.busy = busy
        self._pending = pending
        self.snap = snap or {}
        self.turns: list = []
        self.on_execute = None
        self._lock = threading.Lock()

    def is_busy(self, key: str) -> bool:
        return self.busy

    def pending_approval(self, key: str):
        return self._pending

    def activity_snapshot(self) -> dict:
        return dict(self.snap)

    async def execute(self, event):
        with self._lock:
            self.turns.append(str(event.message.content))
            n = len(self.turns)
        if self.on_execute is not None:
            return self.on_execute(event, n)
        return "答复%d" % n


class _Loop:
    """一条常驻的"通道循环"（旁白 / 回答都往它上面投）。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 10.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:                                     # noqa: BLE001
            pass


def _collector(out: list):
    async def _on_reply(text: str) -> None:
        out.append(str(text))
    return _on_reply


# 1. 队列语义

def test_queue_fifo():
    print("\n-- 1. 队列：立刻返回 / 同会话 FIFO / 异常不中断 --")
    sess = _session("fifo")
    key = session_key_for(sess)
    gate = threading.Event()
    seen: list = []

    def _job(tag: str):
        async def _run() -> None:
            seen.append("start-" + tag)
            await asyncio.get_running_loop().run_in_executor(None, gate.wait)
            seen.append("end-" + tag)
        return _run

    t0 = time.monotonic()
    for tag in ("a", "b", "c"):
        CT.submit(key, _job(tag))
    cost = time.monotonic() - t0
    check("★ submit 立刻返回（三次合计 < 0.2s）", cost < 0.2, "%.3f s" % cost)
    check("a 已经开始跑", _wait_until(lambda: "start-a" in seen), repr(seen))
    check("★ 队里还剩两条没开始", CT.pending(key) == 2, str(CT.pending(key)))
    gate.set()
    check("三条都跑完", _wait_until(lambda: len(seen) == 6), repr(seen))
    check("★★ 同一会话严格 FIFO（不是「谁先抢到锁」）",
          seen == ["start-a", "end-a", "start-b", "end-b", "start-c", "end-c"],
          repr(seen))

    # 异常不许把消费协程带走（否则整条会话永久卡死，症状是"发消息没反应"）
    sess2 = _session("boom")
    key2 = session_key_for(sess2)
    after: list = []

    def _boom():
        async def _run():
            raise RuntimeError("boom")
        return _run

    def _ok():
        async def _run():
            after.append(1)
        return _run

    CT.submit(key2, _boom())
    CT.submit(key2, _ok())
    check("★ 前一条炸了，后一条照跑", _wait_until(lambda: after), repr(after))

    CT.purge(key)
    CT.purge(key2)


def test_loop_stays_alive():
    print("\n-- 2. worker 的循环常驻（审批倒计时挂的就是它）--")
    sess = _session("loop")
    key = session_key_for(sess)
    fired: list = []
    names: list = []

    def _job():
        async def _run() -> None:
            names.append(threading.current_thread().name)
            asyncio.get_running_loop().call_later(
                0.05, lambda: fired.append(time.monotonic()))
        return _run

    CT.submit(key, _job())
    _wait_until(lambda: names)
    check("★ 轮次跑在**另一条线程**上（不是收消息那条）",
          bool(names) and names[0] != threading.current_thread().name,
          repr(names))
    check("★★ 轮次**结束之后**循环还活着（`call_later` 还会响）",
          _wait_until(lambda: fired, 3),
          "用 asyncio.run / run_until_complete 时这里恒空 —— 审批超时会静默失效")
    CT.purge(key)


# 3. `/stop`：三种口 / 丢队列与收件箱

def test_intake_stop():
    print("\n-- 3. `/stop`：三种口各一句 + 真的丢掉排队与收件箱 --")
    from core import cancellation, steer

    # (a) 什么都没在跑
    sess = _session("stop-idle")
    key = session_key_for(sess)
    sch = _Sch(busy=False)
    ch = _Chan(sch)
    L = _Loop()
    try:
        L.run(CT.intake(ch, _event("/stop", sess)))
        check("★★ 什么都没跑 -> 不说「剩余消息已取消」（那是撒谎）",
              ch.notes == [CT._COPY["idle_stop"]], repr(ch.notes))

        # (b) 有轮次在跑、队列空
        ch.notes.clear()
        sch.busy = True
        L.run(CT.intake(ch, _event("/stop", sess)))
        check("★ 只有轮次在跑 -> 一句「已中断」",
              ch.notes == [CT._COPY["stopped_bare"]], repr(ch.notes))

        # (c) 有轮次在跑 + 队列里有货 + 收件箱里有货
        ch.notes.clear()
        gate = threading.Event()

        def _blocking():
            async def _run():
                await asyncio.get_running_loop().run_in_executor(None, gate.wait)
            return _run

        CT.submit(key, _blocking())
        _wait_until(lambda: CT.is_serving(key))
        time.sleep(0.05)
        CT.submit(key, _blocking())
        steer.push(key, {"id": "s1", "text": "补的"})
        L.run(CT.intake(ch, _event("/stop", sess)))
        check("★★ 丢掉排队的那条 -> 说「剩余消息已取消」",
              ch.notes == [CT._COPY["stopped"]], repr(ch.notes))
        check("★ 队列真的空了", CT.pending(key) == 0, str(CT.pending(key)))
        check("★ 收件箱也清了", steer.pending(key) == 0, str(steer.pending(key)))
        check("★ 中断标志置上了", cancellation.is_cancelled(key))
        gate.set()
    finally:
        cancellation.clear(key)
        steer.clear(key)
        CT.purge(key)
        L.close()


# 4. steer 的受理与四条拒绝

def test_intake_steer():
    print("\n-- 4. 补话：受理一条 / 四条不 steer 的判据 --")
    from core import steer

    L = _Loop()
    try:
        # 受理
        sess = _session("steer-ok")
        key = session_key_for(sess)
        sch = _Sch(busy=True)
        ch = _Chan(sch)
        steer.clear(key)
        L.run(CT.intake(ch, _event("顺便查一下天气", sess)))
        check("★★ 进收件箱", steer.pending(key) == 1, str(steer.pending(key)))
        check("★ **没有**进队列", CT.pending(key) == 0 and not sch.turns,
              "%s / %r" % (CT.pending(key), sch.turns))
        check("★ 回了一句", ch.notes == [CT._COPY["steer_ok"]], repr(ch.notes))
        steer.clear(key)

        # ① 命令
        sess = _session("steer-cmd")
        key = session_key_for(sess)
        sch = _Sch(busy=True)
        ch = _Chan(sch)
        L.run(CT.intake(ch, _event("/clear", sess)))
        check("① 命令不 steer（进队列 + 说排哪句）",
              steer.pending(key) == 0 and ch.notes == [CT._COPY["queued"]],
              repr(ch.notes))
        check("① 命令真的排上了", _wait_until(lambda: "/clear" in sch.turns),
              repr(sch.turns))

        # ② 附件
        sess = _session("steer-media")
        key = session_key_for(sess)
        sch = _Sch(busy=True)
        ch = _Chan(sch)
        L.run(CT.intake(ch, _event("看图", sess, media=True)))
        check("② 带附件不 steer", steer.pending(key) == 0
              and ch.notes == [CT._COPY["queued_media"]], repr(ch.notes))

        # ③ 有待审批（那句话是审批回答）
        sess = _session("steer-approval")
        key = session_key_for(sess)
        sch = _Sch(busy=True, pending={"kind": "command"})
        ch = _Chan(sch)
        L.run(CT.intake(ch, _event("同意", sess)))
        check("③ 有审批挂着时不 steer", steer.pending(key) == 0,
              str(steer.pending(key)))

        # ④ 目标不唯一（多条会话活跃、没有认领）
        sess = _session("steer-ambiguous")
        key = session_key_for(sess)
        now = time.monotonic()
        sch = _Sch(busy=True, snap={"workspace:group:x": now,
                                    "workspace:group:y": now})
        ch = _Chan(sch)
        L.run(CT.intake(ch, _event("补一句", sess)))
        check("④ 目标不唯一时不 steer（判不出来就别替用户挑）",
              steer.pending(key) == 0, str(steer.pending(key)))

        # ⑤ 没在跑：不许 steer（没有下一个边界）
        sess = _session("steer-idle")
        key = session_key_for(sess)
        sch = _Sch(busy=False)
        ch = _Chan(sch)
        L.run(CT.intake(ch, _event("普通一句", sess)))
        check("⑤ 没在跑 -> 走普通发送，且**不插嘴**（没活还 ack = 纯噪声）",
              steer.pending(key) == 0 and ch.notes == [], repr(ch.notes))
        check("⑤ 普通那句真的跑上了", _wait_until(lambda: "普通一句" in sch.turns),
              repr(sch.turns))
        for k in ("steer-cmd", "steer-media", "steer-approval",
                  "steer-ambiguous", "steer-idle"):
            steer.clear("workspace:group:ct-%s" % k)
    finally:
        L.close()


# 5. 轮末残留接管

def test_leftover_turn():
    print("\n-- 5. 轮末没赶上的补话：自动接着跑一轮 --")
    L = _Loop()
    try:
        sess = _session("left")
        key = session_key_for(sess)
        sch = _Sch()
        ch = _Chan(sch)
        replies: list = []

        def _on_event(event, n):
            if n == 1:
                event.extras["steer_leftover"] = [{"id": "l1", "text": "补一句"}]
            return "答复%d" % n

        sch.on_execute = _on_event
        L.run(CT.intake(ch, _event("第一句", sess),
                        on_reply=_collector(replies)))
        check("★★ 残留那条**自动**接着跑了一轮",
              _wait_until(lambda: len(sch.turns) >= 2) and
              sch.turns == ["第一句", "补一句"], repr(sch.turns))
        check("★ 两轮的回答都发出去了",
              _wait_until(lambda: len(replies) >= 2), repr(replies))

        # 被打断的那一轮：不许接管
        for flag in ("cancelled", "awaiting_approval"):
            sch2 = _Sch()
            ch2 = _Chan(sch2)

            def _once(event, n, _f=flag):
                event.extras[_f] = True
                event.extras["steer_leftover"] = [{"id": "x", "text": "别跑"}]
                return "答"

            sch2.on_execute = _once
            sess2 = _session("left-" + flag)
            L.run(CT.intake(ch2, _event("一句话", sess2),
                            on_reply=_collector([])))
            time.sleep(0.3)
            check("★★ `%s` 的那一轮不接管残留" % flag,
                  sch2.turns == ["一句话"], repr(sch2.turns))
    finally:
        L.close()


# 6. 文案表 / 翻译同步 / 状态登记

def test_error_turn_speaks():
    print("\n-- 6. 这一轮抛了：也要说一句人话（飞书 / 钉钉那条路）--")
    L = _Loop()
    try:
        sess = _session("err")
        sch = _Sch()
        ch = _Chan(sch)

        def _boom(event, n):
            raise RuntimeError("probe boom")

        sch.on_execute = _boom
        L.run(CT.intake(ch, _event("一句话", sess)))       # 没有 on_reply = 飞书/钉钉
        check("★★ 抛了也要回一句（旧形状里钉钉有、飞书没有）",
              _wait_until(lambda: ch.notes), repr(ch.notes))
        check("★ 台词只有一份（`_COPY['error']`）",
              ch.notes == [CT._COPY["error"]], repr(ch.notes))
    finally:
        L.close()


# 7. 通道接线（AST：除 CLI 外不许再自己 await execute）

def _execute_calls(src: str) -> int:
    import ast
    hits = 0
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "execute":
            try:
                target = ast.unparse(fn.value)
            except Exception:                                 # noqa: BLE001
                target = ""
            if "scheduler" in target:
                hits += 1
    return hits


def test_channels_do_not_await_execute():
    print("\n-- 7. 通道不再自己 `await scheduler.execute`（AST 读者）--")
    root = Path(__file__).resolve().parent.parent
    bad = []
    for p in sorted((root / "channels").rglob("*.py")):
        if p.parent.name == "cli_channel":
            continue          # CLI：`input()` 阻塞是它自己的语义（设计 §6 残余①）
        try:
            if _execute_calls(p.read_text(encoding="utf-8", errors="replace")):
                bad.append(str(p.relative_to(root)))
        except SyntaxError:
            continue
    check("★★ `channels/` 下除 cli 外不再出现 `scheduler.execute`"
          "（出现 = 那个通道又整轮不回头了）", not bad, repr(bad))
    #: 反证：同一条判据真能红（不然它只是个恒真的签名）。
    check("反证：自造靶子被同一条判据抓到",
          _execute_calls("async def f():\n    await self._scheduler.execute(x)\n") == 1)
    check("反证：注释里提到它不算（AST，不是 grep）",
          _execute_calls("# self._scheduler.execute(x)\nx = 1\n") == 0)


def test_stop_ack_does_not_duplicate_the_cancel_line():
    """IM 按的 `/stop`：框架那句「已打断，需要 … 怎么做？」不再发一遍。

    飞书反馈：一次 `/stop` 冒出两句话 —— 当场那句「已中断」
    （`intake` 回的）+ 这一轮自己的收尾（`agent_loop.CANCEL_REPLY`）。
只丢文字，文件照发；Web 那头按的打断不丢（那标记只由 IM 打）。
    """
    from core import approval_timeout as AT
    from core.agent_loop import CANCEL_REPLY

    L = _Loop()
    try:
        # ① 标记真的由 intake 打上（挂在那一轮的事件上，不新增状态）
        sess = _session("stop-mark")
        key = session_key_for(sess)
        sch = _Sch(busy=True)
        ch = _Chan(sch)
        ev = _event("干活", sess)
        AT.remember(key, ev)
        L.run(CT.intake(ch, _event("/stop", sess)))
        check("★ `/stop` 在那一轮的事件上打了标记", ev.extras.get("im_stop_ack") is True,
              repr(ev.extras))
        from core import cancellation
        cancellation.clear(key)

        # ② 带标记，那句话不发（`on_reply` 那条路：电报 / 微信）
        class _Cancel(_Sch):
            async def execute(self, event):
                event.extras["cancelled"] = True
                return CANCEL_REPLY

        ch2 = _Chan(_Cancel())
        replies: list = []
        ev2 = _event("干活", sess)
        ev2.extras["im_stop_ack"] = True
        L.run(CT.intake(ch2, ev2, on_reply=_collector(replies)))
        check("★★ 带标记 -> 那句取消收尾**不发**", _wait_until(lambda: replies),
              repr(replies))
        check("★★ 发出去的是空的（只有这一轮，没有第二句）",
              replies == [""], repr(replies))

        # ③ 不带标记（Web 那头按的 / 压根没有标记），照发
        ch3 = _Chan(_Cancel())
        replies3: list = []
        sess3 = _session("stop-plain")
        L.run(CT.intake(ch3, _event("干活", sess3), on_reply=_collector(replies3)))
        check("★ 没标记 -> 那句话照发（Web 按的打断，IM 用户得看得到）",
              _wait_until(lambda: replies3) and replies3 == [CANCEL_REPLY],
              repr(replies3))

        # ④ `reply_callback` 那条路（飞书 / 钉钉）：只把文字清空
        class _CancelCb(_Sch):
            """照真管线的形状：先写 `response`，再调 `reply_callback`。"""

            async def execute(self, event):
                event.extras["cancelled"] = True
                event.extras["response"] = CANCEL_REPLY
                if event.reply_callback:
                    event.reply_callback(event)
                return CANCEL_REPLY

        ch4 = _Chan(_CancelCb())
        sess4 = _session("stop-cb")
        ev4 = _event("干活", sess4)
        ev4.extras["im_stop_ack"] = True
        seen: list = []

        def _cb(e):
            seen.append(str((e.extras or {}).get("response") or ""))

        ev4.reply_callback = _cb
        L.run(CT.intake(ch4, ev4))
        check("★★ reply_callback 那条路：文字被清空（文件照发的路还在）",
              _wait_until(lambda: seen) and seen[-1] == "", repr(seen))
    finally:
        L.close()
        for tag in ("stop-mark", "stop-plain", "stop-cb"):
            try:
                from core import cancellation as _c
                _c.clear("workspace:group:ct-%s" % tag)
            except Exception:                             # noqa: BLE001
                pass


def test_outbox_draining_channels_override_send_note():
    """**凡是 `send()` 里取走待发文件的通道，必须覆盖 `send_note()`。**

    `send()` 会取走 `core/delivery_outbox` 里本轮待交付的文件，拿它发
    「已收到 / 已中断」会在一轮还在跑的时候把附件提前发出去（用户先看到
    文件、再看到回答）。今天四个通道的 `send()` 全都取件，所以四个都覆盖了。

    判据自适应：不写死通道名 —— 谁的 `send()` 里出现 `_drain`，谁就必须
      有 `send_note`（新通道接进来时这条自动生效，不需要想起它）。
    """
    import ast
    root = Path(__file__).resolve().parent.parent
    draining, missing = [], []
    for p in sorted((root / "channels").rglob("handler.py")):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            send = next((f for f in cls.body
                         if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and f.name == "send"), None)
            if send is None or "_drain" not in ast.unparse(send):
                continue
            draining.append(cls.name)
            if not any(isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and f.name == "send_note" for f in cls.body):
                missing.append("%s::%s" % (p.name, cls.name))
    check("★★ 取件的通道都覆盖了 `send_note`（旁白不许捎带文件）",
          not missing, repr(missing))
    check("★ 这条判据真的看过了通道（不是空转）", len(draining) >= 4,
          repr(draining))

    #: 钉钉的旁白必须走本条消息的 `sessionWebhook` —— 回答就是走它的，
    #:   旁白没理由换一条更脆的路（机器人 OpenAPI 要权限、群聊形式还不同）。
    dd = (root / "channels" / "dingtalk_channel" / "handler.py").read_text(
        encoding="utf-8")
    check("★★ 钉钉的旁白优先用 sessionWebhook",
          "meta.get(\"session_webhook\")" in dd, "")


def test_copy_and_registration():
    print("\n-- 6. 文案：三句已拍的原文 + 同步翻译 + 状态登记 --")
    #: 这三句的措辞一个字都不许自己改。
    check("★ `/stop` 那句", CT._COPY["stopped"] == "已中断，剩余消息已取消",
          CT._COPY["stopped"])
    check("★ 补话那句", CT._COPY["steer_ok"] == "正在处理，已收到消息",
          CT._COPY["steer_ok"])
    check("★ 排队那句", CT._COPY["queued"] == "命令信息，等待本轮任务结束后处理",
          CT._COPY["queued"])
    #: 这句原来是「现在没有在跑的任务」。
    check("★ 空跑时那句（用户改稿）", CT._COPY["idle_stop"] == "无任务",
          CT._COPY["idle_stop"])

    root = Path(__file__).resolve().parent.parent
    en = root / "locales" / "en.json"
    missing = []
    if en.exists():
        pack = json.loads(en.read_text(encoding="utf-8"))
        missing = [v for v in CT._COPY.values() if v not in pack]
    check("★★ `_COPY` 每一句都有英文译文（用户要求「同步翻译」）",
          en.exists() and not missing, repr(missing))

    scan = (root / "scripts" / "i18n_scan.py").read_text(
        encoding="utf-8", errors="replace")
    check("★ 扫描器登记了本文件（不登记 -> 新文案永远进不了表）",
          '"core/channel_turns.py"' in scan)

    from core.state_registry import SESSION_SCOPED
    check("★ 会话级队列已在 `state_registry` 登记（`/clear` 要清它）",
          any(s.name == "channel_turns" for s in SESSION_SCOPED),
          repr([s.name for s in SESSION_SCOPED]))


def _noop_job():
    async def _run() -> None:
        return None
    return _run


def test_clear_keeps_worker_and_caps_queue():
    """`/clear` 只丢队列；队列与 worker 条数都有上限。"""
    print("\n-- 8. `/clear` 不动 worker / 队列上限 --")
    sess = _session("clear-keep")
    key = session_key_for(sess)
    gate = threading.Event()

    def _blocking():
        async def _run() -> None:
            await asyncio.get_running_loop().run_in_executor(None, gate.wait)
        return _run

    CT.submit(key, _blocking())
    check("★ worker 起来了", _wait_until(lambda: CT.is_serving(key)))
    worker = CT._workers.get(key)
    time.sleep(0.05)
    for _ in range(20):
        CT.purge(key)
    check("★★ 反复 `/clear` 之后同一条 worker 还在（换了线程 = 旧的漏了）",
          CT.is_serving(key) and CT._workers.get(key) is worker,
          "%s / %s" % (CT.is_serving(key), CT._workers.get(key) is worker))
    check("★ 收掉的只有队列", CT.pending(key) == 0, str(CT.pending(key)))

    accepted = sum(1 for _ in range(CT.MAX_QUEUE) if CT.submit(key, _noop_job()))
    check("★ 队列能排到上限（%d 条）" % CT.MAX_QUEUE, accepted == CT.MAX_QUEUE,
          str(accepted))
    check("★★ 排满之后 `submit` 返回 False（不是静默丢一条）",
          CT.submit(key, _noop_job()) is False)

    # 收消息那条路也要说出来：带附件的消息不 steering，正好走排队那一支
    L = _Loop()
    try:
        ch = _Chan(_Sch(busy=False))
        L.run(CT.intake(ch, _event("看图", sess, media=True)))
        check("★★ 队列满了 -> 回一句实话（否则与「发了没反应」同形）",
              ch.notes == [CT._COPY["queue_full"]], repr(ch.notes))
    finally:
        L.close()
    gate.set()
    _wait_until(lambda: CT.pending(key) == 0, 10)
    CT.stop(key)


def test_worker_count_is_capped():
    """worker 条数有上限，满了先收最闲的；有审批定时器的不收。"""
    print("\n-- 9. worker 条数上限 --")
    from core import approval_timeout as AT

    old_max, old_idle = CT.MAX_WORKERS, CT.IDLE_EVICT_S
    CT.MAX_WORKERS, CT.IDLE_EVICT_S = 2, -1.0
    pinned = "workspace:group:cap-pinned"
    try:
        # 起点清空：前面几条用例刻意不再摘 worker（`/clear` 不清了），
        #   这里要量的是"上限"，不是"历史残留"。
        for k in list(CT._workers):
            CT.stop(k)
        _wait_until(lambda: not CT._workers)
        check("★ 起点是空的", not CT._workers, repr(sorted(CT._workers)))

        AT.remember(pinned, object())
        CT.submit(pinned, _noop_job())
        _wait_until(lambda: CT.is_serving(pinned))
        for i in range(5):
            CT.submit("workspace:group:cap-%d" % i, _noop_job())
        # 上限是收敛值：正在跑的那几条不会被硬等。等它们跑完，
        #   下一次新建时再收一遍 —— 条数必须落回上限内。
        time.sleep(0.3)
        CT.submit("workspace:group:cap-settle", _noop_job())
        _wait_until(lambda: len(CT._workers) <= 2)
        check("★★ 建了 7 条会话，worker 条数收敛到上限", len(CT._workers) <= 2,
              str(len(CT._workers)))
        check("★★ 挂着审批定时器的那条**不被收**（收了 = 超时静默失效）",
              CT.is_serving(pinned), repr(sorted(CT._workers)))
        gone = [k for k in ("workspace:group:cap-%d" % i for i in range(5))
                if not CT.is_serving(k)]
        check("★ 被收的那些真的不在表里了", len(gone) >= 3,
              "%d 条被收 / 表里 %r" % (len(gone), sorted(CT._workers)))
    finally:
        for k in list(CT._workers):
            if k.startswith("workspace:group:cap-"):
                CT.stop(k)
        AT.forget(pinned)
        CT.MAX_WORKERS, CT.IDLE_EVICT_S = old_max, old_idle


def main() -> int:
    print("=" * 62)
    print("channel turns: queue / intake / leftover")
    print("=" * 62)
    for fn in (test_queue_fifo, test_loop_stays_alive, test_intake_stop,
               test_intake_steer, test_leftover_turn, test_error_turn_speaks,
               test_channels_do_not_await_execute,
               test_outbox_draining_channels_override_send_note,
               test_stop_ack_does_not_duplicate_the_cancel_line,
               test_copy_and_registration,
               test_clear_keeps_worker_and_caps_queue,
               test_worker_count_is_capped):
        try:
            fn()
        except Exception as exc:                              # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " 没抛异常", False,
                  "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
