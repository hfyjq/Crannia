"""
Unit tests for sliding window safety net。

剪法本体五条：
  1. Sliding window NOT triggered when under max_history
  2. Sliding window triggers when message count exceeds max_history
  3. Leading system messages are preserved after truncation
  4. Truncated list length equals max_history
  5. Default max_history = 100

剪法必须满足两件事（都见 `MemoryManager._apply_sliding_window` 的 docstring）：
  · 截断点对齐轮边界（按下标切会切断工具序列 -> 孤儿 tool 结果 -> 上游 400）；
  · 截断点贴 `<window_note>` 说明行（"更早的 N 条已移出，history_search 可找回"）。
  调用点是"发请求前的冷闸"（`maybe_trim_cold_window`，只在
  `now - last_turn_at > 闸值` 时剪 —— 护前缀缓存）。

冷闸的判据：
  6. 会话仍热 -> 不剪；7. 冷 + 超限 -> 剪 + 说明行（kind=window_note）+ _saved_count 同步；
  8. 开关关（settings.json）-> 从不剪；9. last_turn_at 缺（本进程没见过）-> 按不冷；
  10. 闸值可配；11. 对齐轮边界（表尾不以孤儿 tool 开头）；12. record_turn 不剪。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_sliding_window.py
"""

import json
import sys
import tempfile
import time
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _sandbox import sandbox  # noqa: E402
from providers.base import BaseProvider


# ------------------------------------------------------------------
# Mock Provider
# ------------------------------------------------------------------

class MockProvider(BaseProvider):
    """Fake provider for sliding window tests.

    Inherits the real `estimate_request_tokens` — 令牌账只允许一个仪器。
    """

    def __init__(self, context_window: int = 65536):
        self._context_window = context_window
        self.chat_calls = 0

    @property
    def context_window(self) -> int:
        return self._context_window

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError("streaming is not exercised here")

    def chat(self, messages: list[dict], tools=None, system=None, thinking=None):
        self.chat_calls += 1

        class FakeResponse:
            content = "Mock summary."
            tool_calls = None
            prompt_tokens = 100
            completion_tokens = 20

            @property
            def total_tokens(self):
                return self.prompt_tokens + self.completion_tokens

        return FakeResponse()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def make_messages(count: int, with_system: bool = True) -> list[dict]:
    """Build a message list with *count* user↔assistant exchanges."""
    messages = []
    if with_system:
        messages.append({
            "role": "system",
            "content": "You are a document analysis assistant. " * 5,
        })
    for i in range(count):
        messages.append({
            "role": "user",
            "content": f"[Turn {i}] 用户提问内容 " * 3,
        })
        messages.append({
            "role": "assistant",
            "content": f"[Turn {i}] 助手回复内容 " * 3,
        })
    return messages


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

def test_below_limit_untouched():
    """Messages under max_history should not be truncated."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":unused:",
            "short_term": {"max_history": 100},
        }
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["compression"] = {
            "token_threshold": 0.85, "keep_recent_turns": 3,
        }
        config["memory"]["long_term"] = {"chroma_path": f"{tmpdir}/chroma"}
        config["memory"]["semantic"] = {"db_path": f"{tmpdir}/profile.db"}

        manager = MemoryManager(config, provider)

        messages = make_messages(5)  # 1 system + 10 turn msgs = 11 total
        truncated, was_truncated = manager._apply_sliding_window(
            "test", messages,
        )

        assert not was_truncated, (
            f"Should NOT truncate {len(messages)} messages (max_history=100)"
        )
        assert truncated == messages, "Should return original list unchanged"

        print(f"  ✅ Below limit: {len(messages)} messages untouched")
        manager.close()


def test_over_limit_truncated():
    """Messages exceeding max_history should be truncated."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":unused:",
            "short_term": {"max_history": 10},
        }
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["compression"] = {
            "token_threshold": 0.85, "keep_recent_turns": 3,
        }
        config["memory"]["long_term"] = {"chroma_path": f"{tmpdir}/chroma"}
        config["memory"]["semantic"] = {"db_path": f"{tmpdir}/profile.db"}

        manager = MemoryManager(config, provider)

        messages = make_messages(20)  # 1 system + 40 turn msgs = 41 total
        assert len(messages) > 10, "Test requires > 10 messages"

        truncated, was_truncated = manager._apply_sliding_window(
            "test", messages,
        )

        assert was_truncated, (
            f"Should truncate {len(messages)} messages (max_history=10)"
        )
        assert len(truncated) <= 10, (
            f"Truncated list ({len(truncated)}) should be ≤ max_history (10)"
        )
        assert len(truncated) < len(messages), (
            f"Truncated ({len(truncated)}) < original ({len(messages)})"
        )

        print(f"  ✅ Over limit: {len(messages)} → {len(truncated)} messages")
        manager.close()


def test_system_messages_preserved():
    """Leading system messages must survive truncation."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":unused:",
            "short_term": {"max_history": 8},
        }
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["compression"] = {
            "token_threshold": 0.85, "keep_recent_turns": 3,
        }
        config["memory"]["long_term"] = {"chroma_path": f"{tmpdir}/chroma"}
        config["memory"]["semantic"] = {"db_path": f"{tmpdir}/profile.db"}

        manager = MemoryManager(config, provider)

        messages = [
            {"role": "system", "content": "PRIMARY INSTRUCTIONS"},
            {"role": "system", "content": "<historical_context>PAST SUMMARY</historical_context>"},
        ]
        # Add enough turn messages to exceed max_history
        for i in range(20):
            messages.append({"role": "user", "content": f"Q{i}"})
            messages.append({"role": "assistant", "content": f"A{i}"})

        truncated, was_truncated = manager._apply_sliding_window(
            "test", messages,
        )

        assert was_truncated
        assert truncated[0]["role"] == "system", "First message must be system"
        assert truncated[0]["content"] == "PRIMARY INSTRUCTIONS", (
            "Primary instructions must be preserved"
        )
        assert truncated[1]["role"] == "system", "Second message must be system"
        assert "<historical_context>" in truncated[1]["content"], (
            "Historical context must be preserved"
        )

        print(f"  ✅ System messages preserved: "
              f"head={truncated[0]['content'][:30]}... + "
              f"head={truncated[1]['content'][:30]}...")
        manager.close()


def test_single_system_message_truncation():
    """With one leading system message, keep it + last (N-1) messages."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":unused:",
            "short_term": {"max_history": 6},
        }
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["compression"] = {
            "token_threshold": 0.85, "keep_recent_turns": 3,
        }
        config["memory"]["long_term"] = {"chroma_path": f"{tmpdir}/chroma"}
        config["memory"]["semantic"] = {"db_path": f"{tmpdir}/profile.db"}

        manager = MemoryManager(config, provider)

        messages = make_messages(10, with_system=True)  # 1 system + 20 turn = 21

        truncated, was_truncated = manager._apply_sliding_window(
            "test", messages,
        )

        assert was_truncated
        assert len(truncated) == 6, (
            f"Expected exactly 6 messages, got {len(truncated)}"
        )
        assert truncated[0]["role"] == "system", "First message must be system"
        assert truncated[0] == messages[0], (
            "System message content must be identical to original"
        )

        print(f"  ✅ Single system: {len(messages)} → {len(truncated)}, "
              f"system msg intact")
        manager.close()


def test_default_max_history():
    """When config omits max_history, default to 100."""
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": ":unused:",
            # short_term present but no max_history key —— 空表测"缺键落默认"
            "short_term": {},
        }
    }

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        config["memory"]["storage_path"] = tmpdir
        config["memory"]["compression"] = {
            "token_threshold": 0.85, "keep_recent_turns": 3,
        }
        config["memory"]["long_term"] = {"chroma_path": f"{tmpdir}/chroma"}
        config["memory"]["semantic"] = {"db_path": f"{tmpdir}/profile.db"}

        manager = MemoryManager(config, provider)
        assert manager._max_history == 100, (
            f"Default max_history should be 100, got {manager._max_history}"
        )
        print(f"  ✅ Default max_history: {manager._max_history}")
        manager.close()


# ------------------------------------------------------------------
# 冷闸（`maybe_trim_cold_window`）
# ------------------------------------------------------------------

def _make_manager(tmpdir, max_history=10):
    from core.memory.manager import MemoryManager

    provider = MockProvider(context_window=65536)
    config = {
        "memory": {
            "enabled": True,
            "storage_path": str(tmpdir),
            "short_term": {"max_history": max_history},
            "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
            "long_term": {"chroma_path": f"{tmpdir}/chroma"},
            "semantic": {"db_path": f"{tmpdir}/profile.db"},
        }
    }
    return MemoryManager(config, provider)


def _plant_last_turn(sid, minutes_ago):
    """把会话的 last_turn_at 拨到 *minutes_ago* 分钟前。"""
    from core import memory_extract as ME
    ME._state[sid] = ME._blank()
    ME._state[sid]["last_turn_at"] = time.time() - minutes_ago * 60


def test_cold_window_settings_reads_and_defaults():
    """两个旋钮：读不到 -> (开, 90)；坏值逐个回默认（bool 单独拒）。"""
    from core.memory.manager import _cold_window_settings

    with sandbox() as tmp:
        assert _cold_window_settings() == (True, 90), "没有 settings 时该回默认"
        (tmp / "data" / "settings.json").write_text(
            json.dumps({"context_sliding_window": False,
                        "sliding_window_gap_minutes": 10}), encoding="utf-8")
        assert _cold_window_settings() == (False, 10)
        for bad in (True, False, 0, -5, 2000, "90", 90.0, None):
            (tmp / "data" / "settings.json").write_text(
                json.dumps({"sliding_window_gap_minutes": bad}), encoding="utf-8")
            assert _cold_window_settings() == (True, 90), \
                "脏闸值 %r 没回默认（isinstance(True,int) 那族坑）" % (bad,)
    print("  [ok] 冷闸旋钮：读不到/坏值回默认，好值照读")


def test_cold_gate_hot_session_not_trimmed():
    """热的会话不剪 —— 判据不是性能，是前缀缓存（剪一次 = 改写前缀）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        mgr = _make_manager(tmpdir, max_history=10)
        sid = "t-hot"
        _plant_last_turn(sid, minutes_ago=1)
        msgs = make_messages(20)
        out = mgr.maybe_trim_cold_window(sid, msgs)
        assert len(out) == len(msgs), "热的会话被剪了（缓存判据不许）"
        mgr.close()
    print("  [ok] 热会话：41 条 > 上限 10，仍一个不剪")


def test_cold_gate_cold_session_trimmed_with_note():
    """冷 + 超限 -> 剪，截断点贴 window_note 说明行，_saved_count 同步。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        mgr = _make_manager(tmpdir, max_history=10)
        sid = "t-cold"
        _plant_last_turn(sid, minutes_ago=200)
        msgs = make_messages(20)
        out = mgr.maybe_trim_cold_window(sid, msgs)
        assert len(out) <= 10, "冷会话没剪到位：%d" % len(out)
        notes = [m for m in out if m.get("kind") == "window_note"]
        assert len(notes) == 1, "截断点没有说明行（模型会以为历史不存在）"
        assert notes[0]["role"] == "system"
        assert "history_search" in notes[0]["content"], "说明行没给出找回路线"
        assert out.index(notes[0]) >= 1, "说明行要在表头之后（截断点）"
        assert mgr._saved_count[sid] == len(out), "落库相对位没同步（会重复落库）"
        mgr.close()
    print("  [ok] 冷会话：剪到位 + 说明行 + _saved_count 同步")


def test_cold_gate_switch_and_gap():
    """开关关 -> 从不剪；闸值可配（20 分钟前 vs 闸 10 分钟 = 冷）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox() as tmp:
        mgr = _make_manager(tmpdir, max_history=10)
        msgs = make_messages(20)

        (tmp / "data" / "settings.json").write_text(
            json.dumps({"context_sliding_window": False}), encoding="utf-8")
        sid = "t-off"
        _plant_last_turn(sid, minutes_ago=500)
        assert len(mgr.maybe_trim_cold_window(sid, msgs)) == len(msgs), \
            "开关关着还剪了"

        (tmp / "data" / "settings.json").write_text(
            json.dumps({"sliding_window_gap_minutes": 10}), encoding="utf-8")
        sid2 = "t-gap"
        _plant_last_turn(sid2, minutes_ago=20)
        assert len(mgr.maybe_trim_cold_window(sid2, msgs)) <= 10, \
            "比闸值更久（20 > 10）却没剪"
        mgr.close()
    print("  [ok] 开关关=从不剪；闸值可配（10 分钟闸对 20 分钟间隔生效）")


def test_cold_gate_unknown_last_turn_is_not_cold():
    """本进程没见过这个会话（last_turn_at 缺）-> 按不冷（可能刚重启、缓存还在）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        mgr = _make_manager(tmpdir, max_history=10)
        from core import memory_extract as ME
        sid = "t-unknown"
        ME.forget(sid)
        msgs = make_messages(20)
        assert len(mgr.maybe_trim_cold_window(sid, msgs)) == len(msgs)
        mgr.close()
    print("  [ok] last_turn_at 缺：按不冷（保守方向 = 不剪）")


def test_cut_starts_at_a_turn_boundary():
    """截断点对齐轮边界：表尾不以孤儿 tool 结果开头（按下标切会 400）。"""
    messages = [{"role": "system", "content": "sys"}]
    for i in range(12):
        messages.append({"role": "user", "content": f"Q{i}"})
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "probe",
                                                      "arguments": "{}"}}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "r"})

    # 先证明"随意切"真的会坏（否则这条用例在失去靶子）：
    # `[-7:]` 恰好落在一轮中间（表尾以 tool9 开头，它的 assistant 被切掉）
    from core.message_history import check
    naive = messages[:1] + messages[-7:]
    assert not check(naive).balanced, "夹具坏了：随意切居然没切坏（用例没靶子）"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        mgr = _make_manager(tmpdir, max_history=10)
        sid = "t-bnd"
        _plant_last_turn(sid, minutes_ago=200)
        out = mgr.maybe_trim_cold_window(sid, messages)
        assert len(out) <= 10
        report = check(out)
        assert report.balanced, "剪完的历史不平衡：%s" % report.explain()
        body = [m for m in out if m.get("kind") != "window_note"]
        assert body[1]["role"] == "user", "表尾该从轮首（user）开始"
        mgr.close()
    print("  [ok] 对齐轮边界：随意切会坏（已证），新剪法平衡")


def test_record_turn_no_longer_trims():
    """`record_turn` 不剪（滑窗只在发请求前、冷时由阶段调）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        mgr = _make_manager(tmpdir, max_history=10)
        sid = "t-rt"
        msgs = make_messages(20)          # 41 条 > 10
        out = mgr.record_turn(
            session_id=sid,
            user_message={"role": "user", "content": "hi"},
            assistant_message={"role": "assistant", "content": "yo"},
            token_count=5,
            messages=msgs,
        )
        # 注意：没压缩时 record_turn 也返回那份列表（check_and_compress
        #    没压就原样退回，不是 None）—— 判据要看长度，不是"None"。
        assert out is not None and len(out) == len(msgs), \
            "record_turn 又剪了（旧的无条件轮末剪已搬走）"
        assert mgr._saved_count.get(sid) == len(msgs), \
            "record_turn 阶段动了 _saved_count（说明它还在剪）"
        mgr.close()
    print("  [ok] record_turn 不再剪：超限 41 > 10 原样返回那 41 条")


def test_pool_wrapper_delegates():
    """阶段走的那条路：`SessionPool.maybe_trim_cold_window`（key -> sid 解析在里面）。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir, sandbox():
        from core.session_pool import SessionPool
        mgr = _make_manager(tmpdir, max_history=10)
        pool = SessionPool(mgr)
        sid = pool._resolve_sid("k-probe")
        _plant_last_turn(sid, minutes_ago=200)
        msgs = make_messages(20)
        out = pool.maybe_trim_cold_window("k-probe", msgs)
        assert len(out) <= 10, "池包装没把冷闸接上"
        mgr.close()
    print("  [ok] SessionPool 包装：key -> sid -> 冷闸，剪到位")


def test_stage_hook_wired_before_the_branch():
    """接线的位置是判据的一部分：冷闸必须在"分流 sync/流式"之前。

    这条是源级读者：本仓没有直接驱动 `AgentLoopStage` 的夹具
    （起一个真阶段要假 ctx + 假 pool + 假 provider 一串），而这条要钉的
    恰恰是"位置"—— 行为面由 pool/manager 两层各自的用例钉着。
    """
    src = (_PROJECT_ROOT / "core" / "pipeline" / "stages" / "agent_loop.py"
           ).read_text(encoding="utf-8")
    hook = src.find("maybe_trim_cold_window(")
    branch = src.find("if supports_streaming:")
    baseline = src.find("_msg_count_before = len(ctx.messages)")
    assert hook != -1, "阶段里没有冷闸调用 —— 滑窗永远不跑（设置页成了摆设）"
    assert branch != -1 and baseline != -1, "找不到分流点/基线点（阶段改名了？）"
    # 两条硬约束（不是风格）：
    #   ① 在 `if supports_streaming:` 之前 —— sync 与流式两条路都要过闸；
    #   ② 在 `_msg_count_before = len(...)` 之前 —— 那个基线喂给
    #      `_check_cross_zone_pending(ctx, start_idx)`，先剪后取基线会把
    #      扫描区段算错（跨区审批漏检）。
    assert hook < branch, "冷闸排在了分流之后 —— 会漏掉一条路"
    assert hook < baseline, "冷闸排在基线之后 —— 跨区审批的扫描区段会算错"
    assert "session_key_of_ctx(ctx)" in src[hook - 200: hook + 200], \
        "冷闸没按会话键取（key 与 sid 解析在 SessionPool 包装里）"


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Sliding Window Safety Net Tests (P3)")
    print("=" * 60)
    print()

    tests = [
        ("Below limit untouched", test_below_limit_untouched),
        ("Over limit truncated", test_over_limit_truncated),
        ("System messages preserved", test_system_messages_preserved),
        ("Single system message truncation", test_single_system_message_truncation),
        ("Default max_history = 100", test_default_max_history),
        # ---- 冷闸新增用例 ----
        ("Cold window settings", test_cold_window_settings_reads_and_defaults),
        ("Cold gate: hot session untouched", test_cold_gate_hot_session_not_trimmed),
        ("Cold gate: cold session trimmed + note", test_cold_gate_cold_session_trimmed_with_note),
        ("Cold gate: switch off / configurable gap", test_cold_gate_switch_and_gap),
        ("Cold gate: unknown last_turn_at not cold", test_cold_gate_unknown_last_turn_is_not_cold),
        ("Cut aligns to turn boundary", test_cut_starts_at_a_turn_boundary),
        ("record_turn no longer trims", test_record_turn_no_longer_trims),
        ("SessionPool wrapper delegates", test_pool_wrapper_delegates),
        ("Stage hook wired before the branch", test_stage_hook_wired_before_the_branch),
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
