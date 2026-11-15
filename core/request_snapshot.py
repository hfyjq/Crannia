"""请求快照 —— fork 的唯一入口。

## 它是什么

「刚才实际发出去的那四个」：`payload`（消息表）· `tools`（schema 表）·
`system` · `model`。它等价于缓存安全参数，判据是：

> fork 与父请求之间必须逐字节一致，才能共享父的提示缓存。
> 缓存键由五样组成：system prompt、tools、model、messages 前缀、thinking 配置。

子 agent 第一条请求要和父刚发出去的那条逐字节同前缀，才能吃到父的缓存
（缓存命中率是第一指标）。所以抓的必须是「发出去的那四个」，
不是「现在重算一遍的那四个」。

## 三处会分叉（每一条都验证过）

1. `payload ≠ messages` —— 每轮第一次请求会把技能索引附在**最后一条 user
   消息的副本**上（`core/skill_index.py::append_skill_index`）。抓 `messages`，丢掉索引；
   子 agent 若自己重算，索引会落到另一条 user 消息上（子的最后一条 user 是 directive）
恰好在那个字节处断。
2. `tools` 每轮重算（`get_tool_schemas_active(session_id)`）且随发现变。
3. `system` 每轮重算（`assemble_system_prompt(...)`）。

## 快照不是引用

`messages` 是活表：父在派发之后还会往里 append，压缩那一步还会 `del messages[i:]`。
判据：fork 前缀只在创建时捕获一次，
之后不再按父的新历史重新 fork。

本模块 deepcopy。不靠"没人原地改 messages"这条约定 —— 今天确实没人这么干
（对 `messages` 的写入全是 append 新 dict），
但审计结论不是保证：`assistant_msg["tool_calls"][j]["function"]` 与 provider 响应
是同一个对象。deepcopy 的代价可忽略（字符串是不可变原子，deepcopy 不复制它们，
只重建 dict/list）。

## 为什么是 ContextVar

工具 handler 的签名是 `handler(**tool_args)` —— 拿不到 ctx、也拿不到 session_id，
所以"刚才那条请求"只能走环境。模块级单槽在并发下会静默串味
（每会话一把锁之后，两个会话会真的并行），单槽换成 `ContextVar`：

  · web：每个请求一个线程一个 loop，`asyncio.run` 的 MainTask 自带上下文；
  · 飞书/钉钉：`run_coroutine_threadsafe` 提交到共享主循环，各自成 Task、
    各自一份上下文（这是"同线程多协程"那一档，线程局部量治不了它）；
  · 新的一条轮次 = 新任务 = 新上下文，上一轮的快照不会带过来（自洁）。

一个例外要手动传播：只读并发批用 `ThreadPoolExecutor` 跑 handler，
而线程池不继承 contextvars，那里在提交时 `contextvars.copy_context().run` 包一层
（见 `core/agent_loop.py::_prefetch_readonly`）。新开线程跑 handler 的地方都要这么做，
否则那里的 `current_session_id()` 拿到空串（fail-closed，但功能会静静地少一块）。
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class RequestSnapshot:
    """一条已经发出去的请求。字段顺序即 fork 需要的东西。

    里面的 list/dict 不许原地改 —— 它是"那份历史"的唯一副本。要拼子 agent 的
    前缀请新建列表（`snapshot.payload + [...]`），不要在它上面 `append`。
    """

    payload: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    system: str = ""
    model: str = ""
    #: 发起它的会话 —— 子 agent 必须沿用（门禁按 session 查暴露集）
    session_id: str = ""
    #: 这一轮是从哪个渠道来的，形状 = `platform:type:session_id`
    #: （`core/im_routing.py::channel_key_of` 那个口径）。
    #: 它不能由 `session_id` 推出来 —— `core/session_key.py::session_key_for`
    #: 故意忽略 `platform`（私聊一律 `workspace:private`），拿会话键去投递
    #: 等于不知道发到哪个平台。周期任务建任务时要把它记下来。
    channel: str = ""

    def to_dict(self) -> dict:
        """诊断 / 断言用的浅表示（不含完整 payload —— 它可能几十 KB）。"""
        return {
            "messages": len(self.payload),
            "tools": len(self.tools),
            "system_chars": len(self.system or ""),
            "model": self.model,
            "session_id": self.session_id,
            "channel": self.channel,
        }


_current: contextvars.ContextVar[Optional[RequestSnapshot]] = contextvars.ContextVar(
    "crannia_request_snapshot", default=None)


def capture(payload, tools, system, model="", session_id="", channel="") -> RequestSnapshot:
    """把刚要发出去的四个冻成一份快照。纯函数（不碰单槽）。

    调用点必须紧挨真发送（`provider.chat(...)` / `provider.chat_stream(...)`），
    两条路各一处 —— 同步与流式必须锁步。
    """
    return RequestSnapshot(
        payload=copy.deepcopy(list(payload)),
        tools=copy.deepcopy(list(tools) if tools else []),
        system=str(system or ""),
        model=str(model or ""),
        session_id=str(session_id or ""),
        channel=str(channel or ""),
    )


def set_current(snap: RequestSnapshot) -> None:
    """把快照放进本任务的上下文（每个任务一份，互不覆盖）。"""
    _current.set(snap)


def get_current() -> Optional[RequestSnapshot]:
    """最近一条已发出的请求；从没发过就是 `None`。

    `None` 是有意义的：它意味着"现在没有一条请求在飞"，
    而"拿不到快照"与"拿到一条空的"是两件事（`None != 0`）。
    """
    return _current.get()


def clear_current() -> None:
    """清掉本任务上下文里的快照（`/clear` 时调，卫生用途）。

    它不是跨轮的必要清理：每一条轮次 = 新任务 = 新上下文，
    上一轮的快照随任务一起消亡。留着的理由只有一个 —— `/clear` 那一轮自己
    别再拿着旧快照（"清完 `get_current() is None` 是句真话"）。
    """
    _current.set(None)


@contextlib.contextmanager
def preserved():
    """跑一段会自己 `set_current` 的嵌套循环，退出时把快照还原。

    它挡的是什么：子 agent 走的是
    同一个 `agent_loop`（`core/subagent.py::run_child`），而那条路每轮都会
    `set_current(capture(...))`——覆盖父的快照、且从不还原。
    父只在每轮发请求前重设一次，
    同一批里的第二个 `run_subagent` 在 `_dispatch_subagent` 读到的，
    是第一个子跑完时留下的那一份。

    而 `_dispatch_subagent` 那道串味绊线（`snap.session_id != session_id`）
    拦不住它 —— 子沿用父的 `session_id`（fork 的同前缀硬要求）。
症状是"拿别人的转录去 fork"：静默，而且看起来像"子 agent 记错了"。

    为什么是 `Token` 而不是"存一个变量再 set 回去"：`set(...)` 返回的
    `Token` 记得进入这一刻的值，`reset(token)` 精确还原 —— 中间嵌套多少层、
    `set` 了多少次都不影响。下面那句 `set(_current.get())`
    看着像空操作，其实不是：它唯一的产出就是那个 `Token`。
    """
    token = _current.set(_current.get())
    try:
        yield
    finally:
        _current.reset(token)


def current_session_id() -> str:
    """正在跑的这个工具调用属于哪个会话。取不到就是空串。

    这个名字里的 `session_id` 是「通道键」，
    就是 `core/session_key.py::session_key_for` 推出来的那一个
    （私聊 `workspace:private` / 群聊 `workspace:group:<id>`）。
    它不是"消息行挂在哪个 id 上" —— 那是存储键
    （`StageContext.storage_id` / `short_term.session_bindings`）。
    拆键之前两者恒等，所以这个区别看不出来；现在它看得出来了：

      · 要通道键（绝大多数）：待办 · 读态 · 触碰过的文件 · 计划 slug ·
        ICE 句柄 · `run_command` 的取消/进程 · 工作区投影 —— 全是
        "这一段对话正在发生的事"（`/clear` 就该清）。
      · 要存储键的只有一个：`core/history_tools.py::_sid()` ——
        它读的是消息行。它不走这里，自己过一道
        `short_term.bound_storage_id()`（纯查询，不铸新）。

    工具 handler 的签名是 `handler(**tool_args)` —— 拿不到 `ctx`、也拿不到
    `session_id`，所以会话号只能从这台机器（单槽）上取。这是**同一句话的第三份
    拷贝**（`core/todos.py::_session_id`、`core/shell.py` 那条），所以抽到这里：
    三份各写各的就是"两处定义必然漂移"。

    拿不到快照不拒 —— 空会话号落进 `""` 那一格，行为与"没有会话"一致。
    这与 `core/shell.py` 那条是同款取舍：取不到会话号不该让工具变成不可用；
    而"取不到"在生产路径上不会发生（派发前一定捕获过快照，见模块 docstring）。
    空串在这里只是"没有通道"，而唯一要存储键的那一处走的是另一条路
    （上面那条），不受影响。

    子 agent 沿用父的 `session_id`，子里的写会记在父那一格 ——
    对的：子与父是同一段对话，而这里记的本来就是"这段对话进行到哪了"。
    """
    snap = _current.get()
    return snap.session_id if snap is not None else ""


def current_channel() -> str:
    """这一轮从哪个渠道来的：`platform:type:session_id`。取不到就是空串。

    建周期任务时要把"投给谁"记下来。
    它推不出来 —— `current_session_id()` 给的是会话键，
    而 `session_key_for()` 故意忽略 `platform`（私聊一律 `workspace:private`）
拿它去投递等于不知道发到哪个平台。
    （实物证据：过去 freenote 的推送正是因为这个，只能硬绑微信那条通道；
    那个 skill 已经删了，但这条判据不变。）

    取不到（老夹具 / 直接调 `agent_loop` 的单测），空串，调用方要拒
    （建一条投不出去的任务，比不建更坏）。
    """
    snap = _current.get()
    return snap.channel if snap is not None else ""
