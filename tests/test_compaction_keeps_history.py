"""四条判据 —— 压缩不再删原话。

不安全形状：压缩走 `replace_messages`（DELETE + INSERT 整表），被压掉的原话
物理消失，而 `history_search` 读的就是那张表，搜不回来，
工具描述里写着「更早的若已被压缩，原件不在库里（以摘要为界）」。
当前：压缩只追加 `[摘要行, 边界行]`，装载按边界切活动窗口。

四条判据（缺一条都不算过）：

  1. 端到端：真压一次，`history_search` 搜一个只在被压掉那些轮里
     出现过的词，搜得到；
  2. 对照臂：没被压掉的轮里的词也搜得到，而且不相关的词不是命中
     （少了这半边，①会退化成"搜全表坏成什么都搜得到"）；
  3. 反向：装载出来的上下文里没有那些被压掉的轮（边界真的生效）；
  4. 连压两次：第一代摘要的独有内容仍在新摘要里（甲：有旧摘要走合并）。

四条都做过变异验证（不是"绿了就算数"）：
  · 把 `_persist_compaction` 换回整表重写，①红；
  · 让 `_summarize` 丢掉 `prior_summary`，④红；
  · 让 `load_session` 不做边界切片，③红；
  · 让搜索"什么都算命中"，②红。

夹具的两条讲究：
  · 窗口开得很大 —— 装载预算是"从新往旧装到塞不下"，窗口一小，
    被压掉的最旧那几轮光靠预算就进不来，③会因为预算而绿；
  · 假的摘要器不回吐正文 —— 若它回吐 prompt，摘要里就含有全部轮次正文
①恒真（坏实现也照样绿）。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_compaction_keeps_history.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from providers.base import BaseProvider  # noqa: E402

#: 埋在"会被压掉"的那些轮里的词 / 埋在"会被保留"的那一轮里的词。
COMPRESSED_WORD = "ALPHAUNIQUE"
KEPT_WORD = "KEPTUNIQUE"


class MarkerProvider(BaseProvider):
    """上下文窗口很大，摘要器只回一个"它被喂了什么"的标记。

    窗口大与正文不回吐都是判据的一部分 —— 见文件抬头那两条讲究。
    """

    #: 窗口要小到"占用能过 10%"（新判据）：`/compact` 在占用不足 10% 时
    #:    会照实拒绝，而那与这一批要验的"压缩只追加"无关。
    def __init__(self, context_window: int = 2000):
        self._cw = context_window
        self.calls: list = []

    @property
    def context_window(self) -> int:
        return self._cw

    def count_tokens(self, text: str) -> int:
        return len(text)

    async def chat_stream(self, messages, tools=None, system=None, thinking=None):
        raise NotImplementedError

    def chat(self, messages, tools=None, system=None, thinking=None):
        self.calls.append(messages)
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        tag = "merged" if "[已有摘要" in prompt else "fresh"
        note = "+note" if "本会话的笔记" in prompt else ""

        class _R:
            content = "SUMMARY[%s%s]" % (tag, note)
            tool_calls = None
            prompt_tokens = 10
            completion_tokens = 10

            @property
            def total_tokens(self):
                return 20

        return _R()


def _make_mgr(tmp, provider):
    import core.memory.manager as manager_mod

    cfg = {"memory": {
        "storage_path": str(Path(tmp) / "memory"),
        "short_term": {"max_history": 100},
        "compression": {"token_threshold": 0.85, "keep_recent_turns": 2},
        "session_memory": {"sessions_dir": str(Path(tmp) / "sessions")},
    }}
    return manager_mod.MemoryManager(cfg, provider=provider)


def _turns(n, prefix):
    """n 轮问答（正文够长，压缩时才有得压）。"""
    out = []
    for i in range(n):
        out.append({"role": "user",
                    "content": "[%s-%d] " % (prefix, i) + "问" * 200})
        out.append({"role": "assistant",
                    "content": "[%s-%d] " % (prefix, i) + "答" * 200})
    return out


class _Lab:
    """临时库 + 真 manager + 注册（`history_search` 要 `bus.registry` 那一格）。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.provider = MarkerProvider()
        self.mgr = _make_mgr(self._tmp.name, self.provider)
        import bus.registry as R
        self._saved = R.get_memory_manager()
        R.register_memory_manager(self.mgr)
        self.sid, self.msgs = self.mgr.load_session()
        return self

    def __exit__(self, *exc):
        import bus.registry as R
        R.register_memory_manager(self._saved)
        try:
            self.mgr.close()
        finally:
            self._tmp.cleanup()
        return False

    def seed(self, body):
        """把 *body* 当成"到现在为止的全部消息"落库（生产里压缩一定在落库之后）。"""
        self.msgs.extend(body)
        self.mgr.short_term.save_messages(self.sid, body, [0] * len(body))
        self.mgr._saved_count[self.sid] = len(self.msgs)

    def compact(self, note=""):
        self.mgr._session_note_cache[self.sid] = note
        compressed, done = self.mgr.compact(self.sid, list(self.msgs))
        if done:
            # 生产里 `_cmd_compact` 就这一句（`ctx.messages = compacted`）——
            #   夹具少写它的话，第二次压缩看到的是压缩前那份列表
            #   （前导块里没有摘要）=> 走的是"没有旧摘要"那条路，判据 4 白测。
            self.msgs = compressed
        return compressed, done

    def reload(self):
        return self.mgr.load_session(self.sid)[1]


def _body(with_words=True):
    b = _turns(6, "T")
    if with_words:
        # 前两轮（会被压掉）各埋一个词；最后一轮（会被保留）埋另一个
        b[0]["content"] = COMPRESSED_WORD + " 第一轮的独有词 " + "问" * 200
        b[2]["content"] = COMPRESSED_WORD + "-B " + "答" * 200
        b[-2]["content"] = KEPT_WORD + " 保留轮里的词 " + "问" * 200
    return b


def _text_of(msgs, skip_summary=False):
    return "\n".join(
        str(m.get("content", "")) for m in msgs
        if not (skip_summary and m.get("kind") == "history_summary"))


# ----------------------------------------------------------------------
# 判据 3 -- 反向：装载里没有那些被压掉的轮（先跑它，它最容易假绿）
# ----------------------------------------------------------------------

def test_3_loaded_context_drops_the_compressed_turns():
    with _Lab() as lab:
        lab.seed(_body())
        _, done = lab.compact()
        assert done, "夹具坏了：压缩没跑起来"

        msgs = lab.reload()
        # 只看逐条的轮：摘要里当然有那段内容（它的职责就是把它压进去），
        # 这里问的是"被压掉的那几轮原样还在不在上下文里"。
        assert COMPRESSED_WORD not in _text_of(msgs, skip_summary=True), (
            "被压掉的轮进了上下文 -- 边界没生效（或装载返回了全表）")
        assert "SUMMARY[" in _text_of(msgs), "摘要没进上下文"
        assert KEPT_WORD in _text_of(msgs), "边界切过头了：保留的轮不见了"


def test_3b_boundary_row_exists_and_rows_are_appended():
    with _Lab() as lab:
        lab.seed(_body())
        lab.compact()
        rows = lab.mgr.short_term.load_message_rows(lab.sid)[0]
        kinds = [r.get("kind") for r in rows]
        assert kinds[-1] == "compaction_boundary", "摘要后面必须跟一条边界行"
        assert kinds[-2] == "history_summary", "摘要是追加进去的"
        assert COMPRESSED_WORD in _text_of(rows), (
            "被压掉的原话必须留在库里")


# ----------------------------------------------------------------------
# 判据 1 / 2 -- history_search：被压掉的搜得到，没被压掉的也搜得到
# ----------------------------------------------------------------------

def test_1_compressed_away_words_are_still_searchable():
    from core import request_snapshot
    from core.history_tools import search_history

    with _Lab() as lab:
        lab.seed(_body())
        lab.compact()

        channel = "w168:private:probe"
        lab.mgr.short_term.bind_session(channel, lab.sid)
        request_snapshot.set_current(request_snapshot.capture(
            [], [], "", model="probe", session_id=channel))
        try:
            r = search_history(lab.sid, COMPRESSED_WORD, turns=100)
        finally:
            request_snapshot.clear_current()

        # 判据打在结构化结果上，不许打在渲染文本上 -- 那段文本会把
        # query 原样回显（"在最近 N 轮里搜「…」"）=> 子串断言恒真。
        assert r["total"] >= 1, "被压掉的原话搜不到了（核心判据）"
        assert any(COMPRESSED_WORD in str(h.get("snippet", ""))
                   for h in r["hits"]), "命中的不是真 snippet"
        assert r["window"]["available"] >= 6, (
            "窗口读数只该看全表 -- 被压掉的那些轮也在库里")


def test_2_kept_words_are_findable_and_noise_is_not():
    from core.history_tools import search_history

    with _Lab() as lab:
        lab.seed(_body())
        lab.compact()

        r2 = search_history(lab.sid, KEPT_WORD, turns=100)
        assert r2["total"] >= 1, "对照臂：没被压掉的轮里的词也必须搜得到"

        r3 = search_history(lab.sid, "NOSUCHWORDXYZ", turns=100)
        assert r3["total"] == 0, "不相关的词不许成为命中（否则判据 1 是假的）"


# ----------------------------------------------------------------------
# 判据 4 -- 连压两次，第一代的独有内容仍在新摘要里
# ----------------------------------------------------------------------

def test_4_second_compaction_carries_the_first_generation():
    with _Lab() as lab:
        lab.seed(_body())
        lab.compact()                       # 第一压：无笔记 -> 纯 LLM
        first = lab.mgr.compressor.last_compression
        assert first and first["source"] == "llm", "第一压应当走 LLM"

        lab.seed(_turns(4, "U"))
        lab.mgr._saved_count[lab.sid] = len(lab.msgs)
        compressed2, done2 = lab.compact(
            note="## Current Status\n- 在测压缩边界\n\n## Key Decisions\n- 改完整\n")
        assert done2, "第二压没跑起来"

        new_summary = compressed2[0]["content"]
        assert "SUMMARY[merged+note]" in new_summary, (
            "有旧摘要时必须走**合并**；改成 notes 顶包就说明旧摘要被扔了")
        fed = "\n".join(str(m.get("content", ""))
                        for m in (lab.provider.calls[-1] or []))
        assert "SUMMARY[fresh]" in fed, (
            "第一代摘要的**正文**必须进了这次摘要调用的输入")
        assert "Key Decisions" in fed, "会话笔记也要一起喂进去"


# ----------------------------------------------------------------------
# 老库 / 新会话：没有边界行 => 全表（= 今天的行为，不需要迁移）
# ----------------------------------------------------------------------

def test_5_no_boundary_means_no_slicing():
    with _Lab() as lab:
        lab.seed(_turns(4, "T"))            # 落库，但从没压过
        rows = lab.mgr.short_term.live_message_rows(lab.sid)
        all_rows = lab.mgr.short_term.load_message_rows(lab.sid)[0]
        assert len(rows) == len(all_rows), "没有边界行时不许切片（老库的行为）"
        assert "[T-0]" in _text_of(rows)


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
