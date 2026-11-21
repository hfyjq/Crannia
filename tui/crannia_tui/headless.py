"""`--once` 那条路：发一句、打印、退出。

它不走 Textual —— 这个模式的输出对象是脚本和日志（经常被重定向到文件），
为一个"打完就走"的场景起一套全屏应用，既慢又会往输出里掺光标控制序列。

渲染仍然借用 `render.py`：那里定义了"一条事件该长什么样"，
两套界面共用同一份判断，才不会出现"全屏里好看、管道里乱码"的分叉。

退出码约定：
  0 —— 正常完成；
  1 —— 后端没起来或请求失败；
  3 —— 本轮触发审批。`--once` 没有人能回答，立即中断后端回合并请用户改用交互界面。
"""

from __future__ import annotations

import json
import threading
from typing import Callable

from rich.console import Console
from rich.text import Text

from . import events as E
from .backend import Backend, BackendError
from .client import EventStream
from .i18n import t
from .render import ScrollbackRenderer

#: 一轮的上限。真跑起来它取决于模型和工具链，这里只是"别无限等"。
TURN_TIMEOUT = 600
#: 等 HTTP 回复时盯审批的间隔。50ms 足够"立刻"，又不会空转吃 CPU。
_APPROVAL_POLL = 0.05
#: HTTP 回来后留给事件流送尾巴的时间；期间审批来了也照样收尾。
_DRAIN_SECONDS = 1.5


def run_once(backend: Backend, text: str,
             stream_factory: Callable[..., EventStream] | None = None) -> int:
    """发一条消息，打印定稿内容后退出。

    *stream_factory* 只为测试留注入点：签名与 `EventStream` 的构造一致，
    默认仍是真流。生产路径一个字节不变。
    """
    try:
        backend.start()
        backend.wait_ready()
        backend.start_framework()
        #: 会话键只从 `/api/status` 取（与全屏 TUI 同一个来源）。不拿它，
        #: 事件流会订阅**全部会话**：别的通道的一轮会混进输出，别处的
        #: `approval.request` 也会被当成"这一条要审批"而中断当前任务。
        status = backend.get_json("/api/status")
    except BackendError as exc:
        Console().print(Text(t("后端错误：") + f"\n{exc}", style="red"))
        backend.stop()
        return 1
    except Exception as exc:                                 # noqa: BLE001
        Console().print(Text(t("后端错误：") + f"\n{type(exc).__name__}: {exc}",
                             style="red"))
        backend.stop()
        return 1

    session = str((status or {}).get("session") or "").strip()
    if not session:
        #: 宁可拒绝，也不退回"订阅全部" —— 那正是这条要修的病。
        Console().print(Text(t("无法确定会话（/api/status 没有 session），"
                               "不再订阅全部会话；请改用交互界面。"), style="red"))
        backend.stop()
        return 1

    console = Console()
    renderer = ScrollbackRenderer(console)
    lock = threading.Lock()
    approval = threading.Event()

    def on_event(ev: E.Event) -> None:
        with lock:
            renderer.feed(ev)
        if ev.type == E.APPROVAL_REQUEST:
            # 回调跑在事件流的读线程里，不能在那里等 HTTP。先做标记，
            # 让主循环中断那个还阻塞着的 /api/chat。
            approval.set()

    factory = stream_factory or EventStream
    stream = factory(backend.base, backend.token, on_event, lambda _s: None,
                     session=session)
    stream.start()

    def finish_with_approval() -> int:
        """与交互界面 Esc 走同一条中断路；载荷 {}，不留挂着的回合。"""
        try:
            backend.post_json("/api/stop", {})
        except Exception as exc:                             # noqa: BLE001
            # 中断失败也不能把这一轮拖回 600 秒：提示后照旧按审批退出。
            console.print(Text(
                t("中断请求失败：%s: %s") % (type(exc).__name__, exc),
                style="yellow"))
        console.print(Text(
            t("需要审批；--once 模式下无法响应，请改用交互界面。"),
            style="yellow"))
        return 3

    answered: dict = {}
    done = threading.Event()

    def do_chat() -> None:
        # HTTP 会一直阻塞到这一轮结束。放到工作线程里，主线程才有机会
        # 在审批到达的那一刻就收尾，而不是等 TURN_TIMEOUT。
        try:
            answered["reply"] = backend.post_json(
                "/api/chat", {"text": text}, timeout=TURN_TIMEOUT)
        except Exception as exc:                             # noqa: BLE001
            answered["error"] = exc
        finally:
            done.set()

    threading.Thread(target=do_chat, daemon=True,
                     name="crannia-once-chat").start()

    try:
        while not done.is_set():
            if approval.wait(_APPROVAL_POLL):
                return finish_with_approval()
        # 同一瞬间两条路都到了：审批优先，别把"等人拍板"误报成完成。
        if approval.is_set():
            return finish_with_approval()
        if approval.wait(_DRAIN_SECONDS):
            return finish_with_approval()
        if "error" in answered:
            exc = answered["error"]
            console.print(Text(
                "✗ " + t("请求失败：%s") % f"{type(exc).__name__}: {exc}",
                style="red"))
            return 1
        code, body = answered["reply"]
        if code != 200:
            console.print(Text(
                "✗ " + t("后端返回 %s：%s") % (code, body[:200]), style="red"))
            return 1
        try:
            reply = (json.loads(body) or {}).get("reply")
        except json.JSONDecodeError:
            reply = None
        # 事件流和 HTTP 是两条并发的路，谁先到不固定。兜底在 renderer
        # 里做过重复判断，所以这段等待不会造成重复输出。
        if isinstance(reply, str) and reply.strip():
            with lock:
                renderer.said(reply)
        return 0
    finally:
        stream.stop()
        backend.stop()
