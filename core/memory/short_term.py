"""
Short-term memory: SQLite-backed message store with session management.

Messages are saved after every turn and can be restored on startup
so conversations survive process restarts.

Tables:
  sessions  — one row per conversation session
  messages  — one row per message in a session
"""

import functools
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 消息 `content` 的唯一文本视图（见 `_insert_message` 与
#   `replace_messages` 那两处）。
from core.image_input import content_text

# 压缩边界这一行的 `kind`。
#
# 压缩不删原话 —— 它只做两件事：把新摘要追加进表，再追加一条
# 「边界」行记住*活动窗口从哪一行起*。被压掉的原话一行不动地留在库里
#`history_search` 真能搜回。若改成 DELETE + INSERT 整表，
# 原件就物理消失，搜索永远找不到它们。
#
# 不加列、不加迁移 —— 边界就是一行，它的「从哪一行起」存在
#   `extra_json` 里（`_insert_message` 本来就把非 role/content 的字段收进去）。
#空表 / 老库（没有 boundary 行）天然等于默认行为（horizon = 0）。
COMPACTION_BOUNDARY_KIND = "compaction_boundary"


#: 同一连接上的读写串行化：事务边界属于连接，不属于进入 `with` 的线程。
_DB_LOCK = threading.RLock()


def _serialized(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _DB_LOCK:
            return fn(*args, **kwargs)
    return wrapper


class ShortTermMemory:
    """SQLite-backed conversation message store."""

    def __init__(self, db_path: str = "data/memory/short_term.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        # 从较早一版起框架代笔的 user 行带 `kind`；旧行没有 ——
        #    不补的话，重启后那些行照样长成用户气泡（实际就是一条旧行）。
        try:
            _n = self.backfill_framework_notes()
            if _n:
                print("[memory] 已给 %d 条旧消息补上 framework_note 标记" % _n)
        except Exception as exc:                       # noqa: BLE001
            print("[memory] 旧消息补标记失败（不影响启动）：%s: %s"
                  % (type(exc).__name__, exc))

    @staticmethod
    def _framework_note_heads() -> tuple:
        """框架代笔那条 user 消息的开头（只服务旧行；新行落库时已带 `kind`）。

        两段都从产者那里取，不在这儿抄字面量：
          审批那一头有机械读者钉着「全仓只有 `core/approval.py` 拼得出来」。
        只认开头；正文里出现同样的字不算。
        """
        heads = []
        try:
            from core.tasks_notify import NOTIFICATION_PREFIX
            heads.append(NOTIFICATION_PREFIX)
        except Exception:                              # noqa: BLE001
            pass
        try:
            from core.approval import APPROVAL_HEAD
            heads.append("[系统] " + APPROVAL_HEAD)
        except Exception:                              # noqa: BLE001
            pass
        return tuple(heads)

    @_serialized
    def backfill_framework_notes(self) -> int:
        """给 在此之前落库的框架代笔消息补上 `kind`。返回补了几条。

        为什么要补、而不是在读取时猜：判据只留一处（`kind`）。
          在读取路径上再嗅一遍内容 = 同一条规矩的第二个实现，而且它只在
          "旧行"上生效 —— 永远测不全，也没有人会去维护它。
        """
        heads = self._framework_note_heads()
        if not heads:
            return 0
        conds = " OR ".join("ltrim(COALESCE(content, '')) LIKE ?" for _ in heads)
        rows = self._conn.execute(
            "SELECT id, content, extra_json FROM messages "
            "WHERE role = 'user' "
            "  AND instr(COALESCE(extra_json, ''), 'framework_note') = 0"
            "  AND (" + conds + ")",
            tuple(h + "%" for h in heads),
        ).fetchall()
        changed = 0
        for mid, content, extra_json in rows:
            if not (content or "").lstrip().startswith(heads):
                continue           # LIKE 认得、Python 不认，不信，跳过
            try:
                extra = json.loads(extra_json) if extra_json else {}
            except json.JSONDecodeError:
                extra = {}
            if not isinstance(extra, dict):
                extra = {}
            extra["kind"] = "framework_note"
            self._conn.execute(
                "UPDATE messages SET extra_json = ? WHERE id = ?",
                (json.dumps(extra, ensure_ascii=False), mid),
            )
            changed += 1
        if changed:
            self._conn.commit()
        return changed

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @_serialized
    def init_session(self, session_id: str | None = None) -> str:
        """
        Create a new session or verify an existing one.

        Returns the session_id.
        """
        if session_id is None:
            session_id = f"session_{uuid.uuid4().hex[:12]}"

        now = _now()
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at, updated_at) "
            "VALUES (?, ?, ?)",
            (session_id, now, now),
        )
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        self._conn.commit()
        return session_id

    # ------------------------------------------------------------------
    # 通道键 -> 存储 id 的绑定
    # ------------------------------------------------------------------

    @_serialized
    def bind_session(self, channel_key: str, storage_id: str) -> None:
        """把通道键指向当前的存储 id。

        它是那条恒等式里唯一一张持久的「这个通道现在在哪条线程上」的表
        —— `SessionPool._loaded` 是同一件事的内存副本，进程一停就没了，
        而 `/clear` 会给存储面铸新 id，没有它，重启之后就找不回
        当前那一段（只能回落到"按通道键加载"，那读到的正是第一条线程）。

        为什么落在这张库里：绑定与 `sessions` / `messages` 必须
        同生共死 —— 拆成两个事务就会出现「有线程、没人指」或
        「指向一条不存在的线程」。同一张库 = 同一个事务 = 不可能分叉。
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO session_bindings "
            "(channel_key, storage_id, bound_at) VALUES (?, ?, ?)",
            (str(channel_key), str(storage_id), _now()),
        )
        self._conn.commit()

    @_serialized
    def bound_storage_id(self, channel_key: str) -> str | None:
        """纯查询：这个通道键绑在哪条线程上；没绑过，`None`。

        与 `MemoryManager.storage_id_for` 是两个动词：那个会铸新，
        这个只回答"有没有"。凡是「问在不在」的调用（路由判活那一族）
        必须走这一条 —— 否则"查一下"就会凭空建出一个会话。
        """
        key = str(channel_key or "")
        if not key:
            return None
        row = self._conn.execute(
            "SELECT storage_id FROM session_bindings WHERE channel_key = ?",
            (key,),
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # 线程历史 —— 所有存档线程的名册
    # ------------------------------------------------------------------
    #
    # 与 `session_bindings` 的分工（别合并成一张表）：
    #   · `session_bindings` = 当前指针（一行、每次 `/clear` 覆盖）
    #   · `session_threads`  = 历史名册（追加、永不覆盖）
    # 合并的那一刻，"当前是哪条"与"历史上有哪些"就变成同一个问题了 ——
    # 而那张表只有 `channel_key` 一个主键，覆盖一次就再也列不出历史。
    #
    # `seq` 用 AUTOINCREMENT 而不是拿 `started_at` 排序：
    # 同一秒内连按两次 `/clear` 是真实会发生的，
    # 而 `started_at` 只到秒，两条线程的顺序不定。

    @_serialized
    def open_thread(self, channel_key: str, storage_id: str) -> None:
        """把一条线程登记进名册，并把它标成当前（`ended_at = NULL`）。

        幂等：已经登记过就只把 `ended_at` 清掉（`/resume` 切回旧线程走这条）。
        """
        key = str(channel_key or "")
        sid = str(storage_id or "")
        if not key or not sid:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO session_threads "
            "(storage_id, channel_key, started_at, ended_at, turn_count) "
            "VALUES (?, ?, ?, NULL, 0)",
            (sid, key, _now()),
        )
        self._conn.execute(
            "UPDATE session_threads SET ended_at = NULL WHERE storage_id = ?",
            (sid,),
        )
        self._conn.commit()

    @_serialized
    def end_thread(self, storage_id: str) -> None:
        """把一条线程标成已结束（它还在名册里，只是不再是当前那条）。"""
        sid = str(storage_id or "")
        if not sid:
            return
        self._conn.execute(
            "UPDATE session_threads SET ended_at = ? WHERE storage_id = ?",
            (_now(), sid),
        )
        self._conn.commit()

    @_serialized
    def bump_thread_turn(self, storage_id: str) -> None:
        """一条线程多了一轮（`record_turn` 调）—— 列表里那个「N 轮」的来源。"""
        sid = str(storage_id or "")
        if not sid:
            return
        self._conn.execute(
            "UPDATE session_threads SET turn_count = turn_count + 1 "
            "WHERE storage_id = ?",
            (sid,),
        )
        self._conn.commit()

    @_serialized
    def thread_turn_count(self, storage_id: str) -> int:
        """这条线程已经完成了几轮（文件留档要拿它当"第几轮"的戳）。

        为什么用它而不是现数消息：它在一轮之内是稳定的 ——
        `bump_thread_turn` 只在轮末（`record_turn`）加一次。
        而"现数消息"会在轮内随着工具结果落库而变，同一轮里先后碰的两个文件
        会拿到两个不同的轮次号，回退那天就分不清它们是不是同一轮的。
        """
        sid = str(storage_id or "")
        if not sid:
            return 0
        row = self._conn.execute(
            "SELECT turn_count FROM session_threads WHERE storage_id = ?",
            (sid,),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    @_serialized
    def list_threads(self, channel_key: str, limit: int = 20) -> list:
        """这个通道的线程名册，新 → 旧。每项一个 dict。

        按 `seq` 排，不按 `started_at`（同一秒连按两次 `/clear` 是真实的；
        只到秒的时间戳排不出先后）。

        也不许把"当前"那条提到最前面：
        用户读了【2】、打了 `/resume 2`，切过去之后若列表重排，`2` 就变成
        另一条了 —— 编号不稳定 = 那段编号没用。顺序只由插入序决定，
        "当前"只体现为那个标记（它可以出现在任何一格）。
        """
        key = str(channel_key or "")
        if not key:
            return []
        rows = self._conn.execute(
            "SELECT storage_id, started_at, ended_at, turn_count "
            "FROM session_threads WHERE channel_key = ? "
            "ORDER BY seq DESC LIMIT ?",
            (key, max(1, int(limit))),
        ).fetchall()
        return [
            {"storage_id": r[0], "started_at": r[1], "ended_at": r[2],
             "turn_count": int(r[3] or 0), "is_current": r[2] is None}
            for r in rows
        ]

    # 置顶（「置顶此对话 / 此集」）
    #: 两种 `kind` —— 「对话」是那一段、「集」是左栏那个目录。
    PIN_KINDS = ("thread", "group")

    @_serialized
    def set_pin(self, kind: str, key: str, on: bool) -> bool:
        """置顶 / 取消置顶。返回"现在是不是置顶的"。

        不做归属核对 —— 那是端点的事（照 `/api/threads/delete` 那条：
        *能不能动，由服务端说了算，不由请求体说了算*）。这一层只管存取。
        键是空串 / `kind` 不认识，拒（返回 `False` 并且什么都不写）：
        静默写进一行按空键索引的置顶，只会在排序那一步表现为"莫名有一条排前面"。
        """
        k, s = str(kind or ""), str(key or "")
        if k not in self.PIN_KINDS or not s:
            return False
        if on:
            #: `at` 用写入那一刻，先置顶的排前面（重排一次就排到后面去，
            #: 那是"最近置顶的排最后"还是"最前"的选择题 —— 这里选保持原位：
            #: 已经置顶的再点一次不该让它跳位置）。
            self._conn.execute(
                "INSERT OR IGNORE INTO pins (kind, key, at) VALUES (?, ?, ?)",
                (k, s, time.time()))
        else:
            self._conn.execute("DELETE FROM pins WHERE kind = ? AND key = ?", (k, s))
        self._conn.commit()
        return bool(on)

    @_serialized
    def pinned(self, kind: str) -> List[str]:
        """这个 `kind` 下全部置顶的键，先置顶的排前面。

        空表，空列表（= 默认行为，谁都不置顶）。不许在这里兜底排序。
        """
        rows = self._conn.execute(
            "SELECT key FROM pins WHERE kind = ? ORDER BY at ASC",
            (str(kind or ""),)).fetchall()
        return [r[0] for r in rows]

    @_serialized
    def count_threads(self, channel_key: str) -> int:
        """这个通道一共有多少条线程 —— 不受 `list_threads` 的 `limit` 影响。

        50 条提醒那一格要的是总数，
          而 `list_threads` 只给一页（`limit=30`），从那一页推总数必然说假话
          （拿到 30 条就说"有 30 条"，而真实可能是 300）。
          与 `history_search` 同族：报数就报真数。
        但"真数"指的是"真对话"：空壳不算 ——
          判据与左栏 / `pre_process._visible_threads` 同一条：
          `turn_count > 0 OR ended_at IS NULL`（当前那一段即使是 0 轮也留着）。
          以前这里是 `COUNT(*)`，`/clear` 留下的空壳把「50 条提醒」的阈值
          顶高了，而界面上一条都看不见（列表是过滤过的）—— 数字和列表在说两件事。
        """
        key = str(channel_key or "")
        if not key:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM session_threads WHERE channel_key = ? "
            "AND (turn_count > 0 OR ended_at IS NULL)", (key,)
        ).fetchone()
        return int(row[0] or 0) if row else 0

    @_serialized
    def copy_messages(self, src_sid: str, dst_sid: str, *, keep: int | None = None) -> int:
        """把 *src_sid* 的前 *keep* 条复制到 *dst_sid*，返回复制了几条。

        `/rewind` 与 `/fork` 共用的那一台。

        * `keep=None`，全部（`/fork`：整条照抄一份）
        * `keep=N`，前 N 条（`/rewind`：退到第 N 轮之前）

        是复制，不是移动 —— 源线程一条都不动。这是"永不销毁"的底线，
        也是 `/rewind` 之后还能 `/resume` 回去的原因。
        新行拿到的是新的 `id`（AUTOINCREMENT）—— 这是对的：
        两条线程从此各自往前走，行号不该再互相约束。
        """
        rows = self._conn.execute(
            "SELECT role, content, extra_json, token_count, created_at "
            "FROM messages WHERE session_id = ? ORDER BY id ASC",
            (str(src_sid),),
        ).fetchall()
        if keep is not None:
            rows = rows[:max(0, int(keep))]
        if not rows:
            return 0
        self._conn.executemany(
            "INSERT INTO messages "
            "(session_id, role, content, extra_json, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(str(dst_sid), r[0], r[1], r[2], r[3], r[4]) for r in rows],
        )
        self._conn.commit()
        return len(rows)

    @_serialized
    def get_last_session_id(self) -> str | None:
        """Return the most recently updated session_id, or None."""
        row = self._conn.execute(
            "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    @_serialized
    def discard_thread(self, storage_id: str) -> None:
        """把一条线程整个抹掉：消息 + 会话行 + 名册行。

        试跑浮窗用的线程：那一段是框架为一次测试造的，
        不是用户的对话，关掉浮窗就一条都不留。

        与 `/clear` 正相反，别混：`/clear` 是留档 ——
        旧行原样留着、只换指针；这里连名册行一起删
        （留着它会变成一个点进去什么都没有的幽灵任务）。
        """
        sid = str(storage_id or "")
        if not sid:
            return
        self.delete_session(sid)
        self._conn.execute("DELETE FROM session_threads WHERE storage_id = ?", (sid,))
        self._conn.commit()

    @_serialized
    def delete_session(self, session_id: str) -> None:
        """Delete a session and all its messages."""
        self._conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        self._conn.execute(
            "DELETE FROM sessions WHERE session_id = ?", (session_id,)
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Message CRUD
    # ------------------------------------------------------------------

    @_serialized
    def _insert_message(
        self,
        session_id: str,
        message: Dict,
        token_count: int = 0,
    ) -> int:
        """只 `INSERT`，不 commit —— 事务的边界由调用方定。

        不能让这里 commit：`save_messages` 走 `with self._conn:` +
        循环调本函数；若这里自己 commit，那条"单事务"每次都会被切碎 ——
        "single transaction" 就只写在 docstring 里（name 是真的，行为是假的）。
        """
        role = message.get("role", "")
        # 必须过 `content_text` —— 带图那一轮的
        # `content` 是一个块列表（OpenAI 形状），而这一列是 TEXT。
        # 直接把 list 绑上去，`sqlite3.InterfaceError`，整轮落库失败
        # （而且是先写盘再回用户的那条路，用户看不到回复）。
        # 不在这里 `json.dumps`：那会把整张图的 base64 写进库里，
        # 且装载回来是一块模型的输入（它看不懂，还会被算进 token）。
        # `content_text` 给的是一句带路径的指针 —— 下一轮
        # `image_input.expand_attachments` 靠它把图装回来。
        content = content_text(message.get("content"))

        # Store ALL non-standard fields in extra_json so we never
        # lose tool_call_id, tool_calls, name, etc.
        extra = {
            k: v for k, v in message.items()
            if k not in ("role", "content")
        }
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None

        # 把新行号还回去。
        #   前端要知道"这一轮落库之后是哪一行"，才做得成两件事：
        #   ① 聊天区那条消息上的 （收藏存的是引用 `stars.message_id`）
        #   ② 点侧栏旧对话装载回聊天区之后"滚到哪一行"
        #   返回 `None` 会让行号只活在库里，前端一条都拿不到
        #   （`/api/chat` 回的是 `{reply,tokens,files}`，`task.end` 也没有 id）。
        #   用 `lastrowid` 而不是"上一行 + 1"：AUTOINCREMENT 只保证
        #   单调，回滚/失败会留洞；`lastrowid` 是 sqlite 自己给的答案。
        cur = self._conn.execute(
            "INSERT INTO messages (session_id, role, content, extra_json, "
            "token_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, content, extra_json, token_count, _now()),
        )
        return int(cur.lastrowid or 0)

    @_serialized
    def save_message(
        self,
        session_id: str,
        message: Dict,
        token_count: int = 0,
    ) -> None:
        """Persist a single message to the database —— 自己开一个事务。"""
        self._insert_message(session_id, message, token_count)
        self._conn.commit()

    @_serialized
    def save_messages(
        self,
        session_id: str,
        messages: List[Dict],
        token_counts: List[int] | None = None,
    ) -> List[int]:
        """Persist multiple messages in a single transaction.

        → 返回新落那几行的行号（与 *messages* 一一对应、同序）。
          读者是前端那条地基：靠它把"内存里那条消息"对到"库里那一行"。
          返回值是加性的 —— 忽略返回值的调用方行为一个字节不变。

        一次 `commit`（= 一次 fsync）而不是 N 次。一轮对话要落 3 条以上消息，
        而每条各自 `commit`，每条一次 fsync。
        机械读者 `tests/test_short_term_batch.py` 按 `COMMIT` 的条数钉着它
        （不是按"代码里有 `with self._conn`"）。
        """
        if token_counts is None:
            token_counts = [0] * len(messages)

        ids: List[int] = []
        with self._conn:
            for msg, tc in zip(messages, token_counts):
                ids.append(self._insert_message(session_id, msg, tc))
        return ids

    @_serialized
    def replace_messages(
        self,
        session_id: str,
        messages: List[Dict],
    ) -> None:
        """Rewrite a session's stored rows to be exactly *messages*, in order.

        **它唯一的调用方是 `MemoryManager.repersist_messages`**
        （审批重放要就地改一条已落库的旧消息，只追加那条路写不进去）。
        别再把它当压缩的落点：它整表重写，
        会把压缩前的原话真的删掉（压缩的落点是 `append_compaction`）。

        Atomic (one transaction).
        """
        now = _now()
        rows = []
        for msg in messages:
            role = msg.get("role", "")
            # 与 `_insert_message` 逐字同一条（两份会漂，
            #   而漂掉的那份只会在这条路上崩）。
            content = content_text(msg.get("content"))
            extra = {
                k: v for k, v in msg.items()
                if k not in ("role", "content")
            }
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
            rows.append((session_id, role, content, extra_json, 0, now))

        with self._conn:
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            self._conn.executemany(
                "INSERT INTO messages (session_id, role, content, "
                "extra_json, token_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def append_compaction(
        self,
        session_id: str,
        summary_message: Dict,
        *,
        kept_from_id: int,
    ) -> None:
        """把一次压缩的产物追加进表：`[摘要行, 边界行]`。

        只追加，一行都不删 —— 被压掉的原话
        留在库里，`history_search` 搜得回来。若走
        `replace_messages`（DELETE + INSERT 整表），原件物理消失。

        * *summary_message* —— `Compressor._summary_message` 造的
          `kind="history_summary"` 那条。
        * *kept_from_id* —— 活动窗口的第一行行号：往后（含这一行）是活的，
          之前的是"已被摘要取代"。调用方算它（`MemoryManager._kept_region_start`）。

        保留的那几轮不在这里重写一遍 —— 它们已经在库里了
        （压缩发生在落库之后：`record_turn` 先存后压、`/compact` 也发生在
        "上一轮已落库"之后），重复追加会让同一句话在
        `history_search` 里出现两次。

        `kept_from_id = 0` 是合法的（"这一条边界什么都没压掉"），
        它的效果与"没有边界行"一致 —— 见 `live_message_rows`。
        """
        boundary = {
            "role": "system",
            "kind": COMPACTION_BOUNDARY_KIND,
            "content": (
                "压缩边界：这一行之前、上一条摘要之后的轮次已被摘要取代。"
                "原文**仍在库里** —— 需要时用 history_search 按关键词找回。"
            ),
            # 数据放 `extra_json`（`_insert_message` 收非 role/content 的字段）
            #不加列、不加迁移。
            "kept_from_id": int(kept_from_id or 0),
        }
        self.save_messages(session_id, [summary_message, boundary])

    def live_message_rows(self, session_id: str) -> List[Dict]:
        """活动窗口里的原始行 —— 从最后一条边界的 `kept_from_id` 起。

        这条判据只许有一份：装载（`MemoryManager.load_session`）
        读它；`history_search` 故意不读它（它要搜的是全表，含被压掉的
        原话 —— 那正是它存在的意义）。两件事、两个函数，别合并。

        没有边界行，全表原样返回（老库 / 新会话）—— 也就是说
        空表与老库天然等于默认行为，不需要迁移。

        它总是读全表、不收窗口参数 —— 边界行有可能不落在某一页里
        （翻页是按 `id` 切的），拿"这一页里有没有边界"当判据会**静默退回
        不切片**。要分页的读者用 `load_message_rows`（那一位不吃这套）。

        返回的是 `load_message_rows` 那种带 `id` 的行。
        """
        rows = self.load_message_rows(session_id)[0]
        horizon = 0
        for r in rows:
            if r.get("kind") == COMPACTION_BOUNDARY_KIND:
                horizon = int(r.get("kept_from_id") or 0)
        if not horizon:
            return rows
        return [r for r in rows if int(r.get("id") or 0) >= horizon]

    @_serialized
    def load_messages(self, session_id: str) -> List[Dict]:
        """
        Load ALL messages for a session, ordered by insertion time.

        Returns an empty list if the session does not exist.
        """
        rows = self._conn.execute(
            "SELECT role, content, extra_json FROM messages "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()

        messages: List[Dict] = []
        for role, content, extra_json in rows:
            msg: Dict = {"role": role, "content": content}
            if extra_json:
                try:
                    extra = json.loads(extra_json)
                    msg.update(extra)
                except json.JSONDecodeError:
                    pass
            messages.append(msg)
        return messages

    #: 历史面板一页的默认 / 上限条数。
    DEFAULT_PAGE = 50
    MAX_PAGE = 200

    @_serialized
    def load_message_rows(
        self,
        session_id: str,
        *,
        before_id: int | None = None,
        limit: int | None = None,
        conversation_only: bool = False,
    ):
        """读原始行（带 `id`/`created_at`），可选"取尾部一页" → `(rows, has_more)`。

        为什么不复用 `load_messages`（两个读者、两个函数）：
          那个是给装载进上下文用的 —— 「全量 + 丢 `id`」，因为那份本来
          就不需要行号，还要原样喂给 provider。而界面要的是另外两件事：
          ① 向上翻页 ② 点条目跳回那一行，必须有 `id`。
          往 `load_messages` 的返回值里塞一个 `id` 是个陷阱：那些 dict 会被
          `_insert_message` 原样收进 `extra_json`，`id` 会变成模型看得见的一个
          字段、每轮重发（一个口径不许有两个地方）。

        游标是 `id` 一个数，不是 `(created_at, id)`：`id` 是 AUTOINCREMENT
          （严格插入序、唯一、单调），而 `created_at` 只到秒 —— 同一秒里落
          十几条是常态（含一串 tool 结果），拿它当游标会漏行或重复行。
          全仓其它读也一律 `ORDER BY id`。

        * `limit=None`，全部（"会话内搜索"要扫整个窗口）
        * `before_id`，只要更早的（向上翻页）
        * `conversation_only=True`，只给给人看的对话行
          （`role ∈ {user, assistant}` 且正文非空）—— 历史面板只显示回复，
          不显示工具调用。
          为什么筛在 SQL 里、不筛在调用方：一页 50 条原始行里可能只有
          8 条是对话行（其余是 tool 结果），筛在调用方的话，
          「加载更早」点一次几乎没动静、而 `has_more` 还是 true。
          **判据必须和 `core/history_tools.is_conversation_row` 是同一条**
          （那边是 Python 版）—— 两处漂了就会出现「列表里有、搜不到」。
          机械读者按集合相等钉着（`tests/test_history_api.py`）。
          已知的一处细微差异：SQLite 的 `TRIM` 只认下面写死的那几个空白，
          而 Python 的 `strip()` 还认全角空格一类 —— 只有"整条消息全是全角空格"
          才会露出来，那是看着像空行的东西，不打算为它加复杂度。

        返回升序（`id` 小 → 大）：SQL 取"尾部 N 条"必须 `DESC`，而界面要把
          这一页prepend 到头顶 —— 翻转在这里做一次，调用方不必再想第二遍。
        """
        sid = str(session_id or "")
        if not sid:
            return [], False
        take = None if limit is None else max(1, min(int(limit), self.MAX_PAGE))
        sql = ("SELECT id, role, content, extra_json, created_at FROM messages "
               "WHERE session_id = ?")
        args: list = [sid]
        if conversation_only:
            # 那几个字符要和 Python 的 `strip()` 对齐多少写多少 —— 见 docstring 那条。
            # 与 `history_tools.FRAMEWORK_NOTE_KINDS` 同一条判据
            #    （框架代笔的 user 行不收）：`extra_json` 里带那个 kind。
            #    用 `instr` 而不是 `json_extract` —— 不依赖 JSON1 扩展。
            sql += (" AND role IN ('user', 'assistant')"
                    " AND instr(COALESCE(extra_json, ''), 'framework_note') = 0"
                    " AND TRIM(COALESCE(content, ''),"
                    " ' ' || char(9) || char(10) || char(13)) <> ''")
        if before_id is not None:
            sql += " AND id < ?"
            args.append(int(before_id))
        sql += " ORDER BY id DESC"
        if take is not None:
            # 多取一条只为判断还有没有（`has_more`）—— 不每页 COUNT 一次全表。
            sql += " LIMIT ?"
            args.append(take + 1)
        rows = self._conn.execute(sql, tuple(args)).fetchall()
        has_more = False
        if take is not None and len(rows) > take:
            has_more = True
            rows = rows[:take]
        out: List[Dict] = []
        for mid, role, content, extra_json, created_at in reversed(rows):
            msg: Dict = {"id": mid, "role": role, "content": content,
                         "ts": created_at}
            if extra_json:
                try:
                    msg.update(json.loads(extra_json))
                except json.JSONDecodeError:
                    pass
            out.append(msg)
        return out, has_more

    @_serialized
    def thread_channel(self, storage_id: str) -> str | None:
        """这条线程属于哪个通道键（不在名册里，`None`）。

        端点用它把「只列桌面端的」从列表延伸到读：列表已经只会给出
          本通道的 id，但端点收到的是客户端传来的 id —— 不核对的话，
          那句约束只管住了那一屏、管不住那条路。
        """
        sid = str(storage_id or "")
        if not sid:
            return None
        row = self._conn.execute(
            "SELECT channel_key FROM session_threads WHERE storage_id = ?", (sid,)
        ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    # 会话的模型配置冻结
    # ------------------------------------------------------------------
    #
    # 语义：一旦一段对话已经有了模型回复，再改默认就不再对它生效 ——
    # 只影响后续新建。
    #
    # 键是存储 id，不是通道键 ——
    #   于是 `/clear`（它铸一条新存储 id）天然拿到新默认；
    #   而 `/resume` 回一条旧线程时，那条线程冻的那个配置还在。
    #   这正好是同一条判据的两面：冻结跟着"这一段对话"走。
    #
    # 与 `session_bindings` 的关系：那一张是「通道 → 现在在哪条线程」，
    #   这一张是「那条线程用的是哪个模型配置」。两张表、两个问题，
    #   合并的那一刻"当前指针"与"这段对话的模型"就变成同一件事了。
    #
    # 这块不进 `/clear` 的 purge 清单（`core/state_registry.py`）——
    #   它不是内存态，它就是这条线程自己的一行；`/clear` 换的是线程，
    #   线程一换，新线程那一格本来就不存在（= 「跟随默认」）。

    @_serialized
    def set_session_model(self, storage_id: str, config_id: str) -> None:
        """给这条线程钉上它的模型配置。幂等（同一个 id 重复写没有副作用）——
        调用方（轮末的惰性冻结）本来就可能在每条轮都问一次，它自己先判过了。"""
        sid = str(storage_id or "")
        cid = str(config_id or "")
        if not sid or not cid:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO session_models "
            "(storage_id, config_id, frozen_at) VALUES (?, ?, ?)",
            (sid, cid, _now()),
        )
        self._conn.commit()

    @_serialized
    def session_model(self, storage_id: str) -> str:
        """这条线程冻的是哪条配置。没冻过，`""`（= 跟随默认）。

        `""` 不是"没有模型" —— 它是「还没冻」，下游据此走默认那条。
          两者在调用方长得一样，而它们要做的事正好一致（用默认），
          所以这里刻意不造一个哨兵值（同 `model_configs.default_id` 那条）。
        """
        sid = str(storage_id or "")
        if not sid:
            return ""
        row = self._conn.execute(
            "SELECT config_id FROM session_models WHERE storage_id = ?", (sid,)
        ).fetchone()
        return str(row[0]) if row and row[0] else ""

    @_serialized
    def clear_session_model(self, storage_id: str) -> None:
        """把这条线程退回「跟随默认」（用户显式点「跟随默认」时走它）。

        与 `set_session_model` 不是一对反操作：一个写、一个删。
          它们分两个函数是因为两个都是用户能做主的动作，
          而"写空串"这种写法会让 `session_model()` 的读法分叉
          （`""` 到底是"没冻"还是"冻了个空的"？—— 一个值只能说一件事）。
        """
        try:
            self._conn.execute("DELETE FROM session_models WHERE storage_id = ?",
                               (str(storage_id or ""),))
            self._conn.commit()
        except Exception as exc:                                    # noqa: BLE001
            print(f"[memory] 清会话模型失败（{type(exc).__name__}: {exc}）")

    # ------------------------------------------------------------------
    # 会话 -> 工作目录（ ）
    # ------------------------------------------------------------------
    #
    # 键是存储 id，不是通道键 —— 与 `session_models` 同一条判据：
    #   `/clear`（铸一条新存储 id）天然拿到默认工作区；
    #   `/resume` 回一条旧线程时，那条线程选的那个目录还在。
    #「这一段对话的工作目录」跟着这一段对话走，不需要在
    #   `/clear` 的 purge 清单里加任何一行（它不是内存态，是线程自己的一行）。
    #
    # 与 `session_bindings` 的关系：那一张是「通道 → 现在在哪条线程」，
    #   这一张是「那条线程的工作目录是哪个」。两张表、两个问题。
    #
    # 它不承担权限语义：这一格只决定
    #   「产物往哪儿落 / 左栏按什么分组 / 提示词里那句语境」。
    #   "能不能碰别的目录"是审批轴的事（`core/security.py` 那两道门），
    #这一格里写进一个目录不等于给那个目录开了权限
    #   （唯一例外是 `classify_path` 那条显式旁路，见 `_is_session_workspace`）。

    @_serialized
    def set_session_workspace(self, storage_id: str, path: str) -> None:
        """给这条线程钉上它的工作目录。幂等（重复写同一个没有副作用）。"""
        sid = str(storage_id or "")
        p = str(path or "")
        if not sid or not p:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO session_workspaces "
            "(storage_id, path, set_at) VALUES (?, ?, ?)",
            (sid, p, _now()),
        )
        self._conn.commit()

    @_serialized
    def session_workspace(self, storage_id: str) -> str:
        """这条线程选的工作目录。没选过，`""`（= 用默认工作区）。

        `""` 不是"没有目录" —— 它是「还没选」，下游据此走默认那个。
          两者在调用方长得一样，而它们要做的事正好一致，所以这里刻意
          不造哨兵值（同 `session_model` 那条）。
        """
        sid = str(storage_id or "")
        if not sid:
            return ""
        row = self._conn.execute(
            "SELECT path FROM session_workspaces WHERE storage_id = ?", (sid,)
        ).fetchone()
        return str(row[0]) if row and row[0] else ""

    @_serialized
    def clear_session_workspace(self, storage_id: str) -> None:
        """把这条线程退回「用默认工作区」（用户点「用默认」时走它）。

        与 `set_session_workspace` 不是一对反操作：一个写、一个删
          —— 同 `clear_session_model` 那条（一个值只能说一件事）。
        """
        try:
            self._conn.execute(
                "DELETE FROM session_workspaces WHERE storage_id = ?",
                (str(storage_id or ""),))
            self._conn.commit()
        except Exception as exc:                                    # noqa: BLE001
            print(f"[memory] 清会话工作目录失败（{type(exc).__name__}: {exc}）")

    @_serialized
    def first_user_text(self, session_id: str, limit: int = 40) -> str:
        """这段对话开头那句（会话列表里当标题用）。

        与 `preview`（最近一条）不是一回事，而且两个都要：
          · `preview` = 「我上次聊到哪儿了」（IM 选址列表用）
          · 本条     = 「这条对话是关于什么的」（历史列表用）
          拿最近一条当标题，会在用户中途换话题时把整条对话标错名
          （ChatGPT 那类列表取的是第一句，正是这个理由）。
        拿不到就给空串 —— 调用方负责回退（同 `preview` 的契约）。
        """
        try:
            row = self._conn.execute(
                "SELECT content FROM messages WHERE session_id = ? "
                "AND role = 'user' AND COALESCE(content, '') <> '' "
                "ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()
        except Exception:
            return ""
        if not row or not row[0]:
            return ""
        raw = str(row[0])
        #: `limit=None` = 不截断、也不折叠换行 —— 给"先判内容、再截断"的
        #:   调用方（会话标题要先把附件标记整行剔掉：指针那一行本身就有 45+ 字符，
        #:   先截再判会被切成半截，认不出来）。
        if limit is None:
            return raw
        text = " ".join(raw.split())
        return text[:limit] + ("…" if len(text) > limit else "")

    # ------------------------------------------------------------------
    # 收藏夹（ ）
    # ------------------------------------------------------------------

    @_serialized
    def anchor_of(self, message_id: int) -> Dict:
        """那一轮的起点挂在哪 —— `{"ok", "why", "storage_id"}`。只读。

        这是唯一一处"什么算合法锚点"的判据：`star_turn` 调它，
          端点在写之前也调它（先校验、后动笔，且校验只有一份 ——
          抄第二份的代价不是多写六行，是两份会漂）。

        合法锚点 = 那一行存在、且 `role='user'`：
          · 存在 —— 引用式的前提。解不出来的引用压根不该写进去。
          · `role='user'` —— 一轮的开头就是那条提问。
            存成别的角色，`list_stars` 那个 `JOIN` 永远解不出"上半问"，
            条目会静默地变成一条空壳，当场拒
            （一处坏就全拒，不许静默少一条）。

        `storage_id` 由本函数解析，不由调用方传 —— 那一行属于哪段会话
          是库自己知道的事实，让客户端传一遍 ＝ 多一个能说谎的来源
          （也与"聊天区根本不知道自己的 storage_id"对不上）。
        """
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return {"ok": False, "why": "message_id 要是整数行号", "storage_id": ""}
        row = self._conn.execute(
            "SELECT session_id, role FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            return {"ok": False,
                    "why": "这条消息不在库里（行号 %d）。" % mid,
                    "storage_id": ""}
        if str(row[1]) != "user":
            # 话术要说怎么才对，不能只说"不行"。
            return {"ok": False,
                    "why": "星标的锚点必须是那一轮的**提问**（role=user），"
                           "这一条是 role=%s。" % row[1],
                    "storage_id": ""}
        return {"ok": True, "why": "", "storage_id": str(row[0])}

    @_serialized
    def star_turn(self, message_id: int) -> Dict:
        """给一轮打星 —— 锚点是那一轮的起点（`role='user'` 的那条）。

        → `{"ok": bool, "why": str, "storage_id": str}`。幂等：已经在
          收藏里了也算成功 —— 重复点一下 不该报错，那只是"它本来就在里面"。

        写之前必须已经过了归属核对 —— 本函数不管通道（
          那是 `web/server.py::_desktop_thread` 的事）。直接用它的调用方
          要自己保证这一点。
        """
        a = self.anchor_of(message_id)
        if not a["ok"]:
            return a
        mid = int(message_id)
        self._conn.execute(
            "INSERT INTO stars (message_id, storage_id, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(message_id) DO NOTHING",
            (mid, a["storage_id"], _now()),
        )
        self._conn.commit()
        return {"ok": True, "why": "", "storage_id": a["storage_id"]}

    @_serialized
    def unstar_turn(self, message_id: int) -> bool:
        """取消一颗星。→ `True` = 真删掉了一条 / `False` = 本来就没有。

        不校验归属、也不校验角色（与 `star_turn` 有意不对称）：
          取消是幂等且无害的 —— 删一个不存在的行不改变任何状态。
          而"谁也别想取消别人的星"不归这一层管（端点上 `_desktop_thread`
          已经只放行本通道的会话）。
        """
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return False
        cur = self._conn.execute("DELETE FROM stars WHERE message_id = ?", (mid,))
        self._conn.commit()
        return bool(cur.rowcount)

    @_serialized
    def star_storage_id(self, message_id: int) -> Optional[str]:
        """一颗星挂在哪一段会话上（没这颗星，`None`）。

        端点用它把归属核一遍再删：`/api/stars/delete` 收到的是
          客户端传来的 `message_id` —— 不核对的话，「只动本通道的东西」
          就只管住了列表那一屏、管不住这条写的路（`_desktop_thread` 的注释
          里那条判据，`/api/messages` 与 `/api/search` 都照它办）。
        """
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return None
        row = self._conn.execute(
            "SELECT storage_id FROM stars WHERE message_id = ?", (mid,)
        ).fetchone()
        return str(row[0]) if row else None

    @_serialized
    def starred_ids(self, storage_id: str) -> List[int]:
        """这一段会话里被星标过的起点行号（升序）—— 界面拿它给行标 。

        只查 `stars`，不 join —— 这里回答的是「哪些标过」，
          而"解不解得出源"是 `list_stars` 的事。两件事、两个函数
          （合并的话，这里会跟着一起把"源没了"的行藏掉，而界面需要知道
           *这一行的星还在*，才好让 保持点亮、点一下能取消）。
        """
        sid = str(storage_id or "")
        if not sid:
            return []
        rows = self._conn.execute(
            "SELECT message_id FROM stars WHERE storage_id = ? ORDER BY message_id",
            (sid,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _stars_sql(self) -> str:
        """`list_stars` / `count_stars` 共用的 `FROM` 那一段。

        合在一处是判据：两份写法会漂，而**漂了以后
          两个答案看着都对**（"总数 3、列表 2 条"看起来像分页）。
        """
        return (
            "FROM stars s "
            "JOIN messages m ON m.id = s.message_id "
            "JOIN session_threads t ON t.storage_id = s.storage_id "
            "WHERE t.channel_key = ? AND m.role = 'user'"
        )

    @_serialized
    def count_stars(self, channel_key: str) -> int:
        """这个通道能解析出来的收藏条数（列表可能被 `limit` 截断，它不会）。"""
        key = str(channel_key or "")
        if not key:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) " + self._stars_sql(), (key,)
        ).fetchone()
        return int(row[0] or 0) if row else 0

    @_serialized
    def list_stars(self, channel_key: str, limit: int = 100) -> List[Dict]:
        """这个通道的收藏条目，新 → 旧（按起点行号降序）。每条一个 dict。

        两边都是 `JOIN`，所以"源没了就不显示"是白拿的：
          起点行被删，`JOIN messages` 落空，条目根本不会出现在结果里。
          这里没有 `LEFT JOIN`、也没有"解不出来就填空串"那种兜底
             —— 加一个 `LEFT JOIN` 就等于把"源没了"变成一条空壳条目，
             而那正是要它消失的东西。

        ✅ 压缩不影响它 —— 压缩只追加（摘要 + 边界，
        见 `append_compaction`），行号一行都不动，星标照旧解得出来。
        （若压缩改成整表重写，行号会全部换新，压缩之前打的星解不出来而消失。）
        仍然会消失的只有一种：源行真的被删了（那正是 `JOIN` 要表达的）。

        每条：
          · `message_id` / `storage_id` —— 跳转的抓手（`/api/messages` 认它）
          · `starred_at`  —— 打星那一刻（不是对话发生的时间）
          · `ts`          —— 那一轮提问的时间（列表上显示这个）
          · `question`    —— 上半
          · `answer`      —— 下半；解不出来给 `None`，不给空串
            （读不到 ≠ 空。界面据此说"找不到回复"，而不是
             显示一片空白让人以为那条回复是空的）
        """
        key = str(channel_key or "")
        if not key:
            return []
        # 下半：那一轮起点之后第一条非空 assistant 行。
        # 用 `LIMIT 1` 的相关子查询而不是 Python 里扫 —— 收藏是低频读，
        #   但"每轮对话都可能是几十行 tool 结果"，在 SQL 里定序更省也更好读。
        rows = self._conn.execute(
            "SELECT s.message_id, s.storage_id, s.created_at, "
            "       m.content, m.created_at, "
            "       (SELECT a.content FROM messages a "
            "         WHERE a.session_id = s.storage_id AND a.id > s.message_id "
            "           AND a.role = 'assistant' "
            "           AND COALESCE(a.content, '') <> '' "
            "         ORDER BY a.id ASC LIMIT 1) "
            + self._stars_sql() +
            " ORDER BY s.message_id DESC LIMIT ?",
            (key, max(1, int(limit))),
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "message_id": int(r[0]),
                "storage_id": r[1],
                "starred_at": r[2],
                "question": str(r[3] or ""),
                "ts": r[4],
                "answer": (None if r[5] is None else str(r[5])),
            })
        return out

    #: 存史内容的上限（防止越留越大，超限后淘汰旧的）。
    DEFAULT_MAX_CONTENT_BYTES = 500 * 1024 * 1024

    #: 淘汰到上限的几成（低水位）。不留一点余量的话，删到刚好等于上限，
    #: 下一轮只要再落一条就又超，每轮都触发一次全表扫描 + 删除。
    PRUNE_LOW_WATER = 0.8

    @_serialized
    def content_bytes(self, session_id: str | None = None) -> int:
        """存史内容的字节数（`SUM(LENGTH(content)+LENGTH(extra_json))`）。

        为什么不用"文件大小"（这一格最容易做错，两条独立理由）：
          ① 库是 WAL 模式（`__init__` 里 `PRAGMA journal_mode=WAL`）——
             刚提交还没 checkpoint 的那一截在 `-wal` 里、`.db` 本体量不到；
          ② 删行不会让文件变小（要 `VACUUM` 才回收），拿文件大小当触发条件，
             会每一轮都超限、每一轮都白删一批，而且看起来"上限生效了"。
        所以这个数是「内容的上限」，不是「磁盘占用的上限」—— 两者别读成一个。
        """
        sid = str(session_id or "")
        sql = ("SELECT COALESCE(SUM(LENGTH(COALESCE(content,'')) "
               "+ LENGTH(COALESCE(extra_json,''))), 0) FROM messages")
        if sid:
            row = self._conn.execute(sql + " WHERE session_id = ?", (sid,)).fetchone()
        else:
            row = self._conn.execute(sql).fetchone()
        return int(row[0] or 0) if row else 0

    @_serialized
    def prune_old_tool_messages(self, *, max_bytes: int | None = None,
                                log=print) -> Dict:
        """超上限，从最旧开始整组删 `role='tool'` 的行。→ 读数 dict。

        只删 `role='tool'` —— `user` / `assistant`
           那些行一条都不许动。用户自己要的那句话、模型给过的回答，是劳动成果。

        整组删：「一组」= 行序上连续的一段 `role='tool'`，也就是一条
           assistant 的 `tool_calls` 带出来的那一串结果。只删一半会留下
           没有对应 tool_call 的孤儿 tool 结果，而孤儿 tool 结果是明写的上游 400。
        分组按行序切，不去解析 `extra_json` 里的 `tool_calls` —— 工具结果落库时
           本来就紧跟在那条 assistant 后面，行序就是分组依据；再去解析 JSON 等于
           给同一件事立第二份真相源。

        **删掉 tool 结果会不会让那条 assistant 的 `tool_calls` 悬空？** 不会：
           装载那条路（`load_session`）**只认 `user`/`assistant`，且只取
           `role`+`content`**（`manager.py:574-586`），重启/重载时 `tool_calls`
           本来就被丢掉。活会话里更没关系 —— 送上游的是内存里那份
           `messages`，删库不碰它。

        永不抛给调用方去处理外的东西：坏掉就原样返回读数（调用方还会再包一层）。
        """
        cap = self.DEFAULT_MAX_CONTENT_BYTES if max_bytes is None else int(max_bytes)
        before = self.content_bytes()
        out: Dict = {"before": before, "cap": cap, "after": before, "target": 0,
                     "groups": 0, "rows": 0, "freed": 0, "pruned": False}
        if cap <= 0 or before <= cap:
            return out                      # 没超，一个字节不碰

        target = int(cap * self.PRUNE_LOW_WATER)
        out["target"] = target
        need = before - target

        # 从最旧那一头走，够了就停。必须在"组边界"上停 ——
        # 在组中间 `break` 会删掉半组，那正是上面说的孤儿来源。
        picked: List[List[int]] = []
        freed = 0
        run: List[int] | None = None
        stop = False
        rows = self._conn.execute(
            "SELECT id, role, LENGTH(COALESCE(content,'')) "
            "+ LENGTH(COALESCE(extra_json,'')) FROM messages ORDER BY id")
        for mid, role, nbytes in rows:
            if role != "tool":
                run = None
                if stop:
                    break                   # ← 上一组已经收完，可以停了
                continue
            if run is None:
                run = []
                picked.append(run)
            run.append(int(mid))
            freed += int(nbytes or 0)
            if freed >= need:
                stop = True                 # 先记下"够了"，等这组走完再停

        ids = [i for g in picked for i in g]
        if not ids:
            return out                      # 超了但没有 tool 行可删 —— 如实返回

        try:
            # 一个事务：删一半崩掉会留下孤儿。
            with self._conn:
                self._conn.executemany(
                    "DELETE FROM messages WHERE id = ?", [(i,) for i in ids])
        except Exception as exc:            # noqa: BLE001
            log("[memory] tool 淘汰失败（本轮不动它）：%s: %s"
                % (type(exc).__name__, exc))
            return out

        out.update({"after": self.content_bytes(), "groups": len(picked),
                    "rows": len(ids), "freed": freed, "pruned": True})
        log("[memory] tool 淘汰：删了 %d 组 / %d 行 / %.0f KB"
            "（内容 %.1f MB -> %.1f MB，上限 %.0f MB）"
            % (out["groups"], out["rows"], freed / 1024.0,
               before / 1048576.0, out["after"] / 1048576.0, cap / 1048576.0))
        return out

    @_serialized
    def preview(self, session_id: str, limit: int = 24) -> str:
        """一句话摘要：最近一条非空 user 消息的前 *limit* 字（选址列表用）。

        拿不到就给空串 —— 调用方（`SessionPool.session_preview`）负责回退到键名。
        """
        try:
            row = self._conn.execute(
                "SELECT content FROM messages WHERE session_id = ? "
                "AND role = 'user' AND COALESCE(content, '') <> '' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        except Exception:
            return ""
        if not row or not row[0]:
            return ""
        text = " ".join(str(row[0]).split())
        return text[:limit] + ("…" if len(text) > limit else "")

    @_serialized
    def last_message_at(self, session_id: str) -> str | None:
        """这条线程最后一条消息的时间戳；一条都没有，`None`。

        「回到当前对话」那个按钮要判活跃（窗口 30 分钟），
        而名册里那两个时间字段都不回答这件事 —— `started_at` 是
        铸出来的时刻，`ended_at` 是被下一段顶掉的时刻，只有这个数说的是
        「最近一次说话是什么时候」。

        空线程给 `None`，不给 `""`、不给 `0`（读不到 ≠ 0）——
        铸出来还没说过话的那条本来就不算活跃，界面据此不显示那个按钮。
        """
        sid = str(session_id or "")
        if not sid:
            return None
        row = self._conn.execute(
            "SELECT MAX(created_at) FROM messages WHERE session_id = ?",
            (sid,),
        ).fetchone()
        return (row[0] or None) if row else None

    @_serialized
    def count_messages(self, session_id: str) -> int:
        """Return the number of messages stored for a session (fast COUNT)."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    @_serialized
    def get_token_total(self, session_id: str) -> int:
        """Return the sum of token_count for a session."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(token_count), 0) FROM messages "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row else 0

    @_serialized
    def session_exists(self, session_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @_serialized
    def _create_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                extra_json TEXT,
                token_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session "
            "ON messages(session_id, id)"
        )
        # 通道键 -> 当前存储 id 的绑定。
        # 空表 = 默认行为（每个通道键都指向自己），所以它是加性的。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_bindings (
                channel_key TEXT PRIMARY KEY,
                storage_id  TEXT NOT NULL,
                bound_at    TEXT NOT NULL
            )
        """)
        # 线程名册 —— 上面那张是"当前是哪条"，
        # 这张是"历史上有哪些"。两张不许合并（见 `open_thread` 上方的注释）。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_threads (
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                storage_id  TEXT NOT NULL UNIQUE,
                channel_key TEXT NOT NULL,
                started_at  TEXT NOT NULL,
                ended_at    TEXT,
                turn_count  INTEGER DEFAULT 0
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_threads_channel "
            "ON session_threads(channel_key, seq)"
        )
        # 收藏夹（ ）。
        #
        # 一条星标 = 一轮（不是一条消息）—— `message_id` 是那一轮的起点，
        # 也就是那条 `role='user'` 的行。
        #
        # 引用式，不存副本 —— 「来源被删，条目消失」是一条
        #   结构要求而不是提示：列表走 `JOIN messages`，源没了自然 join 不到、
        #   一行级联代码都不用写。
        # 不进 `/clear` 的 purge 清单：收藏是跨会话的东西，不许被
        #   单会话的 `/clear` 清掉。
        # ✅ 压缩也不换行号：压缩改成"追加摘要 + 追加边界"
        #   （`append_compaction`），被压掉的原话一行不删，行号稳定，星标
        #   一直解得出来。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS stars (
                message_id INTEGER PRIMARY KEY,
                storage_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stars_storage ON stars(storage_id)"
        )
        # 会话的模型配置冻结 ——
        # 形状照 `stars`（一行、按存储 id 键、不进 `/clear` 的 purge 清单），
        # 理由与访问器一起写在 `set_session_model` 那一段注释里。
        # 一个加性的表：空表 == 默认行为（每一段对话都跟随默认）。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_models (
                storage_id TEXT PRIMARY KEY,
                config_id  TEXT NOT NULL,
                frozen_at  TEXT NOT NULL
            )
        """)
        # 会话的工作目录 ——
        # 形状逐格照 `session_models`（一行、按存储 id 键、加性表、
        # 不进 `/clear` 的 purge 清单），理由见 `set_session_workspace`。
        # 一个加性的表：空表 == 默认行为（每一段都落在默认工作区）。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS session_workspaces (
                storage_id TEXT PRIMARY KEY,
                path       TEXT NOT NULL,
                set_at     TEXT NOT NULL
            )
        """)
        # 置顶（对话右键「置顶此对话/此集」）——
        # 一张表装两种东西（`kind` 分开），不是两张：它们除了键的来源以外
        # 行为一模一样（排序时排前面、可取消、不参与 `/clear` purge），
        # 而分成两张就要把"排序靠前"那条判据抄第二遍。
        #   · `kind='thread'`，`key` = `storage_id`（那一段对话）
        #   · `kind='group'`，`key` = 目录路径（左栏那一"集"）
        # 一个加性的表：空表 == 默认行为（谁都不置顶，按原来的顺序）。
        # `at` 是"先置顶的排在前面"的排序键（不是时间戳给人看的，
        #   所以用单调递增的 epoch 秒就够，`seq` 那种自增会让"重排"变麻烦）。
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                kind TEXT NOT NULL,
                key  TEXT NOT NULL,
                at   REAL NOT NULL,
                PRIMARY KEY (kind, key)
            )
        """)
        self._conn.commit()

    @_serialized
    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def __del__(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
