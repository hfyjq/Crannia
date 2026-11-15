"""
Document session management for office write operations.

A draft-then-finalize state machine with idle eviction: a document is
created as a draft, blocks are appended to it, then it is finalized
(locked, no more edits) and exported to a file. Idle sessions expire so
a long-running process does not accumulate them; a hard cap bounds how
many can exist at once.

State transitions::

    create_document  →  DRAFT
    add_block/slide/write_sheet  →  DRAFT (append to blocks[])
    finalize_document  →  FINALIZED (lock, no more edits)
    export_document    →  EXPORTED (render to file)
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ------------------------------------------------------------------
# Document model
# ------------------------------------------------------------------

@dataclass
class DocSession:
    """In-memory representation of a document under construction."""

    session_id: str
    doc_type: str  # "word" | "ppt" | "excel"
    status: str = "DRAFT"  # DRAFT | FINALIZED | EXPORTED

    # Word/PPT: accumulated content blocks
    blocks: List[Dict[str, Any]] = field(default_factory=list)

    #: `add_block` 的 20 块上限从这里往上数。
    #:
    #: 那个上限的本意是"防交互式无限追加"，而 `create_document` 的整篇写入
    #: 是另一次操作（内容长度本来就受模型的输出限制）。若把整篇写入也数进去，
    #: 模型一次写 30 块之后，第一次调 `add_block` 就会被拒
    #: —— `{"error": "MAXIMUM 20 blocks reached."}`，只能自己去写 python-docx。
    #:记一个基线，只数基线之后的追加。
    append_base: int = 0

    # Excel: sheet-based storage
    sheets: Dict[str, List[List[Any]]] = field(default_factory=dict)

    # Metadata
    title: str = ""
    output_path: str = ""
    created_at: float = field(default_factory=time.monotonic)
    last_access: float = field(default_factory=time.monotonic)

    # v0.4.0: PPT SVG generation fields (only populated for doc_type="ppt_svg")
    spec_lock: Dict[str, Any] = field(default_factory=dict)
    # spec_lock structure:
    # {
    #     "colors": {"primary": str, "secondary": str, "accent": str,
    #                "background": str, "text": str, "text_light": str},
    #     "fonts": {"title": str, "body": str, "title_size": str, "body_size": str},
    #     "layout": {"margins": str, "title_area": str, "content_flow": str},
    # }

    svg_files: Dict[int, str] = field(default_factory=dict)
    # Maps page_number → svg_file_path. Ordered by page_number for export.

    svg_dir: str = ""
    # Path to the temp directory holding SVG files for this session.
    # Created under DocSessionStore.output_dir / "svg_sessions" / session_id.

    canvas_format: str = "ppt169"
    # "ppt169" | "ppt43" | "ppt1610" — slide dimensions for SVG viewBox.

    segment_history: List[Dict[str, Any]] = field(default_factory=list)
    # Structured summary of previous generation segments for tiered context.
    # Each entry: {"segment": int, "pages": "3-5", "slides": [{"page": N, "fingerprint": str}, ...]}

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def is_expired(self, ttl_seconds: float) -> bool:
        return (time.monotonic() - self.last_access) > ttl_seconds


# ------------------------------------------------------------------
# Session store
# ------------------------------------------------------------------

class DocSessionStore:
    """Thread-safe in-memory store for document sessions.

    Args:
        ttl_seconds: How long an idle session lives before eviction
                     (default 30 min).
        max_sessions: Maximum number of concurrent sessions (default 50).
        output_dir: Where exported files are written.
    """

    def __init__(
        self,
        ttl_seconds: float = 1800.0,
        max_sessions: int = 50,
        # `None`，走唯一落点口径（`primary_output_dir()`）。
        # 不许写成默认参数 `= primary_output_dir()`：默认值在导入时求值，
        # 那一刻工作组还没装载，会永远钉死在 `./workspace`（正是这条要治的病）。
        output_dir: "str | None" = None,
    ) -> None:
        from core.workspace import primary_output_dir
        self._ttl = ttl_seconds
        self._max = max_sessions
        self._output_dir = Path(output_dir) if output_dir else primary_output_dir()
        self._lock = threading.RLock()
        self._sessions: Dict[str, DocSession] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(
        self,
        doc_type: str,
        title: str = "",
    ) -> DocSession:
        """Create a new document session and return it."""
        with self._lock:
            self._evict_expired()

            # Evict oldest if at capacity
            while len(self._sessions) >= self._max:
                oldest_key = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].last_access,
                )
                del self._sessions[oldest_key]

            sid = f"doc_{uuid.uuid4().hex[:12]}"
            session = DocSession(
                session_id=sid,
                doc_type=doc_type,
                title=title,
            )
            self._sessions[sid] = session
            return session

    def get(self, session_id: str) -> Optional[DocSession]:
        """Retrieve a session by ID or by its file path, or None.

        文件路径也是合法句柄 —— 缺了这一条，`add_block` 的 20 块上限
        就从未生效过（模型拿回来的路径查不到 session）。

        链路：`create_document("word")` 建 session 时用 `store.create()` 铸的内部
        键（`doc_<hex>`），而它返回给模型的是文件路径
        （`handler.py` 的 `"session_id": str(out_path)`，注释逐字写着
        "file path IS the session_id"）。模型下一轮拿这个路径回来查，
        `self._sessions.get("<workspace>/_wip_x.docx")` 必然落空。

        后果（两处，都在 `add_block`）：
          · `session is None`，块计数恒 0，回执里永远是 `Block 0/20 added`
          · 同一个 `session is None`，那个 20 块上限从来没拦过任何东西

        修法不新建索引：直接扫 `output_path` 比对。理由是索引要在
        `create()` 之后再写一次（`output_path` 是调用方随后赋的），
        而那正是"两张表会漂移"的形状 —— 扫描最多 50 条，只在字典未命中时发生。
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._by_output_path(session_id)
            if session is None:
                return None
            if session.is_expired(self._ttl):
                del self._sessions[session.session_id]
                return None
            session.touch()
            return session

    def _by_output_path(self, handle: str) -> Optional[DocSession]:
        """按 `output_path` 反查会话（调用方须已持锁）。

        先比字符串（快路径，也是绝大多数情况：两边都是 `store.output_dir` 拼出来的
        那个相对路径）；不一致再 `resolve()` 比 —— 绝对/相对两种写法都要认，
        否则"同一份文件"会因为写法不同而查到两个答案。
        """
        if not handle:
            return None
        for session in self._sessions.values():
            op = session.output_path
            if not op:
                continue
            if op == handle:
                return session
            try:
                if Path(op).resolve() == Path(handle).resolve():
                    return session
            except OSError:
                continue
        return None

    def finalize(self, session_id: str) -> bool:
        """Lock a session's status to FINALIZED. Returns False if not found."""
        session = self.get(session_id)
        if session is None:
            return False
        if session.status != "DRAFT":
            return False
        session.status = "FINALIZED"
        return True

    def remove(self, session_id: str) -> None:
        """Remove a session and clean up its SVG temp directory if present."""
        import shutil
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session and session.svg_dir:
            svg_dir = Path(session.svg_dir)
            if svg_dir.exists():
                shutil.rmtree(str(svg_dir), ignore_errors=True)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def active_count(self) -> int:
        with self._lock:
            self._evict_expired()
            return len(self._sessions)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evict_expired(self) -> None:
        """Remove all expired sessions. Must be called under lock."""
        import shutil
        expired = [
            k for k, s in self._sessions.items()
            if s.is_expired(self._ttl)
        ]
        for k in expired:
            session = self._sessions.pop(k)
            # Clean up SVG temp directory if present
            if session.svg_dir:
                svg_dir = Path(session.svg_dir)
                if svg_dir.exists():
                    shutil.rmtree(str(svg_dir), ignore_errors=True)


# ------------------------------------------------------------------
# Global singleton (initialized by handler.init)
# ------------------------------------------------------------------

_store: Optional[DocSessionStore] = None


def get_store() -> DocSessionStore:
    """Return the global DocSessionStore singleton."""
    global _store
    if _store is None:
        _store = DocSessionStore()
    return _store


def reset_store() -> None:
    """Reset the global store (testing only)."""
    global _store
    _store = None
