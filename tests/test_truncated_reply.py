"""「被上游截断」不许被当成「说完了」。

## 它治的病（逐个字都是盘上读出来的）

档 4（`thinking` 开）跑表格题：22 轮真活（写脚本 / 跑 /
读图 / 发现机器上装着 Excel 就用 COM 驱动它），一路没有死循环 ——
然后戛然而止。库里最后一条：

    content='' · tool_calls=[] · reasoning_content=13979 字

而那段思考的末尾是 `If the grader'` —— 半句话、引号没闭合。

三条链一起断，缺一条都构不成这个场景：

  1. `providers/openai_compat.chat()` 的同步路从不给
     `ChatResponse.finish_reason` 赋值（字段在 `base.py` 声明了很久）；
     流式那条看起来给了，但它挂在「有正文 / 有工具调用 / 有 usage」
     三个出口上 —— 而上游的收尾片恰好三样都没有
     （`test_stream_accumulator.py` 里那条现成的夹具就是这么写的）
`finish_reason` 一样丢。
  2. `core/` 里没有一处读 `finish_reason`，「被截断」与「说完了」同形。
  3. 于是循环走 `if not tool_calls`：同步路回一句英文占位符，
     流式路回一个空串，界面上什么都没有，用户只能猜。

## 判据

「被截断」必须与「真的没说话」分得开，分开了要说出来，而且那句话不进历史。

  * 读不到 `finish_reason`，不猜（退回今天的行为，不会更坏）；
  * 正常一轮，一个字都不许多（负对照，见第 4 条）。

用法:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_truncated_reply.py

ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉。
"""

import asyncio
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.agent_loop as AL  # noqa: E402
from providers.base import StreamChunk  # noqa: E402

# 模式影响派发 —— 自己钉住，别继承开发机的 data/settings.json。
from _sandbox import neutralise_settings  # noqa: E402
neutralise_settings()

_SESS = "probe:truncated"


# ------------------------------------------------------------------
# 两个同形的替身 provider
# ------------------------------------------------------------------

class _SyncProvider:
    _model = "probe-model"

    def __init__(self, content, finish):
        self._content, self._finish = content, finish
        self.calls = 0

    def chat(self, messages, tools, system=None, thinking=None):
        self.calls += 1
        return types.SimpleNamespace(
            content=self._content, tool_calls=None, reasoning_content=None,
            finish_reason=self._finish, total_tokens=1)


class _StreamProvider:
    _model = "probe-model"

    def __init__(self, content, finish):
        self._content, self._finish = content, finish

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        if self._content:
            yield StreamChunk(content=self._content)
        if self._finish:
            yield StreamChunk(finish_reason=self._finish)


def _run_sync(content, finish):
    out, msgs, _c, _t = AL.agent_loop(
        "hi", _SyncProvider(content, finish),
        messages=[{"role": "user", "content": "hi"}],
        session_id=_SESS, max_turns=5)
    return out, msgs


def _run_stream(content, finish):
    async def _drain():
        last = None
        async for st in AL.agent_loop_stream(
                "hi", _StreamProvider(content, finish),
                messages=[{"role": "user", "content": "hi"}],
                session_id=_SESS, max_turns=5):
            last = st
        return last
    return asyncio.run(_drain())


# ------------------------------------------------------------------
# 1. 两条出口分得开
# ------------------------------------------------------------------

def test_a_cut_off_turn_is_not_reported_as_finished():
    """核心：`length` 不许走「说完了」那条路。"""
    out, _msgs = _run_sync("", "length")
    assert out == AL._TRUNCATED_NOTE, (
        "被截断的那一轮没有走到它该走的出口，实得 %r" % (out,))
    assert "截断" in out, out
    assert "no response from model" not in out, (
        "还是那句英文占位符 —— 说明这个字段读到了却没用上：%r" % (out,))
    print("  [ok] 被截断 -> 说出'截断'，不再是英文占位符")


def test_the_two_empty_cases_are_told_apart():
    """判别力在这条上：两个案例必须给不同的话。

    只断言"被截断时说了截断"是不够的 —— 把两句话写成同一句也能过。
    这里逼的是它们不同。
    """
    cut, _ = _run_sync("", "length")
    silent, _ = _run_sync("", "stop")
    assert cut != silent, (
        "被上游截断 与 模型真的没说话 给了**同一句话** —— 那就等于没区分：%r" % (cut,))
    assert cut == AL._TRUNCATED_NOTE, cut
    assert silent == AL._EMPTY_REPLY, silent
    print("  [ok] 被截断 与 真的没说话 是两句不同的话")


# ------------------------------------------------------------------
# 2. 有正文时：正文留着，那句话不进历史
# ------------------------------------------------------------------

def test_the_partial_text_is_kept_and_flagged():
    out, msgs = _run_sync("前半句", "length")
    assert out.startswith("前半句"), (
        "残缺的正文被丢掉了 —— 那是已经做完的活：%r" % (out,))
    assert AL._TRUNCATED_NOTE in out, out
    assert msgs[-1]["content"] == "前半句", (
        "**框架自己那句话进了历史**：%r\n"
        "  => 它是渲染，不是模型说的话（同 `CANCEL_REPLY` 那条判据）"
        % (msgs[-1],))
    print("  [ok] 残缺正文保留、框架那句话不进历史")


# ------------------------------------------------------------------
# 3. 正常一轮：一个字都不许多（负对照）
# ------------------------------------------------------------------

def test_a_normal_turn_is_left_untouched():
    """负对照。少了它，把每一条回复都追加一句说明也能全绿。"""
    out, _ = _run_sync("完整回答", "stop")
    assert out == "完整回答", (
        "正常一轮被这条新分支改动了：%r" % (out,))
    print("  [ok] 正常一轮逐字不变（负对照）")


def test_a_missing_finish_reason_is_not_guessed_as_truncation():
    """读不到，不猜。判错的代价不对称：误报会让每一轮都多一句假噪声。"""
    out, _ = _run_sync("", None)
    assert out == AL._EMPTY_REPLY, (
        "读不到 `finish_reason` 时被当成了'被截断'：%r" % (out,))
    print("  [ok] 读不到 finish_reason -> 不猜（同那条判据）")


# ------------------------------------------------------------------
# 3b. 被截断，接着写
# ------------------------------------------------------------------
#
# 定案：进历史（一条带 `kind` 的 user 消息）· 不算一轮
# （独立计数器，上限 3）。
#
# 上一节的替身是"每次都给同一个答案"的 —— 它用来验那两句话
# 刚好够用，而验不了"接着写"（那一支要求第二次给的不一样）。
#这里加一个按脚本走的替身。


class _ScriptProvider:
    """按脚本逐次返回 `(content, finish)`，用完了就一直重复最后一条。"""

    _model = "probe-model"

    def __init__(self, script):
        self._script, self.calls = list(script), 0

    def _next(self):
        got = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return got

    def chat(self, messages, tools, system=None, thinking=None):
        content, finish = self._next()
        return types.SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=None,
            finish_reason=finish, total_tokens=1)

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        content, finish = self._next()
        if content:
            yield StreamChunk(content=content)
        if finish:
            yield StreamChunk(finish_reason=finish)


def _run_script(script, max_turns=5, stream=False):
    prov = _ScriptProvider(script)
    if stream:
        async def _drain():
            last = None
            async for st in AL.agent_loop_stream(
                    "hi", prov, messages=[{"role": "user", "content": "hi"}],
                    session_id=_SESS, max_turns=max_turns):
                last = st
            return last
        out = asyncio.run(_drain())
    else:
        out = AL.agent_loop(
            "hi", prov, messages=[{"role": "user", "content": "hi"}],
            session_id=_SESS, max_turns=max_turns)
    return out, prov


def test_a_truncated_turn_is_continued_in_the_same_turn():
    """核心：断了就接着写，而给用户的是一整句（前半截 + 续写的）。

    那条"拼起来"是有判据的：被截断掉的那半截正是唯一做出来的东西，
    拿续写的那一半当最终回复 = 把用户的东西弄丢了。
    """
    (out, msgs, _c, _t), prov = _run_script([("前半句，", "length"),
                                             ("接着写完了。", "stop")])
    assert out == "前半句，接着写完了。", (
        "最终回复不是拼起来的：%r —— 前半截被丢了" % (out,))
    assert prov.calls == 2, "只调用了一次 —— 没有续写：%d" % prov.calls
    kinds = [m.get("kind") for m in msgs if m.get("role") == "user"]
    assert AL.OUTPUT_RECOVERY_KIND in kinds, (
 "那条续写指令没进历史（定案是**进**）：%r" % kinds)
    cut = [m for m in msgs if m.get("role") == "assistant"
           and m.get("content") == "前半句，"]
    assert cut, "被截断的那条 assistant 不在历史里：%r" % msgs
    assert AL.OUTPUT_RECOVERY_MSG not in out, "那句指令漏进了给用户看的文本"
    print("  [ok] 被截断 -> 同一轮里接着写；回复是拼起来的；指令进历史不进正文")


def test_the_stream_path_does_the_same_thing():
    """流式那条路必须逐字同义。"""
    (out, msgs, _c, _t), prov = _run_script([("前半句，", "length"),
                                            ("接着写完了。", "stop")], stream=True)
    assert out == "前半句，接着写完了。", out
    assert prov.calls == 2, prov.calls
    assert any(m.get("kind") == AL.OUTPUT_RECOVERY_KIND for m in msgs), msgs
    print("  [ok] 流式那条路同义")


def test_the_recovery_does_not_eat_a_model_turn():
    """另一半：不算一轮 `max_turns`。

    `max_turns=1` 是这条的全部判别力：如果续写吃掉了那一轮，
    第二次调用根本不会发生（现在的 `for turn in range(max_turns)` 形状）。
    """
    (out, _msgs, _c, _t), prov = _run_script([("半句", "length"),
                                              ("完。", "stop")], max_turns=1)
    assert prov.calls == 2, (
 "续写吃掉了 `max_turns` 那一轮（只调用了 %d 次）—— 定的是**不算**"
        % prov.calls)
    assert out == "半句完。", out
    print("  [ok] `max_turns=1` 也能续写一次 —— 续写不吃轮次")


def test_the_recovery_is_capped_at_three():
    """上限 3 —— 而且用完之后退回「被截断 / 空回复」那两句话。"""
    (out, msgs, _c, _t), prov = _run_script([("永远说不完，", "length")])
    n_recovery = sum(1 for m in msgs if m.get("kind") == AL.OUTPUT_RECOVERY_KIND)
    assert n_recovery == AL.MAX_OUTPUT_RECOVERY == 3, (
        "续写的次数不是 3：%d（常量 %d）" % (n_recovery, AL.MAX_OUTPUT_RECOVERY))
    assert prov.calls == 4, "总调用次数应当 = 1 + 3：%d" % prov.calls
    assert AL._TRUNCATED_NOTE in out, "用完之后没有退回那句话：%r" % out[-80:]
    print("  [ok] 续写最多 3 次，之后退回「被截断」那句话")


def test_nothing_to_continue_from_is_not_a_recovery():
    """负对照：两样都空（没有正文、也没有思考），没有"接着写"的起点。

    那时照旧走 `_EMPTY_REPLY` / `_TRUNCATED_NOTE`，
    不许装作恢复过（追加一条全空的 assistant 消息还可能被上游 400）。
    """
    (out, msgs, _c, _t), prov = _run_script([("", "length")])
    assert prov.calls == 1, "空回复也去续写了：%d 次" % prov.calls
    assert not any(m.get("kind") == AL.OUTPUT_RECOVERY_KIND for m in msgs), msgs
    assert out == AL._TRUNCATED_NOTE, out
    print("  [ok] 没东西可接时不续写（负对照）")


# ------------------------------------------------------------------
# 4. 流式那条路：同一句话
# ------------------------------------------------------------------

def test_the_stream_path_says_the_same_thing():
    """流式那条若不镜像，会给空串 —— 界面上什么都没有。"""
    for content, finish in (("", "length"), ("", "stop"), ("正文", "stop")):
        a = _run_sync(content, finish)[0]
        b = _run_stream(content, finish)[0]
        assert a == b, (
            "两条路给了不同的话（content=%r finish=%r）：sync=%r stream=%r"
            % (content, finish, a, b))
    cut = _run_stream("", "length")[0]
    assert cut, "流式那条又给了空串（修前的形状）—— 用户会看到一片空白"
    print("  [ok] 流式与 sync 逐字同话（含'修前是空串'那一格）")


# ------------------------------------------------------------------
# 5. 观测那一半：`finish_reason` 必须真的到得了
# ------------------------------------------------------------------

def _sdk(delta=None, finish=None, usage=None):
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(delta=delta or types.SimpleNamespace(
            content=None, reasoning_content=None, tool_calls=None),
            finish_reason=finish)],
        usage=usage)


def _provider(chunks=(), message=None):
    """一个假客户端的 provider。`_build_client` 故意炸 —— key 没变不许重建。"""
    from providers.openai_compat import OpenAICompatProvider
    p = OpenAICompatProvider(api_key="x", base_url="http://127.0.0.1:1", model="probe")
    p._calibrator = None            # 别读也别写盘上的校准器
    p._build_client = lambda key: (_ for _ in ()).throw(
        AssertionError("key 没变却重建了客户端（`A1` 回退了）"))

    if message is not None:
        p._client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(
                create=lambda **kw: types.SimpleNamespace(
                    choices=[types.SimpleNamespace(message=message, finish_reason=chunks)],
                    usage=None))))
    else:
        p._client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kw: iter(list(chunks)))))
    return p


def _drain_stream(p):
    async def _d():
        return [c async for c in p.chat_stream([{"role": "user", "content": "hi"}], None)]
    return asyncio.run(_d())


def test_the_terminator_chunk_carries_finish_reason_out():
    """上游的收尾片是「`delta` 空、只有 `finish_reason`」。

    若那三个出口（正文 / 工具调用 / usage）一个都不命中，整片丢掉。
    这条夹具的形状照抄 `test_stream_accumulator.py` 里那条现成的。
    """
    chunks = _drain_stream(_provider([_sdk(finish="length")]))
    got = [c.finish_reason for c in chunks if c.finish_reason]
    assert got == ["length"], (
        "收尾片的 finish_reason 被丢掉了（实得 %r）—— \"被截断\"就到不了下游" % (got,))
    print("  [ok] 只有 finish_reason 的收尾片 -> 照样有一片带它出去")


def test_a_reasoning_only_truncation_still_carries_finish_reason():
    """就是"只有思考、然后被截断"的形状。"""
    chunks = _drain_stream(_provider([
        _sdk(types.SimpleNamespace(content=None, reasoning_content="想啊想",
                                   tool_calls=None)),
        _sdk(finish="length"),
    ]))
    assert any(c.reasoning_content for c in chunks), "思考增量没出来，夹具不对"
    got = [c.finish_reason for c in chunks if c.finish_reason]
    assert got == ["length"], (
        "'只有思考被截断'这一类信号传不出去（实得 %r）—— 而实际那一次正是这一类"
        % (got,))
    print("  [ok] 只有思考的截断 -> 信号照样传得出去（实际那一次的形状）")


def test_the_sync_provider_records_finish_reason():
    """同步那条路必须填上这个字段（流式一直有填）。"""
    msg = types.SimpleNamespace(content="", tool_calls=None, reasoning_content=None)
    p = _provider(chunks="length", message=msg)
    resp = p.chat([{"role": "user", "content": "hi"}], None)
    assert resp.finish_reason == "length", (
        "同步路的 ChatResponse 没带上 finish_reason（实得 %r）" % (resp.finish_reason,))
    print("  [ok] 同步路的 ChatResponse 带上了 finish_reason")


# ------------------------------------------------------------------

def _main():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))
    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
