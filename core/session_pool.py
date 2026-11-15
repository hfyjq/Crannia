"""
SessionPool — manages per-user session state on top of a shared MemoryManager.

MemoryManager already stores per-session state internally (keyed by
`session_id`), so we don't need multiple MemoryManager instances.
The pool simply:

1. Maps `str(MessageSession)` → internal `session_id` for
   load/restore.
2. Provides `get_or_create()` to retrieve (session_id, messages)
   for a given session.
3. Wraps `MemoryManager` methods so pipeline stages don't need to
   manage session IDs manually.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Dict, List, Tuple

#: 内存里最多常驻多少条会话的完整消息表。
#:
#: 每条是一个整段对话（工具结果都算在内），而它原来无上限：进程跑得越久、
#: 走过的会话越多，常驻的表只增不减。淘汰是安全的 —— 每一轮结束都会落库
#: （§5「先写盘再返回用户」），下一次取用按绑定表从库里重载，内容一致。
MAX_LOADED_SESSIONS = 32


class SessionPool:
    """Per-user session facade over a single MemoryManager.

    Each :class:`~channels.base.MessageSession` stringifies to a
    globally-unique key (e.g. `"cli:private:local"`).  This key
    is used as the `session_id` for MemoryManager operations,
    so every user/group gets independent short-term history,
    compression state, and session notes.
    """

    def __init__(self, memory_manager) -> None:
        """
        Args:
            memory_manager: A fully-initialised
                :class:`~core.memory.manager.MemoryManager` instance.
        """
        self._mgr = memory_manager
        #: 外键 → (存储 id, messages)。按最近使用排序，条数有上限
        #: （活动会话的完整消息表常驻，是内存随使用增长的主因）。
        self._loaded: "OrderedDict[str, Tuple[str, List[dict]]]" = OrderedDict()
        #: 缓存那把锁。Web 侧是多线程的，两个会话可以同时进出：
        #: 一个线程 `move_to_end` 时另一个正好把那个键淘汰掉，`KeyError`
        #: 就会从一次普通的取会话里冒出来。读也加锁 —— `OrderedDict`
        #: 在扩容期间的并发读同样不安全。
        #: 用 `RLock`：`_remember` 会在已经持锁的路径里被调用。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def get_or_create(
        self, session_key: str, provider=None
    ) -> Tuple[str, List[dict]]:
        """Return (session_id, messages) for *session_key*.

        On first access the session is loaded from persistent storage
        (or created fresh).  Subsequent calls return the cached state.
        """
        with self._lock:
            got = self._loaded.get(session_key)
            if got is not None:
                self._loaded.move_to_end(session_key)      # 命中即刷新热度
                return got

        # 锁外加载（库 I/O 不占着缓存锁）—— 然后二次检查：
        # 同一个键可能已经被另一个线程加载完放进去了，那就用他那份，
        # 免得两份 messages 各自演化（那才是真正的坏结果）。
        sid, messages = self._load_thread(session_key)
        with self._lock:
            got = self._loaded.get(session_key)
            if got is not None:
                self._loaded.move_to_end(session_key)
                return got
            self._remember_locked(session_key, (sid, messages))
        return sid, messages

    def _remember(self, session_key: str,
                  value: Tuple[str, List[dict]]) -> None:
        """写进缓存并维持上限（持锁版本，见 `_remember_locked`）。"""
        with self._lock:
            self._remember_locked(session_key, value)

    def _remember_locked(self, session_key: str,
                         value: Tuple[str, List[dict]]) -> None:
        """写进缓存并维持上限。调用方必须已经持有 `self._lock`。

        淘汰最久没用的那条：它下一次取用会按绑定表从库里重载。**这条保证
        的前提是"每轮结束都已落库"** —— 不成立的话淘汰就会丢话。
        """
        self._loaded[session_key] = value
        self._loaded.move_to_end(session_key)
        while len(self._loaded) > MAX_LOADED_SESSIONS:
            self._loaded.popitem(last=False)

    def _load_thread(self, session_key: str) -> Tuple[str, List[dict]]:
        """`通道键 → (存储 id, messages)` —— 那座桥。

        中间多了一步：`MemoryManager.storage_id_for(key)` 先在
        `short_term.db` 的 `session_bindings` 里查这个通道键现在绑在哪条线程上
        （没绑过就铸一条新的并绑上）。
        `/clear` 换的就是那张表里的一行，旧线程的行原样留着。
        `_loaded` 只是它的内存副本（进程一停就没了）——
        持久的那一份只有那次绑定，别在这里再发明第二张映射。
        """
        return self._mgr.load_session(self._mgr.storage_id_for(session_key))

    # ------------------------------------------------------------------
    # Convenience wrappers that delegate to MemoryManager
    # ------------------------------------------------------------------

    def get_context(
        self, session_key: str, user_query: str, messages=None
    ) -> str:
        """Thin wrapper around `MemoryManager.get_context()`."""
        sid = self._resolve_sid(session_key)
        return self._mgr.get_context(sid, user_query, messages=messages)

    def record_turn(
        self,
        session_key: str,
        user_message: dict,
        assistant_message: dict,
        token_count: int,
        messages: List[dict],
        rows: dict | None = None,
    ) -> List[dict] | None:
        """Thin wrapper around `MemoryManager.record_turn()`.

        `rows`（出参）：本函数把它原样透传，
          由 `MemoryManager.record_turn` 填成 `{"user": <行号>, "assistant": <行号>}`，
          `post_process` 再挂到 `ctx.metadata`，`task.end` 带给前端。

        薄壳的签名是独立的，漏一个参数就是 `TypeError` —— 而它打在
        生产路径上（每轮都跑）。`rows=` 只加在了 `MemoryManager` 那边、
        这里没加，每一轮 PostProcessStage 抛 TypeError
回复生成了却留在步骤流里没打出来。
        而全量测试可能一个都不红：假池的 `**kw` 照单全收
替身把真签名遮住了。
必须有一条按签名对账的读者（直接拿 `inspect.signature`
        比双方的参数名）。
        """
        sid = self._resolve_sid(session_key)
        result = self._mgr.record_turn(
            session_id=sid,
            user_message=user_message,
            assistant_message=assistant_message,
            token_count=token_count,
            messages=messages,
            # 两个键都给。`session_id` = 存储键
            # （消息行 / 笔记 / 压缩记账），`channel_key` = 通道键
            # （后台抽取与整理那两个 fork 全按它走）。
            channel_key=session_key,
            rows=rows,
        )
        return result

    def new_session(self, session_key: str) -> Tuple[str, List[dict]]:
        """Force a fresh session (`/clear`). Purges old SQLite + session note.

        这个 key 下的会话可能还没被本进程加载过 —— `/clear` 由
        pre_process 在加载会话之前处理（`pre_process.py` 的 `/clear` 分支
        早于 `pool.get_or_create`）。所以「重启后的第一条消息就是 `/clear`」
        这个再常见不过的情况下，`self._loaded` 里没有这个 key。

        此时不能退回空串：那会让 `MemoryManager.new_session` 铸一个新 id
        并一条都不删 —— 本 key 下的旧行全部留下，下一次重启按 key 又把它们
        装回来（旧对话『复活』），而你刚才那段对话留在一个没有任何东西指向
        的新 id 上，再也找不回来。

        判据："不认识"不能只看内存缓存 —— 用户自己的会话也可能因此被当成不认识。

        回退值取 绑定表（`bound_storage_id(session_key)`），
        不再是"key 自己" —— 通道键指向的可能是另一条铸出来的线程。

三种情形各自的答案是唯一的：
          · `_loaded` 里有，那就是当前线程（同一个进程刚用过）
          · 没有、绑定表里有，绑定说的那条（重启之后走这条）
          · 两个都没有，`""` = 这个通道还没有旧线程，没什么可归档的

        刻意不取 `get_last_session_id()`：认领任意一条全局最近的会话
        正是上面那个"旧对话复活"要防的 bug。
        """
        # 旧 id 走那座桥取，不"拿通道键顶上"。
        #    那个回退只有在"通道键就是存储 id"时才安全；否则
        #    顶上去会让 `/clear` 去数/归档一条没人指过的线程，
        #    而真正的那一条留在原处、绑定还指着它。
        #    没绑过，`""`，"这个通道还没有旧线程"，没什么可数可归档的。
        #      （此处刻意不取 `storage_id_for` —— 那个会铸一条新的并绑上，
        #       于是紧接着的 `new_session` 又铸一条，每次多一个空壳线程。）
        with self._lock:
            old_sid = (self._loaded.get(session_key, (None, []))[0]
                       or self._mgr.short_term.bound_storage_id(session_key) or "")
        # 绑定的写入在 `MemoryManager.new_session(channel_key=...)` 一处
        # （铸 id 的那一步也在那儿，两件事必须同一个事务/同一个所有者，不然会漏）。
        sid, messages = self._mgr.new_session(old_sid, channel_key=session_key)
        self._remember(session_key, (sid, messages))
        return sid, messages

    def open_detached_thread(self, session_key: str, seed: str = "") -> str:
        """铸一条不与通道绑定的新线程（周期任务的 `deliver="new"`）。

        刻意不写 `self._loaded` —— 那是"这个通道现在的段"。
        写进去就等于把用户的当前对话换掉了，而那正是 `deliver="new"` 不许做的事
        （与 :meth:`new_session` 正相反：那条是 `/clear` 的语义）。

        于是同一个 session_key 会短暂地有两个"段"：`_loaded` 里那个
        （用户的）与本轮 `ctx.storage_id` 那个（任务的）。这是有意的——
        判据是"谁的这一轮"：`ctx` 里那个只在这一轮活着，
        而 `_loaded`/绑定表才是"下一次谁进来"的答案。
        """
        return self._mgr.open_detached_thread(session_key, seed)

    def discard_thread(self, storage_id: str) -> None:
        """把框架自己造的那一段整个丢掉（试跑浮窗关掉时）。

        调用方必须先确认这个 id 是我们造的（见 `CronRunner.is_test_segment`）
        —— 否则这就是一条"凭客户端给的一个字符串删掉用户一段对话"的路。
        这里不用管 `_loaded`：独立段（`open_detached_thread`）从来没写进去过，
        那正是它的设计（不抢通道当前那一段）。
        """
        self._mgr.short_term.discard_thread(storage_id)

    def thread_rows(self, channel_key: str) -> List[dict]:
        """这条线程在库里的原始消息行 —— 切点判据只能用它。

        不许拿 `ctx.messages` 代替：那是渲染过的提示形状
        （`MemoryManager.load_session` 会把历史折成一个 `<conversation_history>`
        system 块，记忆档则是逐条 `name="history"` 的副本）。
拿它数轮、拿它的下标当"行数"去切，两个数根本不是一回事 ——
        而症状是"回退到了莫名其妙的地方"。

        `tool_calls` 在库里是保真的（`_insert_message` 把非标准字段整个塞进
        `extra_json`，`load_messages` 原样 `update` 回来），这里的行够
        给 `balanced_prefix_end()` 判平衡。
        """
        return self._mgr.short_term.load_messages(self._resolve_sid(channel_key))

    def list_threads(self, channel_key: str, limit: int = 20) -> List[dict]:
        """这条通道的线程名册，新 → 旧 —— `/resume` 列表的来源。

        每一行附一句 `hint`（最近一条用户消息）—— 没有它这张表就没法用：
          `session_9fk2…` 这种 id 认不出哪条是哪条。
        """
        out = self._mgr.short_term.list_threads(channel_key, limit=limit)
        for t in out:
            t["hint"] = self._mgr.short_term.preview(t["storage_id"], 28)
        return out

    def thread_belongs_to(self, channel_key: str, storage_id: str) -> bool:
        """这个存储 id 是不是本通道名册里的一条。

        它是 `resume` 前面那道闸 —— `resume` 自己不校验存在性
        （`_handle_resume` 的 docstring 逐字：「喂错就是一个凭空建出来的空会话」：
        走 `storage_id_for` 那条"没绑过就铸新"）。`/resume` 那条路上，
        编号是先过了名册再变成 id 的；而"接着某一段"那条路上，
        id 是界面递进来的，必须在下手切之前问一次。

        判据只有一条，且只有一处：`ShortTermMemory.thread_channel`
        （名册那张表按 `storage_id` 反查 `channel_key`）。`/api/messages` 与
        `/api/search` 那两条只读口问的同一个问题（`web/server.py::_desktop_thread`）
        —— 不在这里另写一份（一个口径一个地方）。
        不回答"在不在名册里"以外的任何事 —— 尤其不替它建会话。
        """
        key = str(channel_key or "")
        sid = str(storage_id or "")
        if not key or not sid:
            return False
        try:
            return self._mgr.short_term.thread_channel(sid) == key
        except Exception as exc:  # noqa: BLE001
            # 判不出来，不算数（fail-closed）：这一格的后果是
            #    "把消息追加到另一段对话上"，方向与"少切一次"不对称。
            print(f"[memory] 线程归属查不动（按不属于处理）："
                  f"{type(exc).__name__}: {exc}")
            return False

    def fork_thread(self, channel_key: str,
                    *, keep: int | None = None) -> Tuple[str, List[dict]]:
        """从当前线程分出一条新的并切过去。

        * `keep=None`，`/fork`（整条照抄一份）
        * `keep=N`，`/rewind`（退到第 N 轮之前）

        `keep` 的合法性由调用方保证 —— 它才有 `messages` 列表，
        也只有它能判"这个切点平不平衡"（不平就拒，别往上游发一条 400）。
        """
        src = self._resolve_sid(channel_key)
        new_sid = self._mgr.fork_thread(channel_key, src, keep=keep)
        sid, messages = self._mgr.load_session(new_sid)
        self._remember(channel_key, (sid, messages))
        return sid, messages

    def resume(self, channel_key: str, storage_id: str) -> Tuple[str, List[dict]]:
        """把通道切回一条已经存在的线程。

        与 `new_session` 的差别：它什么都不复制、什么都不删 —— 只是把
        `session_bindings` 那一行指过去，再把名册里那条标成当前。
回去是免费的。

        不校验 `storage_id` 存不存在 —— 调用方（`/resume` 那个分支）
        必须先从 `list_threads()` 里取编号，别拿用户打的一串数字直接喂进来。
        """
        with self._lock:
            prev = (self._loaded.get(channel_key, (None, []))[0]
                    or self._mgr.short_term.bound_storage_id(channel_key))
        if prev and prev != storage_id:
            self._mgr.short_term.end_thread(prev)
        self._mgr.short_term.bind_session(channel_key, storage_id)
        self._mgr.short_term.open_thread(channel_key, storage_id)
        sid, messages = self._mgr.load_session(storage_id)
        self._remember(channel_key, (sid, messages))
        return sid, messages

    def compact(
        self, session_key: str, messages: List[dict]
    ) -> Tuple[List[dict], bool]:
        """Manually compact the session (`/compact`)."""
        sid = self._resolve_sid(session_key)
        #: `channel_key` 只用于机械那两段里"动过的文件"—— 那份账本按通道键记。
        return self._mgr.compact(sid, messages, channel_key=session_key)

    def maybe_trim_cold_window(
        self, session_key: str, messages: List[dict]
    ) -> List[dict]:
        """滑窗冷闸 —— `AgentLoopStage` 在发请求前调它。

        冷（上次活动 > 闸值）且超 `max_history` 才剪；否则原样返回。
        详见 `MemoryManager.maybe_trim_cold_window` 的 docstring。
        """
        sid = self._resolve_sid(session_key)
        return self._mgr.maybe_trim_cold_window(sid, messages)

    def get_context_usage(self, session_key: str, messages: List[dict]) -> dict:
        """Get context-window usage for the session.

        The session id is forwarded because the tool-schema part of the
        estimate is per-session (_CORE_TOOLS ∪ _discovered) — dropping
        it silently switched the display to the 22-tool default.
        这里传的是通道键（不是 `_loaded` 里那个存储 id）。
        `_discovered` 那张发现集是通道作用域的 —— 传存储 id 就会查空，
        显示悄悄退回只有常驻工具的那个偏小数。
        """
        return self._mgr.get_context_usage(messages, session_id=session_key)

    def note_memory_context(self, session_key: str, memory_context: str) -> None:
        """记下这一轮实际注入的记忆 —— 会话构成面板里「记忆注入」那一格的输入。

        由调度器在轮末调（它手上才有 `ctx.memory_context`）。
        记在这里而不是面板现算：现算的唯一入口 `get_context()` 有副作用
        （它会吃掉 `_pending_memory_inject`）。
        """
        from core.context_report import note_memory

        # 通道键 —— 这条读数说的是"这一段对话注入了多少记忆"，
        # 不是"这条消息线程"，而且它要能被 `purge_session(通道键)` 清掉。
        note_memory(session_key, memory_context)

    def context_breakdown(
        self, session_key: str, messages: List[dict] | None = None
    ) -> dict:
        """会话构成面板 —— 把"本会话现在装了什么、各多大"取出来。

        只读：不碰 `_pending_memory_inject`、不改任何状态
        （除了给「与上次比」那一列记一次快照 —— 那是它自己的状态）。

        *messages* 不给就取会话当前的那份（面板在轮外被打开时走这条路，
        那种时候调用方手上没有 `ctx.messages`）。
        """
        if messages is None:
            _sid, messages = self.get_or_create(session_key)
        # 传通道键（它同时是 `_last` / `_last_memory` 两个缓存与
        # `get_tool_schemas_active` 的键 —— 三条都是通道作用域的，
        # 由 `purge_session(通道键)` 统一清）。
        return self._mgr.context_breakdown(session_key, messages)

    @property
    def last_compression(self) -> dict | None:
        """Stats from the most recent compression run."""
        return self._mgr.last_compression

    @property
    def last_blocked(self) -> bool:
        """上一次压缩"该压却一点都没压成"（占比已进强制区）。"""
        return bool(getattr(self._mgr.compressor, "last_blocked", False))

    @property
    def last_skip_note(self) -> str:
        """上一次没压成的真实原因（空串 = 没这回事）。"""
        return self._mgr.last_skip_note

    # ------------------------------------------------------------------
    def session_exists(self, session_key: str) -> bool:
        """会话在 short_term 里有没有行 —— IM 选址的"指向没了的要报错"用它。"""
        return bool(self._mgr.short_term.session_exists(
            self._resolve_sid(session_key)))

    def session_preview(self, session_key: str, limit: int = 24) -> str:
        """会话的一行人话（最近一条用户消息）—— 选址列表用。"""
        return self._mgr.short_term.preview(self._resolve_sid(session_key), limit)

    def _resolve_sid(self, session_key: str) -> str:
        """Return the internal session_id for *session_key*."""
        with self._lock:
            got = self._loaded.get(session_key)
            if got is not None:
                self._loaded.move_to_end(session_key)
                return got[0]
        # If not loaded yet, load it now（走那座桥，与 `get_or_create` 同一条路）
        sid, messages = self._load_thread(session_key)
        with self._lock:
            got = self._loaded.get(session_key)
            if got is not None:          # 别人先加载好了，用他那份
                self._loaded.move_to_end(session_key)
                return got[0]
            self._remember_locked(session_key, (sid, messages))
        return sid

    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying MemoryManager."""
        self._mgr.close()

    # ------------------------------------------------------------------
    # 模型覆盖那套（`set/get/clear_model_override` 与
    #    `self._model_overrides`）已经整体删除 —— 它是一条死路：
    #    写入端有人，而全仓没有任何地方读它
    #用户打 `/model xxx` 拿到一句"切换成功"，下一轮照旧走原模型
    #    （「标签在说谎」）。
    #
    #    现在"这一段用什么模型"只有一个答案：`core/session_model.py`
    #      （按存储 id 冻在 `short_term` 里，`/clear` 换线程，天然回默认）。
    #不是"换了个地方存"，是删掉了第二个看起来也对的地方
    #      （留着它，下一个人会以为两个表是同一件事的两个视图）。
    # ------------------------------------------------------------------
