"""
Tests for provider infrastructure.

Covers:
  - KeyRotator (round-robin, fault cooldown, expiry)
  - RateLimiter (acquire, refill, update_limits)
  - MessageDeduplicator (LRU, TTL, message_id vs hash keying)
  - ProviderMetadata (register, get, list)
  - ProviderValidation (api key checks, base URL, context window)
  - StreamChunk dataclass

Usage:
    cd <仓库根>
    python tests/test_provider_infra.py
"""

import sys
import time
import threading
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ------------------------------------------------------------------
# KeyRotator tests
# ------------------------------------------------------------------

def test_keyrotator_basic_round_robin():
    """Single-key rotator always returns the same key."""
    from providers.key_rotation import KeyRotator
    kr = KeyRotator(["sk-key1"], cooldown_seconds=10)
    assert kr.current() == "sk-key1"
    assert kr.current() == "sk-key1"
    assert kr.healthy_count == 1


def test_keyrotator_multi_key_rotation():
    """Three keys cycle in order."""
    from providers.key_rotation import KeyRotator
    kr = KeyRotator(["sk-a", "sk-b", "sk-c"], cooldown_seconds=10)
    keys = [kr.current() for _ in range(6)]
    assert keys[:3] == ["sk-a", "sk-b", "sk-c"]
    assert keys[3:] == ["sk-a", "sk-b", "sk-c"]


def test_keyrotator_fault_cooldown():
    """Faulted key is skipped until cooldown expires."""
    from providers.key_rotation import KeyRotator
    kr = KeyRotator(["sk-a", "sk-b", "sk-c"], cooldown_seconds=0.3)
    assert kr.current() == "sk-a"
    kr.mark_fault("sk-a")
    assert kr.current() == "sk-b"
    assert kr.healthy_count == 2
    # After cooldown, sk-a should return
    time.sleep(0.35)
    assert kr.healthy_count == 3


def test_keyrotator_all_faulted_fallback():
    """When all keys are faulted, return oldest fault."""
    from providers.key_rotation import KeyRotator
    kr = KeyRotator(["sk-a", "sk-b"], cooldown_seconds=60)
    kr.mark_fault("sk-a")
    time.sleep(0.05)
    kr.mark_fault("sk-b")
    # Both faulted — should return non-None key
    key = kr.current()
    assert key in ("sk-a", "sk-b")


def test_keyrotator_snapshot():
    """Snapshot returns debug state."""
    from providers.key_rotation import KeyRotator
    kr = KeyRotator(["sk-x", "sk-y"])
    snap = kr.snapshot()
    assert snap["total"] == 2
    assert snap["healthy"] == 2
    assert "cooldown_s" in snap


def test_keyrotator_empty_keys_raises():
    """Empty key list raises ValueError."""
    from providers.key_rotation import KeyRotator
    try:
        KeyRotator([])
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


# ------------------------------------------------------------------
# RateLimiter tests
# ------------------------------------------------------------------

def test_ratelimiter_initial_state():
    """Rate limiter starts with full buckets."""
    from providers.rate_limiter import RateLimiter
    rl = RateLimiter(rpm=60, tpm=100000)
    state = rl.state
    assert state["rpm_bucket"] == 60.0
    assert state["tpm_bucket"] == 100000.0


def test_ratelimiter_acquire_deducts():
    """Acquiring capacity reduces bucket levels."""
    from providers.rate_limiter import RateLimiter
    rl = RateLimiter(rpm=1000, tpm=1000000)
    rl.acquire(expected_tokens=100)
    state = rl.state
    assert state["rpm_bucket"] < 1000.0
    assert state["tpm_bucket"] < 1000000.0


def test_ratelimiter_update_limits():
    """Hot-update changes RPM/TPM."""
    from providers.rate_limiter import RateLimiter
    rl = RateLimiter(rpm=60, tpm=100000)
    rl.update_limits(rpm=120)
    assert rl.state["rpm"] == 120
    rl.update_limits(tpm=200000)
    assert rl.state["tpm"] == 200000


def test_ratelimiter_negative_values_raise():
    """Zero or negative RPM/TPM raises ValueError."""
    from providers.rate_limiter import RateLimiter
    try:
        RateLimiter(rpm=0, tpm=100)
        assert False
    except ValueError:
        pass
    try:
        RateLimiter(rpm=60, tpm=0)
        assert False
    except ValueError:
        pass


def test_ratelimiter_oversized_request_does_not_deadlock():
    """单次请求超过 TPM 上限时必须放行，不能永久挂起。

    回归测试。桶的上限被 _refill 钳在 self._tpm，所以
    needed_tpm > self._tpm 时 tpm_ok 永远为 False，`while True` 会
    无限 sleep。

    触发形状：一次摘要把整篇文档（94,009 字符）发给模型，tiktoken 估算
    ≈108,834 × 校准因子 1.1 ≈ 12 万 tokens > tpm 100,000
    → agent 卡在「正在总结」超过 1 分 26 秒且永不返回。

    用线程 + join(timeout) 断言，这样即使回归了也是测试失败而不是测试挂死。
    """
    import threading
    import time as _time
    from providers.rate_limiter import RateLimiter

    rl = RateLimiter(rpm=60, tpm=100000)
    done = threading.Event()

    def _call():
        t0 = _time.monotonic()
        rl.acquire(expected_tokens=119772)      # 超过 tpm 上限
        done.elapsed = _time.monotonic() - t0    # type: ignore[attr-defined]
        done.set()

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    assert done.wait(timeout=5.0), \
        "acquire() 超过 TPM 上限时挂死了 —— 死锁回归"
    assert done.elapsed < 2.0, f"不该等待: {done.elapsed:.2f}s"  # type: ignore[attr-defined]

    # 边界：刚好等于上限仍应走正常限流逻辑（这里桶是满的，立即通过）
    rl2 = RateLimiter(rpm=60, tpm=100000)
    rl2.acquire(expected_tokens=100000)
    assert rl2.state["tpm_bucket"] == 0.0


# ------------------------------------------------------------------
# MessageDeduplicator tests
# ------------------------------------------------------------------

def test_dedup_first_message_not_duplicate():
    """First occurrence is never a duplicate."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert not dd.is_duplicate("telegram", "user1", "Hello world", message_id="msg1")


def test_dedup_repeat_message_is_duplicate():
    """Second occurrence with same message_id is a duplicate."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert not dd.is_duplicate("telegram", "user1", "Hello", message_id="m1")
    assert dd.is_duplicate("telegram", "user1", "Hello", message_id="m1")


def test_dedup_content_hash_fallback():
    """Without message_id, content hash is used."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert not dd.is_duplicate("wx", "user2", "Same text here")
    assert dd.is_duplicate("wx", "user2", "Same text here")


def test_dedup_different_platforms_independent():
    """Same message_id on different platforms are distinct."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert not dd.is_duplicate("tg", "u1", "msg", message_id="m1")
    assert not dd.is_duplicate("wx", "u1", "msg", message_id="m1")


def test_dedup_lru_eviction():
    """Oldest entries are evicted when cache exceeds max."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=5, ttl_seconds=3600)
    for i in range(10):
        dd.is_duplicate("tg", "u1", f"msg{i}", message_id=f"m{i}")
    assert dd.size <= 5


def test_dedup_ttl_expiry():
    """Entries expire after TTL."""
    from core.dedup import MessageDeduplicator
    dd = MessageDeduplicator(max_entries=100, ttl_seconds=0.1)
    assert not dd.is_duplicate("tg", "u1", "temp", message_id="m1")
    time.sleep(0.15)
    # After TTL, should be seen as new again
    assert not dd.is_duplicate("tg", "u1", "temp", message_id="m1")


def test_dedup_persists_across_a_restart():
    """跨重启：飞书在重启后重投过 47 分钟前的消息（据反馈）。

    内存层 TTL 只有 300 秒、进程一重启就空，那条被当成新消息又答了一遍，
    回复还引用着那条旧消息（用户看到的正是「它怎么自己又回了一遍」）。
「这条答过没有」必须活得比进程久。
    """
    import os
    import tempfile
    from core.dedup import MessageDeduplicator

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "seen.json")
        a = MessageDeduplicator(persist_path=path)
        assert a.is_duplicate("feishu", "u1", "hi", message_id="m1") is False
        # 成功才落账：跨重启那一层只认"做成了"的，
        #:   不认"见过"的 —— 否则一次失败之后重投会被自己的账本挡下。
        a.mark_done("feishu", "u1", "hi", message_id="m1")
        b = MessageDeduplicator(persist_path=path)      # 模拟进程重启
        assert b.is_duplicate("feishu", "u1", "hi", message_id="m1") is True


def test_dedup_does_not_persist_content_hashes():
    """没有消息号时不落盘 —— 同一个人连发两句一模一样的话是合法的。

    落盘的话第二次会被静默吞掉 —— 那比重复答复更糟。
    """
    import os
    import tempfile
    from core.dedup import MessageDeduplicator

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "seen.json")
        a = MessageDeduplicator(persist_path=path)
        assert a.is_duplicate("feishu", "u1", "same words") is False
        #: 账本里不许出现 `:hash:` 这条键（有它就是落盘了）
        raw = open(path, encoding="utf-8").read() if os.path.exists(path) else "{}"
        assert ":hash:" not in raw, raw[:200]
        b = MessageDeduplicator(persist_path=path)
        assert b.is_duplicate("feishu", "u1", "same words") is False


def test_dedup_persisted_ttl_expires():
    """持久层也有 TTL —— 过了就该重新处理（不是永久封条）。"""
    import os
    import tempfile
    from core.dedup import MessageDeduplicator

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "seen.json")
        a = MessageDeduplicator(persist_path=path, ttl_seconds=0.05,
                                persist_ttl_seconds=0.05)
        assert a.is_duplicate("feishu", "u1", "hi", message_id="m2") is False
        a.mark_done("feishu", "u1", "hi", message_id="m2")
        time.sleep(0.08)
        #: 同一个实例（不 reload）也必须过期 —— 那靠 `_prune_seen`
        assert a.is_duplicate("feishu", "u1", "hi", message_id="m2") is False
        a.mark_done("feishu", "u1", "hi", message_id="m2")
        #: 另用一个号验 reload 那一侧（上面那次已经把它重新记过账了）
        assert a.is_duplicate("feishu", "u1", "hi", message_id="m3") is False
        a.mark_done("feishu", "u1", "hi", message_id="m3")
        time.sleep(0.08)
        b = MessageDeduplicator(persist_path=path, ttl_seconds=0.05,
                                persist_ttl_seconds=0.05)
        assert b.is_duplicate("feishu", "u1", "hi", message_id="m3") is False


def test_dedup_not_persisted_until_done():
    """只"见过"不算数 —— 没成功的那条不许进跨重启账本。

    现场：标记发生在管线最前面，后面失败时，
    平台重投会被自己的账本挡下（用户什么都收不到），而账上写着"处理过了"。
    """
    import os
    import tempfile
    from core.dedup import MessageDeduplicator

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "seen.json")
        a = MessageDeduplicator(persist_path=path)
        assert a.is_duplicate("feishu", "u1", "hi", message_id="m9") is False
        b = MessageDeduplicator(persist_path=path)
        assert b.is_duplicate("feishu", "u1", "hi", message_id="m9") is False, (
            "只占了个位、没做成 —— 重启后不许被当成处理过")


def test_dedup_release_allows_a_retry():
    """失败路径 `release()` 之后，同一条重投能被正常处理。"""
    from core.dedup import MessageDeduplicator

    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert dd.is_duplicate("telegram", "u1", "hi", message_id="m5") is False
    assert dd.is_duplicate("telegram", "u1", "hi", message_id="m5") is True, (
        "处理中的同一条重投要被挡住（并发/同批重复）")
    dd.release("telegram", "u1", "hi", message_id="m5")
    assert dd.is_duplicate("telegram", "u1", "hi", message_id="m5") is False, (
        "放位之后重投必须能进来 —— 否则一次失败 = 这条消息永久丢掉")


def test_dedup_scope_separates_chats():
    """会话维度：Telegram 的 `message_id` 只在单个 chat 内唯一。

    不带 `scope` 时，私聊与群里各自第 1 条（都是 `message_id=1`）会互撞，
    第二条被当成重复投递静默丢掉。
    """
    from core.dedup import MessageDeduplicator

    dd = MessageDeduplicator(max_entries=100, ttl_seconds=60)
    assert dd.is_duplicate("telegram", "u1", "早上好",
                           message_id="1", scope="111") is False
    assert dd.is_duplicate("telegram", "u1", "早上好",
                           message_id="1", scope="-222") is False, (
        "另一个 chat 的第 1 条不是重复")
    assert dd.is_duplicate("telegram", "u1", "早上好",
                           message_id="1", scope="111") is True
    #: 反方向：同一个 scope 里重复才该拦
    assert dd.is_duplicate("telegram", "u1", "早上好",
                           message_id="2", scope="111") is False


# ------------------------------------------------------------------
# ProviderMetadata tests
# ------------------------------------------------------------------

def test_metadata_register_and_get():
    """Metadata can be registered and retrieved."""
    from providers.metadata import (
        ProviderMetadata, register_provider_metadata, get_provider_metadata,
        clear_provider_metadata,
    )
    clear_provider_metadata()
    meta = ProviderMetadata(
        name="deepseek",
        api_mode="openai_compat",
        default_model="deepseek-v4-flash",
        context_window=65536,
        supports_function_calling=True,
        supports_streaming=True,
    )
    register_provider_metadata(meta)
    retrieved = get_provider_metadata("deepseek")
    assert retrieved is not None
    assert retrieved.name == "deepseek"
    assert retrieved.api_mode == "openai_compat"
    assert retrieved.context_window == 65536
    clear_provider_metadata()


def test_metadata_list_providers():
    """List returns all registered providers."""
    from providers.metadata import (
        ProviderMetadata, register_provider_metadata,
        list_provider_metadata, clear_provider_metadata,
    )
    clear_provider_metadata()
    register_provider_metadata(ProviderMetadata(name="a"))
    register_provider_metadata(ProviderMetadata(name="b"))
    all_meta = list_provider_metadata()
    assert "a" in all_meta
    assert "b" in all_meta
    clear_provider_metadata()


def test_metadata_get_nonexistent():
    """Get non-existent provider returns None."""
    from providers.metadata import get_provider_metadata
    assert get_provider_metadata("nonexistent") is None


# ------------------------------------------------------------------
# ProviderValidation tests
# ------------------------------------------------------------------

def test_validation_missing_api_key():
    """Empty API key produces error."""
    from providers.validation import validate_provider
    report = validate_provider("test", "", "https://api.test.com", "model-1", 65536)
    assert report.status == "error"
    assert not report.all_passed
    assert any(c.name == "api_key_present" and not c.passed for c in report.checks)


def test_validation_bad_base_url():
    """Non-http base URL produces error."""
    from providers.validation import validate_provider
    report = validate_provider("test", "sk-123", "not-a-url", "model-1", 65536)
    assert report.status == "error"
    assert any(c.name == "base_url" and not c.passed for c in report.checks)


def test_validation_small_context_window():
    """Too-small context window produces error."""
    from providers.validation import validate_provider
    report = validate_provider("test", "sk-123", "https://api.test.com", "m1", 2048)
    assert report.status == "error"
    assert any(c.name == "context_window" and not c.passed for c in report.checks)


def test_validation_ok_provider():
    """Fully valid provider passes validation."""
    from providers.validation import validate_provider
    report = validate_provider("deepseek", "sk-test123", "https://api.deepseek.com/v1",
                                "deepseek-v4-flash", 65536)
    assert report.status == "ok"
    assert report.all_passed


def test_validation_empty_model_name():
    """Empty model name produces error."""
    from providers.validation import validate_provider
    report = validate_provider("test", "sk-123", "https://api.test.com", "", 65536)
    assert report.status == "error"
    assert any(c.name == "model_name" and not c.passed for c in report.checks)


def test_validation_small_context_window_warning():
    """Context window < 16384 produces warning but not error."""
    from providers.validation import validate_provider
    report = validate_provider("test", "sk-123", "https://api.test.com", "m1", 8192)
    assert report.status == "warning"
    assert report.all_passed  # no error-level failures
    assert any(c.name == "context_window" and c.level == "warning" for c in report.checks)


# ------------------------------------------------------------------
# StreamChunk tests
# ------------------------------------------------------------------

def test_streamchunk_creation():
    """StreamChunk can hold all its fields."""
    from providers.base import StreamChunk
    chunk = StreamChunk(
        content="Hello",
        finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    assert chunk.content == "Hello"
    assert chunk.finish_reason == "stop"
    assert chunk.usage["prompt_tokens"] == 10
    assert chunk.error is None


def test_streamchunk_error():
    """StreamChunk can represent an error."""
    from providers.base import StreamChunk
    chunk = StreamChunk(error="Rate limit exceeded")
    assert chunk.error == "Rate limit exceeded"
    assert chunk.content is None


def test_streamchunk_tool_call():
    """StreamChunk can carry a partial tool call."""
    from providers.base import StreamChunk
    chunk = StreamChunk(
        tool_call={
            "id": "call_1",
            "type": "function",
            "function": {"name": "search", "arguments": '{"query":'},
        },
    )
    assert chunk.tool_call["function"]["name"] == "search"


# ------------------------------------------------------------------
# Registry provider metadata tests
# ------------------------------------------------------------------

def test_registry_provider_metadata():
    """bus.registry supports provider metadata CRUD."""
    from bus.registry import (
        register_provider_metadata, get_provider_metadata,
        list_providers, get_provider_metadatas, reset,
    )
    reset()
    register_provider_metadata("deepseek", {
        "name": "deepseek",
        "api_mode": "openai_compat",
        "context_window": 65536,
    })
    assert list_providers() == ["deepseek"]
    meta = get_provider_metadata("deepseek")
    assert meta["api_mode"] == "openai_compat"
    assert get_provider_metadata("nope") is None
    all_m = get_provider_metadatas()
    assert "deepseek" in all_m
    reset()


# ------------------------------------------------------------------
# Report — call this from __main__
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    tests = [
        # KeyRotator (6)
        ("test_keyrotator_basic_round_robin", test_keyrotator_basic_round_robin),
        ("test_keyrotator_multi_key_rotation", test_keyrotator_multi_key_rotation),
        ("test_keyrotator_fault_cooldown", test_keyrotator_fault_cooldown),
        ("test_keyrotator_all_faulted_fallback", test_keyrotator_all_faulted_fallback),
        ("test_keyrotator_snapshot", test_keyrotator_snapshot),
        ("test_keyrotator_empty_keys_raises", test_keyrotator_empty_keys_raises),
        # RateLimiter (4)
        ("test_ratelimiter_initial_state", test_ratelimiter_initial_state),
        ("test_ratelimiter_acquire_deducts", test_ratelimiter_acquire_deducts),
        ("test_ratelimiter_update_limits", test_ratelimiter_update_limits),
        ("test_ratelimiter_negative_values_raise", test_ratelimiter_negative_values_raise),
        # 超限死锁回归
        ("test_ratelimiter_oversized_request_does_not_deadlock",
         test_ratelimiter_oversized_request_does_not_deadlock),
        # MessageDeduplicator (6)
        ("test_dedup_first_message_not_duplicate", test_dedup_first_message_not_duplicate),
        ("test_dedup_repeat_message_is_duplicate", test_dedup_repeat_message_is_duplicate),
        ("test_dedup_content_hash_fallback", test_dedup_content_hash_fallback),
        ("test_dedup_different_platforms_independent", test_dedup_different_platforms_independent),
        ("test_dedup_lru_eviction", test_dedup_lru_eviction),
        ("test_dedup_ttl_expiry", test_dedup_ttl_expiry),
        ("test_dedup_persists_across_a_restart", test_dedup_persists_across_a_restart),
        ("test_dedup_does_not_persist_content_hashes", test_dedup_does_not_persist_content_hashes),
        ("test_dedup_persisted_ttl_expires", test_dedup_persisted_ttl_expires),
        ("test_dedup_not_persisted_until_done", test_dedup_not_persisted_until_done),
        ("test_dedup_release_allows_a_retry", test_dedup_release_allows_a_retry),
        ("test_dedup_scope_separates_chats", test_dedup_scope_separates_chats),
        # ProviderMetadata (3)
        ("test_metadata_register_and_get", test_metadata_register_and_get),
        ("test_metadata_list_providers", test_metadata_list_providers),
        ("test_metadata_get_nonexistent", test_metadata_get_nonexistent),
        # ProviderValidation (6)
        ("test_validation_missing_api_key", test_validation_missing_api_key),
        ("test_validation_bad_base_url", test_validation_bad_base_url),
        ("test_validation_small_context_window", test_validation_small_context_window),
        ("test_validation_ok_provider", test_validation_ok_provider),
        ("test_validation_empty_model_name", test_validation_empty_model_name),
        ("test_validation_small_context_window_warning", test_validation_small_context_window_warning),
        # StreamChunk (3)
        ("test_streamchunk_creation", test_streamchunk_creation),
        ("test_streamchunk_error", test_streamchunk_error),
        ("test_streamchunk_tool_call", test_streamchunk_tool_call),
        # Registry metadata (1)
        ("test_registry_provider_metadata", test_registry_provider_metadata),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
