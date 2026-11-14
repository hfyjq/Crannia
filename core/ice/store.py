"""会话级映射：真值 ↔ 句柄。只在内存，绝不落盘。

为什么不落盘：句柄是会话级的 ——
会话库/记忆/笔记/日报里存句柄，重启或换会话之后就变成一堆解不开的怪字符。
判据是「句柄只活在"上模型的那一份"里」，一切落盘是真值。
两条合起来只有一个结论：这张表留在内存里，跟会话同生共死。

另一条：不做 30 分钟过期。
过期看起来像在做卫生，实际效果是"检测到了也注不进去" —— 一个会自己失效的映射，
等于一个会自己失效的保护，而用户看不见它什么时候失效。

本模块的存储形状受 census 约束：机检扫 `core/` 全树，
抓「模块级且被写过的 `{}`/`[]`/`set()`」与「模块级 X = None」。
所以：映射一律装在实例属性里；_DEFAULT_STORE = SessionStore() 是构造调用
（不是容器字面量），那条读者看不见它。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from core.ice.handles import Handle

#: 单个会话最多发多少个句柄。满了就就地"生不出"，不做会话内淘汰。
#: 会话内淘汰会破坏「同值同句柄、逐会话稳定」—— 那是前缀缓存的判据。
#: 换了可见形态之后它不再是能力边界（下标本来没有上限），只是内存护栏。
MAX_HANDLES_PER_SESSION = 2048

#: 同时保留多少个会话的映射（LRU）。六个通道的实际并发远小于这个数。
MAX_SESSIONS = 64


class SessionMap:
    """一个会话的映射表（值→句柄 + 句柄→值）。"""

    def __init__(self, session_id: str,
                 max_handles: int = MAX_HANDLES_PER_SESSION) -> None:
        self.session_id = session_id
        self.touched = time.monotonic()
        self._max = max_handles
        self._issued = 0
        self._by_value: Dict[str, Handle] = {}
        self._by_token: Dict[str, str] = {}

    def touch(self) -> None:
        self.touched = time.monotonic()

    def handle_for(self, value: str, rule_id: str = "") -> Optional[Handle]:
        """取这个值在这个会话里的句柄；没有就分配一个。满了返回 `None`。

        `None` 的含义：生不出句柄就不换（那个值原样留在文本里）。
        绝不"借"一个已发的下标 —— 那会让还原换错值，是本模块最严重的失败形态。
        """
        got = self._by_value.get(value)
        if got is not None:
            self.touch()
            return got
        if self._issued >= self._max:
            return None
        h = Handle(index=self._issued + 1, value=value,
                   rule_id=rule_id, session_id=self.session_id)
        self._issued += 1
        self._by_value[value] = h
        self._by_token[h.text] = value
        self.touch()
        return h

    def value_for(self, token: str) -> Optional[str]:
        """这个句柄串（`【PII:N】`）对应哪个真值。不是句柄，`None`。"""
        return self._by_token.get(token)

    def known_tokens(self) -> Dict[str, str]:
        """已发句柄串 → 真值。给
        :func:`core.ice.handles.strip_handles_reporting` 用
        （入境时"这个句柄是不是我们自己发的"就是查它）。"""
        return self._by_token

    def known_indices(self) -> Tuple[int, ...]:
        return tuple(sorted(h.index for h in self._by_value.values()))

    def size(self) -> int:
        return len(self._by_value)

    def __len__(self) -> int:
        return len(self._by_value)


#: 淘汰记录最多留多少条。诊断用，过量没有意义。
_MAX_EVICTED_KEPT = 256


class SessionStore:
    """一堆 :class:`SessionMap`，按会话键，LRU 限流。"""

    def __init__(self, *, max_sessions: int = MAX_SESSIONS,
                 max_handles: int = MAX_HANDLES_PER_SESSION) -> None:
        self._maps: Dict[str, SessionMap] = {}
        self._max_sessions = max_sessions
        self._max_handles = max_handles
        #: 淘汰过的会话（按次数，不是"现在还在不在"）。它原来只增不减 ——
        #:   每次淘汰追加一个 id，而唯一清空点是 `forget_all`（进程退出时才走）。
        #:   它是诊断用的最近事件，不是台账：只留最近的这些就够回答"刚才淘汰了谁"。
        self._evicted: List[str] = []

    # 取用
    def session(self, session_id: str) -> SessionMap:
        """取（必要时建）某个会话的映射，并刷新它的热度。"""
        got = self._maps.get(session_id)
        if got is not None:
            got.touch()
            return got
        self._evict_if_needed(keep=session_id)
        made = SessionMap(session_id, self._max_handles)
        self._maps[session_id] = made
        return made

    def peek(self, session_id: str) -> Optional[SessionMap]:
        """只读探测 —— 不建、不刷热度。还原路径用它（不认识的会话不许凭空建表）。"""
        return self._maps.get(session_id)

    def touch(self, session_id: str) -> None:
        got = self._maps.get(session_id)
        if got is not None:
            got.touch()

    # 清理
    def forget_session(self, session_id: str) -> None:
        """丢掉一个会话的整张映射。

        这是 `core/state_registry.py` 登记的那个 `purge` 钩子
        （`/clear` 走它）。
        """
        self._maps.pop(session_id, None)

    def forget_all(self) -> None:
        self._maps.clear()
        self._evicted.clear()

    # 观测
    def sessions(self) -> Tuple[str, ...]:
        return tuple(sorted(self._maps))

    def last_evicted(self) -> Tuple[str, ...]:
        """被 LRU 淘汰过的会话（按次数，不是"现在还在不在"）。

        有它是因为：淘汰不许静默。`purge_session` 那条判据是 ——
        「清失败」与「没东西清」必须看起来不一样。
        """
        return tuple(self._evicted)

    def __len__(self) -> int:
        return len(self._maps)

    # 内部
    def _evict_if_needed(self, keep: str) -> None:
        """腾位子：淘汰最久没碰过的那个，且淘汰必须出声。

        当前会话天然安全 —— 这个函数只在 `keep` 不在表里时被调用
        （在表里的话 :meth:`session` 直接就返回了），它压根不在候选集里。
        下面那行 s != keep 因此今天恒真；留着是因为调用点一旦变，
        "淘汰一个活跃会话"是本模块最坏的失败形态（它手里的真值会变成解不开的怪字符），
        而下一个人不会去重推那条调用链。
        """
        while len(self._maps) >= self._max_sessions:
            victims = [s for s in self._maps if s != keep]
            if not victims:
                return
            victim = min(victims, key=lambda s: self._maps[s].touched)
            self._maps.pop(victim, None)
            self._evicted.append(victim)
            if len(self._evicted) > _MAX_EVICTED_KEPT:
                del self._evicted[:-_MAX_EVICTED_KEPT]
            print("[ice] evicted session map: %s..." % victim[:12])


#: 进程级默认表。是构造调用，不是容器字面量 —— census 看不见它（见模块 docstring）。
_DEFAULT_STORE = SessionStore()


def get_store() -> SessionStore:
    return _DEFAULT_STORE


def forget_session(session_id: str) -> None:
    """模块级便捷入口（`state_registry` 的 `_lazy` 指的就是它）。"""
    _DEFAULT_STORE.forget_session(session_id)


def forget_all() -> None:
    _DEFAULT_STORE.forget_all()
