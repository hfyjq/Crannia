"""
Unit tests for TelegramChannel.

Validates:
  1. TelegramChannel is a Channel subclass
  2. ChannelMetadata is correct
  3. MessageSession creation from mock updates
  4. _split_text helper boundary cases
  5. _guess_file_type mappings
  6. Channel can be constructed without PTB imports
  7. Session isolation (different users → different sessions)

Usage:
    cd <仓库根>
    python tests/test_telegram_channel.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_mock_update(
    chat_id: int = 123456,
    user_id: int = 789,
    chat_type: str = "private",
    text: str = "hello",
    user_name: str = "Test User",
    username: str = "testuser",
):
    """Build a mock Telegram Update object for testing.

    Uses unittest.mock to create an object that quacks like a
    `telegram.Update` enough to test our conversion logic.
    """
    from unittest.mock import MagicMock

    update = MagicMock()
    update.update_id = 1

    # effective_chat
    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    update.effective_chat = chat

    # effective_user
    user = MagicMock()
    user.id = user_id
    user.full_name = user_name
    user.username = username
    update.effective_user = user

    # message
    msg = MagicMock()
    msg.message_id = 1
    msg.text = text
    msg.chat = chat
    msg.from_user = user
    msg.reply_text = MagicMock()
    update.message = msg

    return update


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_telegram_channel_is_channel():
    """TelegramChannel should be a Channel subclass."""
    from channels.telegram_channel.handler import TelegramChannel
    from channels.base import Channel

    assert issubclass(TelegramChannel, Channel), (
        "TelegramChannel must be a Channel subclass"
    )
    print("  ✅ TelegramChannel is a Channel subclass")


def test_channel_metadata():
    """TelegramChannel.meta() should return correct metadata."""
    from channels.telegram_channel.handler import TelegramChannel

    channel = TelegramChannel()
    meta = channel.meta()

    assert meta.name == "telegram_channel"
    assert meta.platform_type == "telegram"
    assert meta.support_multimodal is True
    print(f"  ✅ Metadata: name={meta.name}, platform={meta.platform_type}, "
          f"multimodal={meta.support_multimodal}")


def test_make_session_private():
    """Private chats get PRIVATE MessageType."""
    from channels.telegram_channel.handler import TelegramChannel
    from channels.base import MessageType

    channel = TelegramChannel()
    update = _make_mock_update(chat_id=123456, chat_type="private")

    session = channel._make_session(update)

    assert session.platform == "telegram"
    assert session.message_type == MessageType.PRIVATE
    assert session.session_id == "123456"
    assert str(session) == "telegram:private:123456"
    print(f"  ✅ Private session: {session}")


def test_make_session_group():
    """Group chats get GROUP MessageType."""
    from channels.telegram_channel.handler import TelegramChannel
    from channels.base import MessageType

    channel = TelegramChannel()
    update = _make_mock_update(chat_id=999888, chat_type="group")

    session = channel._make_session(update)

    assert session.platform == "telegram"
    assert session.message_type == MessageType.GROUP
    assert session.session_id == "999888"
    assert str(session) == "telegram:group:999888"
    print(f"  ✅ Group session: {session}")


def test_make_session_supergroup():
    """Supergroups also get GROUP MessageType."""
    from channels.telegram_channel.handler import TelegramChannel
    from channels.base import MessageType

    channel = TelegramChannel()
    update = _make_mock_update(chat_id=555, chat_type="supergroup")

    session = channel._make_session(update)
    assert session.message_type == MessageType.GROUP
    print(f"  ✅ Supergroup session: {session}")


def test_session_isolation_different_users():
    """Two different users should get different sessions."""
    from channels.telegram_channel.handler import TelegramChannel

    channel = TelegramChannel()
    user_a = _make_mock_update(chat_id=111, user_id=1)
    user_b = _make_mock_update(chat_id=222, user_id=2)

    session_a = channel._make_session(user_a)
    session_b = channel._make_session(user_b)

    assert str(session_a) != str(session_b), (
        f"Different users must have different sessions: "
        f"{session_a} vs {session_b}"
    )
    assert session_a.session_id == "111"
    assert session_b.session_id == "222"
    print(f"  ✅ Session isolation: User A={session_a}, User B={session_b}")


def test_make_session_from_update():
    """_make_session should extract correct session from a mock update."""
    from channels.telegram_channel.handler import TelegramChannel
    from channels.base import MessageType

    channel = TelegramChannel()
    update = _make_mock_update(
        chat_id=6584719818, user_id=12345, chat_type="private",
        user_name="Rain",
    )

    session = channel._make_session(update)

    assert session.platform == "telegram"
    assert session.message_type == MessageType.PRIVATE
    assert session.session_id == "6584719818"
    assert str(session) == "telegram:private:6584719818"

    print(f"  ✅ Make session from update: {session}")


def test_split_text_short():
    """Text under limit should not be split."""
    from channels.telegram_channel.handler import _split_text

    text = "Hello, world!"
    chunks = _split_text(text, 100)
    assert len(chunks) == 1
    assert chunks[0] == text

    print(f"  ✅ Short text: 1 chunk ({len(text)} chars)")


def test_split_text_long():
    """Text over limit should be split at newlines."""
    from channels.telegram_channel.handler import _split_text

    # Build text with clear newline breaks
    paragraphs = [f"Paragraph {i}: " + "x" * 50 for i in range(10)]
    text = "\n".join(paragraphs)
    max_len = 120  # Each paragraph ~65 chars, so roughly 2 per chunk

    chunks = _split_text(text, max_len)

    assert len(chunks) > 1, f"Should split into multiple chunks, got {len(chunks)}"
    for chunk in chunks:
        assert len(chunk) <= max_len, (
            f"Each chunk must be ≤ {max_len} chars, got {len(chunk)}"
        )

    # Rejoining should give original content (minus whitespace normalization)
    rejoined = "".join(chunks)
    assert "Paragraph 0" in rejoined
    assert "Paragraph 9" in rejoined

    print(f"  ✅ Long text: {len(text)} chars → {len(chunks)} chunks "
          f"(max {max_len}/chunk)")


def test_split_text_exact_limit():
    """Text exactly at limit should be single chunk."""
    from channels.telegram_channel.handler import _split_text

    text = "A" * 100
    chunks = _split_text(text, 100)
    assert len(chunks) == 1
    print(f"  ✅ Exact limit: {len(text)} chars → 1 chunk")


def test_sanitize_filename():
    """Session IDs with special chars should be sanitized for filenames."""
    from core.memory.session_memory import SessionMemory

    original = "telegram:private:6584719818"
    safe = SessionMemory._sanitize_filename(original)
    assert ":" not in safe
    assert safe == "telegram_private_6584719818"

    # Also test other illegal chars
    bad = 'test<>:"/\\|?*id'
    safe2 = SessionMemory._sanitize_filename(bad)
    for char in '<>:"/\\|?*':
        assert char not in safe2

    print(f"  ✅ Filename sanitize: '{original}' → '{safe}'")


def test_channel_constructor_no_token():
    """Channel should construct without a bot token (graceful start failure)."""
    from channels.telegram_channel.handler import TelegramChannel

    channel = TelegramChannel(config={})
    assert channel._bot_token == ""
    # Should not crash — just won't start
    print(f"  ✅ Constructor: no token → empty string (won't start)")
    print(f"     Start message will tell user to set TELEGRAM_BOT_TOKEN")


def test_channel_constructor_with_token():
    """Channel should read token from config."""
    from channels.telegram_channel.handler import TelegramChannel

    config = {
        "channel": {
            "telegram": {
                "bot_token": "12345:abcde",
            }
        }
    }
    channel = TelegramChannel(config=config)
    assert channel._bot_token == "12345:abcde"
    print(f"  ✅ Constructor: token from config → '{channel._bot_token[:8]}...'")


def test_channel_constructor_env_token():
    """Channel should read token from environment variable."""
    import os
    from channels.telegram_channel.handler import TelegramChannel

    os.environ["TELEGRAM_BOT_TOKEN"] = "env_token_123"
    try:
        channel = TelegramChannel(config={})
        assert channel._bot_token == "env_token_123"
        print(f"  ✅ Constructor: token from env → '{channel._bot_token}'")
    finally:
        del os.environ["TELEGRAM_BOT_TOKEN"]


def test_proxy_is_resolved_at_run_time():
    """Proxy must be re-read on every `run()` (not frozen in `__init__`).

 Live case: the channel instance lives for the whole process,
    so `__init__` runs once. If the proxy is fixed there, "start channel ->
    then enable proxy" cannot be fixed even by stop+start (same instance);
    only a full app restart works. Symptom: green "running" card in the UI
    while messages sent in Telegram get no reply at all.
    """
    import inspect
    from channels.telegram_channel.handler import TelegramChannel

    run_src = inspect.getsource(TelegramChannel.run)
    assert "_resolve_proxy()" in run_src, (
        "run() does not re-read the proxy -- it is frozen at construction again")
    init_src = inspect.getsource(TelegramChannel.__init__)
    assert "_resolve_proxy()" in init_src, "constructor should seed the value"
    print("  [ok] proxy is resolved inside run() (constructor only seeds it)")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def test_lost_update_is_announced_in_the_chat():
    """一条 update 处理失败，在那个会话里说「消息遗失，未接受」。

    判据：游标照推（这条算丢），但必须说出来 ——
    不推游标会让一条坏消息被无限重投、整条通道卡死；推了却不说，用户看见的
    是"我发了，它没理我"。
    """
    import asyncio
    from unittest.mock import AsyncMock
    from channels.telegram_channel.handler import TelegramChannel

    ch = TelegramChannel()
    bot = AsyncMock()
    ch._bot = bot
    update = _make_mock_update(chat_id=4242)

    asyncio.run(ch._notify_lost(update))

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 4242, kwargs
    assert "消息遗失，未接收" in kwargs["text"], kwargs["text"]
    assert "重新发送" in kwargs["text"], kwargs["text"]


def test_lost_update_without_a_chat_only_logs():
    """拿不到 chat / 没有 bot，不抛（失败路径再抛一次会连累同一批后面的 update）。"""
    import asyncio
    from unittest.mock import AsyncMock
    from channels.telegram_channel.handler import TelegramChannel

    ch = TelegramChannel()
    ch._bot = AsyncMock()
    asyncio.run(ch._notify_lost(object()))     # 没有 effective_chat
    ch._bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    print("=" * 60)
    print("  Telegram Channel Tests")
    print("=" * 60)
    print()

    tests = [
        ("Channel ABC compliance", test_telegram_channel_is_channel),
        ("ChannelMetadata", test_channel_metadata),
        ("Make session (private)", test_make_session_private),
        ("Make session (group)", test_make_session_group),
        ("Make session (supergroup)", test_make_session_supergroup),
        ("Session isolation", test_session_isolation_different_users),
        ("Make session from update", test_make_session_from_update),
        ("Split short text", test_split_text_short),
        ("Split long text", test_split_text_long),
        ("Split exact limit", test_split_text_exact_limit),
        ("Session ID filename sanitize", test_sanitize_filename),
        ("Constructor without token", test_channel_constructor_no_token),
        ("Constructor with config token", test_channel_constructor_with_token),
        ("Constructor with env token", test_channel_constructor_env_token),
        #: 手写清单：新加的 test_ 必须自己登记，否则它静默不跑
        #:   （`S4` 那族：没有读者的清单会烂；本文件就是清单式 runner）。
        ("Proxy resolved at run()", test_proxy_is_resolved_at_run_time),
        ("Lost update announced", test_lost_update_is_announced_in_the_chat),
        ("Lost update without chat", test_lost_update_without_a_chat_only_logs),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  ❌ FAIL: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
