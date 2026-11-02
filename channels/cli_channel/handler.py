"""
CLI channel: interactive input loop implementing the Channel ABC.

Session state is managed by the Pipeline (PreProcess → AgentLoop →
PostProcess).  The CLI channel only handles I/O: read input, display
output, show stats.

v0.7.0: uses `CLIMessageConverter` to translate between raw `str`
input and `MessageChain`.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from channels.base import (
    Channel,
    ChannelMetadata,
    Message,
    MessageEvent,
    MessageSession,
    MessageType,
)
from channels.converter import MessageConverter
from core.session_key import session_key_for
from channels.message import MessageChain


# ------------------------------------------------------------------
# Input — plain `input()` with post-turn console drain on Windows
# ------------------------------------------------------------------

def _drain_console() -> None:
    """Discard any lingering console-input characters between turns."""
    if os.name != "nt":
        return
    import msvcrt

    count = 0
    while msvcrt.kbhit():
        msvcrt.getwch()
        count += 1
        if count > 50_000:
            break

    if count > 10:
        print(
            f"\n[提示] 检测到粘贴残留（{count} 字符已丢弃）。\n"
            f"[提示] 分析代码 / 长文本请先保存为文件，再用 "
            f"\"解析 文件名\" 加载。"
        )


WELCOME_BANNER = r"""
╔══════════════════════════════════════════════════════════╗
║        Crannia v2  —  Document Analysis Agent           ║
║                                                          ║
║  Examples:                                               ║
║    解析 te1.txt                                           ║
║    总结这个文档                                             ║
║    搜索 XXX 相关的内容                                      ║
║    总结 workspace/report.pdf                        ║
║                                                          ║
║  Commands:  /clear  /compact  /exit  /quit               ║
╚══════════════════════════════════════════════════════════╝
"""


# ------------------------------------------------------------------
# Context-usage display helpers
# ------------------------------------------------------------------

_BAR_WIDTH = 20
_WARN_PCT = 70
_HIGH_PCT = 85


def _format_tokens(n: int) -> str:
    """Pretty-print token count (e.g. 15420 → '15.4K')."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _render_usage_bar(pct: float, threshold_pct: float) -> str:
    """Draw a 20-char context-usage bar."""
    filled = int(pct / 100 * _BAR_WIDTH)
    filled = max(0, min(_BAR_WIDTH, filled))
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)

    if pct >= threshold_pct:
        return f"{bar} 🔴 COMPRESSING"
    elif pct >= _HIGH_PCT:
        return f"{bar} 🔴"
    elif pct >= _WARN_PCT:
        return f"{bar} ⚠"
    else:
        return bar


def _display_context_usage(pool, session_key, messages) -> None:
    """Print a one-line context-usage summary after each turn."""
    if pool is None:
        return

    usage = pool.get_context_usage(session_key, messages)
    pct = usage["percentage"]
    est = usage["estimated_tokens"]
    win = usage["context_window"]
    threshold_pct = usage["threshold_pct"]

    bar = _render_usage_bar(pct, threshold_pct)
    print(
        f"\n  📊 context: {pct}% {bar}  "
        f"{_format_tokens(est)}/{_format_tokens(win)} tokens  "
        f"(threshold: {threshold_pct}%)"
    )


def _display_compression(stats) -> None:
    """Print a one-line compression summary."""
    if stats is None:
        return

    source_label = (
        "session note" if stats.get("source") == "session_note" else "LLM"
    )
    print(
        f"  ⚡ compressed: {stats['compressed_turns']} turns → summary "
        f"({stats['summary_chars']} chars) [{source_label}], "
        f"{stats['kept_turns']} turns kept  "
        f"({stats['old_message_count']} → {stats['new_message_count']} messages)"
    )


# ------------------------------------------------------------------
# CLIMessageConverter
# ------------------------------------------------------------------

class CLIMessageConverter(MessageConverter):
    """CLI converter: `str` ↔ `MessageChain`.

    The CLI has the simplest possible message format — raw text from
    `input()`.  `to_internal` wraps it in a `Plain` component;
    `to_platform` extracts the text back out.
    """

    def to_internal(self, platform_message: str) -> MessageChain:
        """*platform_message* is the raw text from `input()`."""
        text = platform_message.strip() if isinstance(platform_message, str) else str(platform_message)
        return MessageChain.from_text(text)

    def to_platform(self, message_chain: MessageChain, **kwargs: Any) -> str:
        """Return the plain-text content (for printing to stdout)."""
        return message_chain.content


# ------------------------------------------------------------------
# CLIChannel
# ------------------------------------------------------------------

class CLIChannel(Channel):
    """Interactive command-line channel.

    Directly awaits the pipeline scheduler for each turn (unlike IM
    channels which use the event-handler pattern).  This keeps the
    synchronous-readline CLI simple.
    """

    def __init__(
        self,
        config: dict | None = None,
        scheduler=None,
        session_pool=None,
    ) -> None:
        super().__init__(config)
        self._converter = CLIMessageConverter()
        self._scheduler = scheduler
        self._pool = session_pool
        self._running = False

    # ---- Channel ABC ----

    def meta(self) -> ChannelMetadata:
        return ChannelMetadata(
            name="cli_channel",
            description="Interactive CLI for document analysis",
            platform_type="cli",
        )

    async def run(self) -> None:
        print(WELCOME_BANNER)
        self._running = True

        session = MessageSession("cli", MessageType.PRIVATE, "local")
        # 会话键由统一推导给出（cli 的私聊 = 工作区那条会话）
        session_key = session_key_for(session)

        # Restore previous session on startup
        if self._pool is not None:
            sid, msgs = self._pool.get_or_create(session_key)
            usage = self._pool.get_context_usage(session_key, msgs)
            if msgs:
                print(
                    f"\n  📋 Restored session: {sid}\n"
                    f"     {len(msgs)} messages  |  "
                    f"{_format_tokens(usage['estimated_tokens'])}/"
                    f"{_format_tokens(usage['context_window'])} tokens  "
                    f"({usage['percentage']}%)"
                )
            else:
                print(f"\n  [cli] New session: {sid}")

        while self._running:
            # Flush any pending background-thread output
            if self._pool is not None:
                self._pool._mgr.flush_bg_status()

            try:
                user_input = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break

            if user_input.lower() in ("exit", "quit"):
                print("Goodbye.")
                break

            if not user_input:
                continue

            # Build message + event (via converter)
            chain = self._converter.to_internal(user_input)
            message = Message(
                sender_id="cli_user",
                sender_name="User",
                message_chain=chain,
                message_type=MessageType.PRIVATE,
                session=session,
            )
            event = self.commit_event(message)

            # Run pipeline (blocking — CLI needs inline response)
            if self._scheduler is not None:
                response = await self._scheduler.execute(event)
                # Display response
                print(f"\nAgent: {response}")

                # Display stats
                usage = ctx_usage = None
                compression = None
                if event.extras.get("response") == response:
                    # Pipeline set extras — read stats from there
                    pass

                # Show context bar + compression
                if self._pool is not None and response:
                    sid, msgs = self._pool.get_or_create(session_key)
                    _display_context_usage(self._pool, session_key, msgs)
                    _display_compression(self._pool.last_compression)

                    # Show session note path
                    try:
                        mgr = self._pool._mgr
                        if mgr.session_memory.exists(sid):
                            note_path = mgr.session_memory.get_note_path(sid)
                            note_size = note_path.stat().st_size
                            size_str = (
                                f"{note_size / 1024:.1f}KB"
                                if note_size >= 1024
                                else f"{note_size}B"
                            )
                            print(f"  [cli] session note: {size_str}  ({note_path})")
                    except OSError:
                        pass

                _drain_console()
            else:
                # No scheduler — echo mode for testing
                print(f"\n[echo] {user_input}")
                _drain_console()

    async def send(self, session: MessageSession, content: str | MessageChain) -> bool:
        """打印到终端就是真的送达（CLI 的"对外投递"就是 stdout）。

        必须显式写这一条：基类默认 `send()` 回 `False`
        （默认体没有任何对外投递，不许报送达）——
        CLI 是唯一一个"打印 = 送达"成立的地方，所以它自己回 `True`。
        """
        text = content if isinstance(content, str) else content.content
        print(f"\n[{self.meta().name}] {text}")
        return True

    async def terminate(self) -> None:
        self._running = False
