"""
MessageDeduplicator — prevent duplicate LLM calls when IM platforms
retry message delivery.

Uses an LRU cache of (platform, sender_id, content_hash) tuples
with a configurable TTL.  Entries expire automatically after the TTL
and are ejected from the LRU when the cache is full.

Pattern borrowed from chat-on-wechat's `ExpiredDict` used in its
Feishu and DingTalk channels.

MIT License, Copyright (c) 2022 zhayujie.
License text: `licenses/MIT-chat-on-wechat.txt`.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional


class MessageDeduplicator:
    """Thread-safe message dedup with LRU eviction and TTL expiry.

    Args:
        max_entries: Maximum number of entries in the LRU cache.
        ttl_seconds: How long a message hash is remembered (default 300s = 5 min).
        persist_path: 可选 —— 跨重启的「这条已经答过了」账本（JSON）。
        persist_ttl_seconds: 账本里记多久（默认 24 小时）。
        persist_max_entries: 账本最多留多少条（超了删最旧的）。

    为什么需要持久层：飞书在重启之后重投了一条
      47 分钟前的消息（用户 12:55 发的「消息 + PDF」）。内存层 TTL 只有 300 秒、
      而且进程重启就空了，那条被当成新消息又答了一遍，回复还引用了 12:55
      那条旧消息（用户看到的正是「它怎么自己又回了一遍」）。
「同一条消息答过没有」必须活得比进程久。

    只持久化带 `message_id` 的那些。没有消息号时用的是内容哈希，
      而"同一个人连发两句一模一样的话"是合法的 —— 把它也落盘会让第二次
      被静默吞掉（那比重复答复更糟）。

    生命周期 = `pending` → `done`：
      `is_duplicate()` 只查 + 占位（`pending`），成功才落账
      （`mark_done`，进持久层），没成功由调用方 `release()` 放回去。
      判据："见过这条"与"做成了这条"是两件事 —— 旧实现把两者合成一次写，
      于是管线后面任何环节失败，平台重投都会被它挡下（用户什么都收不到），
      而账本上写着"处理过了"。
    """

    def __init__(self, max_entries: int = 10_000, ttl_seconds: float = 300.0,
                 *, persist_path: str | Path | None = None,
                 persist_ttl_seconds: float = 24 * 3600.0,
                 persist_max_entries: int = 5000) -> None:
        self._max = max_entries
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        #: key -> `(时间戳, 状态)`；状态是 `"pending"`（收到了、还在处理）
        #: 或 `"done"`（处理成功）。两种都算"这条不用再来一次"，
        #: 但只有 `done` 会落盘 —— 见 `is_duplicate` 那段判据。
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        #: 持久层：键同内存层（`platform:mid:<id>`），值是 wall clock 秒
        #: （`monotonic` 跨进程没有意义）。
        self._persist_path = Path(persist_path) if persist_path else None
        self._persist_ttl = float(persist_ttl_seconds)
        self._persist_max = int(persist_max_entries)
        self._seen: dict[str, float] = {}
        self._save_warned = False
        if self._persist_path is not None:
            self._load_seen()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def key_for(platform: str, sender_id: str, content: str,
                message_id: Optional[str] = None, scope: str = "") -> str:
        """这条消息的去重键。

        `scope` 是会话维度，不是装饰：Telegram 的 `message_id`
        只在单个 chat 内唯一（Bot API 的规定），私聊和群里各自第 1 条
        会撞，没有它，第二条会被当成"重复投递"静默丢掉。
        没有 `scope` 的老键形仍然算得出来（调用方不给就是空串）。
        """
        if message_id:
            prefix = f"{platform}:{scope}:" if scope else f"{platform}:"
            return f"{prefix}mid:{message_id}"
        snippet = content[:200]
        content_hash = hashlib.sha256(
            f"{platform}:{scope}:{sender_id}:{snippet}".encode()
        ).hexdigest()[:16]
        return f"{platform}:hash:{content_hash}"

    def is_duplicate(
        self,
        platform: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
        scope: str = "",
    ) -> bool:
        """这条消息要不要跳过。查的同时把它标成 `pending`。

        If *message_id* is provided it is used as the primary dedup key
        (most IM platforms provide unique message IDs).  Otherwise a
        content hash of the first 200 chars is used.

        判据：原来"查"和"记"是同一个
        动作，标记发生在管线最前面，后面任何环节失败（模型超时 / 回复发不
        出去），平台重投都会被这条挡下 —— 用户什么都收不到。
        现在这里是 "查 + 占位"，成功才落账（`mark_done`），
        失败由调用方 `release` 把位子放回去（下一次重投就能正常处理）。

        *scope* = 会话/chat 维度（见 `key_for`）。
        """
        dedup_key = self.key_for(platform, sender_id, content,
                                 message_id=message_id, scope=scope)

        with self._lock:
            self._expire_stale()

            if dedup_key in self._cache:
                # Touch — move to end (most recently used)
                self._cache.move_to_end(dedup_key)
                ts, state = self._cache[dedup_key]
                self._cache[dedup_key] = (time.monotonic(), state)
                return True

            # 跨重启那一层（只认带消息号的，且只认处理成功过的）
            if self._persist_path is not None and message_id:
                if self._seen_hit(dedup_key):
                    return True

            self._remember_pending(dedup_key)
            return False

    def mark_done(
        self,
        platform: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
        scope: str = "",
    ) -> None:
        """这一条真的处理完了（成功 = `PostProcessStage` 走完）。

        只有走到这里的键才进持久层 —— "见过"与"做成了"是两件事，
        而跨重启那一层要的是后者。
        """
        dedup_key = self.key_for(platform, sender_id, content,
                                 message_id=message_id, scope=scope)
        with self._lock:
            self._expire_stale()
            self._remember_pending(dedup_key)
            self._cache[dedup_key] = (time.monotonic(), "done")
            self._cache.move_to_end(dedup_key)
            if self._persist_path is not None and message_id:
                self._remember(dedup_key)

    def release(
        self,
        platform: str,
        sender_id: str,
        content: str,
        message_id: Optional[str] = None,
        scope: str = "",
    ) -> None:
        """这一轮没成功，把占位放回去，让平台重投时能被正常处理。

        只放 `pending` 的位子：已经 `done` 的那条不许被一条迟到的失败路径
        抹掉（那会让同一条消息再答一遍）。
        """
        dedup_key = self.key_for(platform, sender_id, content,
                                 message_id=message_id, scope=scope)
        with self._lock:
            cur = self._cache.get(dedup_key)
            if cur is not None and cur[1] == "pending":
                del self._cache[dedup_key]

    def clear(self) -> None:
        """Remove all entries（内存 + 持久层都清）。

        `/clear` 不调它 —— 用户清空对话之后，一条更早消息的重投仍然要拦。
        这里只给测试用。
        """
        with self._lock:
            self._cache.clear()
            self._seen.clear()
            if self._persist_path is not None:
                self._save_seen()

    @property
    def size(self) -> int:
        with self._lock:
            self._expire_stale()
            return len(self._cache)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _remember_pending(self, key: str) -> None:
        """放进内存层（标 `pending`）并做 LRU 淘汰。锁内调用。

        `pending` 也占 LRU 名额：否则"一大批没处理成功的消息"会把内存
        撑住不放（它们不该进持久层，但也没理由无限留着）。
        """
        if key not in self._cache:
            self._cache[key] = (time.monotonic(), "pending")
        self._cache.move_to_end(key)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)

    def _expire_stale(self) -> None:
        """Remove entries older than TTL.  Must be called under lock."""
        now = time.monotonic()
        expired = [
            k for k, (ts, _state) in self._cache.items()
            if now - ts >= self._ttl
        ]
        for k in expired:
            del self._cache[k]

    # ---- 跨重启那一层（全部在锁里调用） --------------------------------

    def _seen_hit(self, key: str) -> bool:
        """持久层里有没有这条（过期的不算）。命中就顺手续期。"""
        now = time.time()
        self._prune_seen(now)
        if key not in self._seen:
            return False
        self._seen[key] = now
        self._save_seen()
        return True

    def _remember(self, key: str) -> None:
        now = time.time()
        self._seen[key] = now
        self._prune_seen(now)
        self._save_seen()

    def _prune_seen(self, now: float) -> None:
        """丢掉过期的；还超容量就丢最旧的。"""
        stale = [k for k, ts in self._seen.items()
                 if now - ts >= self._persist_ttl]
        for k in stale:
            del self._seen[k]
        if len(self._seen) > self._persist_max:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[
                    : len(self._seen) - self._persist_max]:
                del self._seen[k]

    def _load_seen(self) -> None:
        try:
            raw = self._persist_path.read_text(encoding="utf-8")   # type: ignore[union-attr]
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as exc:                                  # noqa: BLE001
            # 读不到就当空的（不阻断对话），但要出声 —— 否则"跨重启去重
            #   不生效"这件事在用户侧看起来就是"它又答了一遍"。
            print("[dedup] 已处理消息账本读不出来（%s: %s）—— 跨重启去重这一轮不生效"
                  % (type(exc).__name__, exc))
            return
        if isinstance(data, dict):
            now = time.time()
            self._seen = {str(k): float(v) for k, v in data.items()
                          if isinstance(v, (int, float))
                          and now - float(v) < self._persist_ttl}

    def _save_seen(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(
                self._persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._seen), encoding="utf-8")
            tmp.replace(self._persist_path)
        except Exception as exc:                                  # noqa: BLE001
            if not self._save_warned:      # 只吵一次（这条路径可能每轮都走）
                self._save_warned = True
                print("[dedup] 已处理消息账本写不进去（%s: %s）—— 重启后可能重复答复"
                      % (type(exc).__name__, exc))


# 进程级实例

#: 一个进程只有这一份。`pre_process` 那道闸（占位）与轮末的
#: `mark_done` / `release` 必须是同一个实例 —— 分成两份就是"占位写在 A、
#: 落账写在 B"，两边都看不出错，而重投照样被挡。
#:
#: 惰性建：模块导入时不碰盘（`tests/_sandbox.py` 靠 chdir 隔离，
#:    导入期读一次账本会把"沙箱里有没有那个文件"变成一个隐蔽的依赖）。
_default: Optional["MessageDeduplicator"] = None


def default() -> "MessageDeduplicator":
    """取进程级那一份（第一次调用时建，之后同一个）。"""
    global _default
    if _default is None:
        #: 跨重启那一层指向 `data/runtime/seen_messages.json` —— 飞书会在
        #:   重启后重投几十条前的消息，只放内存里就挡不住那一批。
        _default = MessageDeduplicator(
            max_entries=10_000, ttl_seconds=300.0,
            persist_path="data/runtime/seen_messages.json")
    return _default
