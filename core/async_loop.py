"""常驻事件循环 —— 给「活得比一次调用长」的异步资源一个家。

## 为什么要有它

外部 MCP 的通路要求同一个循环一直活着：如果每次调用都 `asyncio.run(coro)`，
MCP 的 `ClientSession` 绑定在"创建它的那个循环"上
（`anyio` 的 task group 也是）。循环一退，session 就废。

需要一个进程内唯一、且活着的循环：连接在它上面建立，调用也在它上面发出。

## 为什么是新造一个线程，而不是复用飞书那个

`channels/feishu_channel/handler.py` 那个"专用线程"跑的是
`lark.ws.Client.start()` —— 第三方阻塞式 API，里面自带循环；而且它自己反过来用
`asyncio.run_coroutine_threadsafe(…, self._main_loop)` 往主循环提交。
它对我们是生产者，不是宿主 —— 方向正好相反。
它那句注释（*"dedicated thread to avoid event-loop conflicts"*）说的也是
「躲开循环」，不是「提供循环」。别去复用它。

## 用法

    from core.async_loop import submit

    server = submit(connect_stdio_server(...))        # 从任意线程
    result  = submit(session.call_tool(name, args))   # 还是它

`submit` 从任意线程都安全 —— 包括"已经有自己循环的线程"
（`web/server.py` 给每个通道起了线程+循环）。它走 `run_coroutine_threadsafe`，
把协程投到常驻循环上，然后在本线程等结果。

唯一的禁区：不许从常驻循环自己的线程里调 `submit` ——
`.result()` 会把那个循环堵死，于是它自己等自己。
那种场合要 `await`，用 `submit_async`。
   —— 这条不是洁癖：另开线程池再 `asyncio.run` 的写法会养出第三个循环，
   而 session 一个都不在它上面。

它是进程级的，不是会话级，不进会话级状态登记表
（那张表管的是"按 `session_id` 键、活过一次请求"的状态，`/clear` 要清它；
这个循环 `/clear` 不能碰 —— 碰了就把 MCP 连接一起掐了）。
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack
from typing import Any, Callable, Coroutine, TypeVar

_T = TypeVar("_T")

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
#: `get_loop()` 会被多个线程同时调（启动期 + 任意工具调用），起循环这一段
#: 必须是原子的，否则会起来两个循环，而 session 只在其中一个上面。
_lock = threading.Lock()

#: 启动握手的等待上限（秒）。够宽 —— 它只是防"线程起不来"这种死等。
_READY_TIMEOUT = 10.0

_THREAD_NAME = "crannia-async-loop"


def _run_forever(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    """常驻线程的入口：装上循环，转起来，直到 `shutdown()`。"""
    asyncio.set_event_loop(loop)
    # 用 `call_soon` 来放行，而不是"线程一起来就 `ready.set()`" ——
    #   后者会在 `run_forever` 真正开始处理之前就放行，调用方于是可能在
    #   循环还没转起来时就把协程投进来。CPython 上那样通常也能跑，但那是
    #   在赌内部实现（`call_soon_threadsafe` 的写入端点还没接好）。
    loop.call_soon(ready.set)
    loop.run_forever()


def get_loop() -> asyncio.AbstractEventLoop:
    """取常驻循环；还没有就起一个。幂等：并发调用也只会起一个。"""
    global _loop, _thread
    with _lock:
        if (_loop is not None and not _loop.is_closed()
                and _thread is not None and _thread.is_alive()):
            return _loop

        loop = asyncio.new_event_loop()
        ready = threading.Event()
        th = threading.Thread(
            target=_run_forever, args=(loop, ready),
            daemon=True, name=_THREAD_NAME,
        )
        th.start()
        if not ready.wait(_READY_TIMEOUT):
            # 起不来就把它收干净，别留一个半死的循环在全局变量里
            # （下一个调用方会以为它可用 —— "读不到 ≠ 0"）。
            loop.close()
            raise RuntimeError(
                f"常驻事件循环没能在 {_READY_TIMEOUT:.0f}s 内启动（线程 {_THREAD_NAME}）")

        _loop, _thread = loop, th
        return loop


def current_loop() -> asyncio.AbstractEventLoop | None:
    """已有的常驻循环，没有就 `None`。只读，不启动。

    与 `get_loop()` 的区别是副作用：`get_loop()` 会起线程。
判定式的调用方（"还连着吗"）一律用这个 ——
    一个谓词不该有"顺手把系统启动起来"的副作用
    （`ExternalMCPServer.connected` 就是那个调用方）。
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return None
    return loop


def on_loop_thread() -> bool:
    """当前线程是不是常驻循环那个。"""
    return _thread is not None and threading.current_thread() is _thread


def submit(coro: Coroutine[Any, Any, _T], timeout: float | None = None) -> _T:
    """把 *coro* 投到常驻循环上跑，在本线程等结果。

    Args:
        coro: 要跑的协程。
        timeout: 等待上限（秒）。`None` = 一直等。

    Raises:
        RuntimeError: 从常驻循环自己的线程里调用（会自己等自己）。
        TimeoutError: 超时。
        Exception: `coro` 自己抛的 —— 原样透传，不吞（错误路径只许有一条，
            包装一层会让调用方判不出到底哪儿坏了）。
    """
    if on_loop_thread():
        # 把没跑的协程收掉，免得留一条 "coroutine was never awaited" 噪声
        # —— 那条警告会盖住真正要说的那句话。
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        raise RuntimeError(
            "submit() 不能从常驻循环自己的线程里调（它会自己等自己）；"
            "已经在那个循环上就改用 `await` —— 见 core.async_loop.submit_async")

    loop = get_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result(timeout)


async def submit_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """在常驻循环上跑 *coro* 并 `await` —— 给"已经在那个循环上"的调用方。

    从别的线程 `await` 它也可以（`run_coroutine_threadsafe` 给的 Future
    本来就是跨线程的），但那种场合 `submit()` 更直白、也不多一层包装。
    留这个口子是为了让"我就在循环上"那条路有地方去 ——
    否则下一个遇到它的人会去装 `nest_asyncio` 之类，把问题盖住而不是解决。
    """
    running = asyncio.get_running_loop()
    target = get_loop()
    if running is target:
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, target))


class OwnedResource:
    """一个专属任务持有的异步资源栈。`open_owned()` 的返回值。

    `value` 是给调用方的那个东西（MCP 那边就是 `ExternalMCPServer`）；
    它从任意任务都能用，只有"收"必须回到专属任务（见 `open_owned`）。
    """

    __slots__ = ("value", "_stop", "_done")

    def __init__(self, value: Any, stop: asyncio.Event, done: asyncio.Future) -> None:
        self.value = value
        self._stop = stop
        self._done = done

    @property
    def closed(self) -> bool:
        """专属任务是不是已经收完栈了。真会回落的判据。"""
        return self._done.done()

    async def aclose(self) -> None:
        """请求收栈，并等它收完。幂等。"""
        if self._done.done():
            return
        self._stop.set()
        await self._done


async def open_owned(enter: Callable[[AsyncExitStack], Any]) -> OwnedResource:
    """在常驻循环上开一个专属任务，让它从头到尾持有整个 async 资源栈。

    *enter* 是 `async def (stack) -> value`：把各个上下文用
    `stack.enter_async_context(...)` 装进 *stack*，返回交给调用方的值。

    ## 为什么需要这一层（不是设计洁癖）

    如果写成 `submit(connect_stdio_server(...))` + 之后 `submit(server.aclose())`：
    工具调用会成功（连接是活的），而 `aclose()` 抛 ——

        RuntimeError: Attempted to exit cancel scope in a different task
                      than it was entered in

    根因：`submit` 每次都 `run_coroutine_threadsafe`，每次新建一个 Task；
    而 MCP SDK 的 `stdio_client` 内部是 `async with anyio.create_task_group()` ——
    **`__aenter__` 记下当前 task，`__aexit__` 断言是同一个**。

"能用"与"能收"是两件事。只看"工具调得通"的话，这条会一直绿到
    进程退出时留下一堆孤儿 MCP 子进程 —— 而那时没人看得见是谁留的。
    （没报错 ≠ 做对了。）
    """
    stack = AsyncExitStack()
    loop = asyncio.get_running_loop()
    ready: asyncio.Future = loop.create_future()
    done: asyncio.Future = loop.create_future()
    stop = asyncio.Event()

    async def _own() -> None:
        try:
            try:
                value = await enter(stack)
            except BaseException as exc:  # noqa: BLE001
                if not ready.done():
                    ready.set_exception(exc)
                await stack.aclose()
                return
            if not ready.done():
                ready.set_result(OwnedResource(value, stop, done))
            await stop.wait()
            await stack.aclose()
        finally:
            if not done.done():
                done.set_result(None)

    asyncio.ensure_future(_own())
    try:
        return await ready
    except BaseException:
        # `enter` 半路失败：把专属任务等结束（它自己会收栈），再原样抛。
        stop.set()
        await done
        raise


def shutdown() -> None:
    """停掉常驻循环。幂等；测试收尾用，生产不走它（线程是 daemon，随进程退）。

    它会掐断挂在它上面的所有资源（MCP 连接就在上面）。
    生产路径上没有任何调用点是有意的 —— 别在 `/clear` 或 `/api/restart` 里调它。
    """
    global _loop, _thread
    with _lock:
        loop, th = _loop, _thread
        _loop, _thread = None, None
    if loop is None:
        return
    if not loop.is_closed():
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass  # 已经停了/关了
    if th is not None and th.is_alive():
        th.join(timeout=5.0)
    if not loop.is_closed():
        loop.close()
