"""
注入的记忆是快照，且失败不许把好笔记写坏。

为什么需要它
============
会话笔记是模型自己上一轮的自述。一句错误的自我描述（例如声称某个文件
"含注入样本、已按不可信数据处理"）会被原样存成笔记，随后以「背景事实」的
身份重新注入上下文（装载时 + 每次压缩后），并且在压缩时被逐字当作摘要
落库 —— 模型的自我描述从此变成了"历史事实"。

结论：
  * 不给"模型自述"打可信度标 —— 核对的位置在读取端，不在写入端。
  * 判据：回忆出的记忆与当前信息冲突时，以此刻观察到的为准；
    「记忆说 X 存在」不等于「X 现在存在」。

所以本文件钉两件事：读端有判据、写端不许写坏。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_provenance.py
"""

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

GOOD_NOTE = (
    "# Session Note - probe\n\n"
    "## Current Status\n"
 "- 正在核对不变量\n\n"
    "## Key Decisions\n"
    "- (None yet)\n\n"
    "## Open Questions\n"
    "- (None yet)\n\n"
    "## Next Steps\n"
    "- (None yet)\n"
)


class _ScriptedProvider:
    """返回一段写死的内容 —— 包括空串和纯围栏那两种坏形状。"""

    def __init__(self, content):
        self._content = content

    def chat(self, messages, tools=None, system=None, thinking=None):
        from providers.base import ChatResponse
        return ChatResponse(content=self._content, tool_calls=None)


def _make_mgr(tmp_dir, provider=None):
    """Build a MemoryManager rooted entirely in *tmp_dir*（不碰生产目录）。"""
    import core.memory.manager as manager_mod

    cfg = {
        "memory": {
            "storage_path": str(Path(tmp_dir) / "memory"),
            "short_term": {"max_history": 100},
            "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
            "session_memory": {"sessions_dir": str(Path(tmp_dir) / "sessions")},
        }
    }
    mgr = manager_mod.MemoryManager(cfg, provider=None)
    mgr._provider = provider
    return mgr


# ------------------------------------------------------------------
# 1-3. 写侧：失败方向必须是"保持不动"，不是"写坏"
# ------------------------------------------------------------------

def _run_update(tmp, response_content):
    """走完整链路：update() -> save() -> 读回磁盘。

    返回 (update 的返回值, 磁盘上的文本) —— 两个都要断言，理由见下。
    """
    from core.memory.session_memory import SessionMemory

    sm = SessionMemory(sessions_dir=str(Path(tmp) / "sessions"))
    sm.save("probe", GOOD_NOTE)
    out = sm.update(
        session_id="probe", current_note=GOOD_NOTE,
        user_message="u", assistant_message="a",
        provider=_ScriptedProvider(response_content),
    )
    sm.save("probe", out)
    return out, sm.load("probe")


def test_empty_response_does_not_wipe_the_note():
    """LLM 返回空串 —— 不许把上一份好笔记覆盖成空文件。

    只断言磁盘会假绿：`save()` 那道守卫单独就能保住文件，于是删掉
    `update()` 那道守卫之后"文件没坏"照样成立。
    所以同时断言 `update()` 的返回值 —— 那才是这道守卫控制的观测量。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out, on_disk = _run_update(tmp, "")
        assert out == GOOD_NOTE, \
            f"update() 把空串交了出去（修前就是这样，而调用方日志打的是 " \
            f"'updated (0 chars)'）: {out[:60]!r}"
        assert on_disk == GOOD_NOTE, "空回复把好笔记覆盖掉了"
    print("  [ok] an empty LLM response no longer wipes the note")


def test_fence_only_response_does_not_wipe():
    """最机械的一条路径：模型只回三个反引号，剥围栏那几行正好把它削成 ''。

    同上：返回值与磁盘两个观测量都断言。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        out, on_disk = _run_update(tmp, "```")
        assert out == GOOD_NOTE, f"纯围栏回复把笔记削空了: {out[:60]!r}"
        assert on_disk == GOOD_NOTE, f"纯围栏回复把笔记写坏了: {on_disk[:60]!r}"
    print("  [ok] a fence-only response no longer wipes the note")


def test_save_refuses_empty_content():
    """不变量落在写盘那一格：任何调用方交出空内容都不许覆盖已有笔记。"""
    import io
    import contextlib
    from core.memory.session_memory import SessionMemory

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        sm = SessionMemory(sessions_dir=str(Path(tmp) / "sessions"))
        sm.save("probe", GOOD_NOTE)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sm.save("probe", "")
        printed = buf.getvalue()

        assert sm.load("probe") == GOOD_NOTE, \
            "save('') 把已有笔记覆盖了 —— 这条不变量没了"
        assert "拒绝" in printed, \
            f"拒绝写入必须是**出声**的（静默兜底 = 没有兜底）。实得: {printed!r}"
    print("  [ok] save() refuses empty content, and says so out loud")


# ------------------------------------------------------------------
# 4. 读端：判据覆盖两个注入标签
# ------------------------------------------------------------------

def test_staleness_rule_names_both_context_tags():
    """判据在系统提示里，一处覆盖两个标签。

    为什么不是写进两个包裹里：`compressor._find_summary_text` 只剥
    `<historical_context>` 标签、其余原样当 `prior_summary` 喂回下一轮摘要
写在包裹里的文本会逐代累积。写在一处就没有这个问题。

    负对照：把 `_CORE_INSTRUCTIONS` 里那两条改名或删掉，本条立刻红。
    """
    from core.prompt_assembler import assemble_system_prompt

    sp = assemble_system_prompt("", model_name="probe-model")
    for tag in ("<background_context>", "<historical_context>"):
        assert tag in sp, f"判据没覆盖 {tag} —— 那条路就没有规则可依"
    assert "trust the tool" in sp, \
        "「与工具冲突时以工具为准」这条判据不在系统提示里"
    assert "not live state" in sp, \
        "没有说明注入的记忆是**快照**而不是现状"
    print("  [ok] the staleness rule covers both context tags, from one place")


# ------------------------------------------------------------------
# 5. 读端：笔记块要说清"这是你自己的笔记"
# ------------------------------------------------------------------

def test_session_note_is_labelled_as_own_notes():
    """断言打在注入输出上，不 grep 源码 —— 源码注释里
    就写着这句话，源码级检查会自己打自己。

    判据：笔记块要用「你自己的笔记」这一类描述性标签，
    不是"不可信内容"降级。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        mgr = _make_mgr(tmp)
        mgr._session_note_cache["probe"] = GOOD_NOTE
        ctx = mgr.get_context("probe", "hi")

        assert "SESSION STATE" in ctx, "笔记没被注入"
        assert "your own notes from earlier turns" in ctx, \
            ("笔记块仍然没说清它是谁写的 —— 它就是模型自己上一轮的自述，"
             "而这一点此前在注入文本里**完全没有**交代")
        assert GOOD_NOTE.strip().splitlines()[-1] in ctx, \
            "笔记正文没进去（只注入了表头？）"
    print("  [ok] the injected note is labelled as the agent's own notes")


# ------------------------------------------------------------------

def main():
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print("  FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d tests: %d passed, %d failed" % (len(tests), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
