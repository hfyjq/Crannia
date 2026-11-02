"""后台记忆抽取的机械读者 —— 闸序 / 游标 / 白名单 / 事件。

钉住落地时的每一条承重件：

  · 闸序（开关 → 在飞 → 快照 → 互斥 → 节流），每道闸一条用例；
    互斥与节流的方向相反：互斥跳过推游标、节流跳过不推；
  · 游标 = 条数 + 指纹：压缩重写，失配，互斥当"没写过"（回退全量会让
    陈年写入永久关掉抽取）、计数回退全量（两条兜底方向不同，别合并）；
  · fork：`_tool_allowlist` 执行侧拒（含 `_prefetch_readonly` 那条先执行的缝）、
    `tools=` 表不摘（缓存判据）、fork 的 messages 是副本（快照语义）；
  · 失败不出声地静默是禁止的：失败，出声 + 游标不推；
    成功，推游标 + `memory.saved` 事件（登记在 `__all__` + `DURABLE_TYPES`）；
  · 节流旋钮：读 `data/settings.json`，脏值（bool/越界/非整数）回默认 3。

全程 `sandbox()`（chdir 临时目录），记忆文件 / settings 全落临时目录，
绝不碰生产的 `data/memory/`。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_extract.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _sandbox import sandbox  # noqa: E402

import core.memory_extract as ME  # noqa: E402
from core import request_snapshot  # noqa: E402
from providers.base import StreamChunk  # noqa: E402

_SESS = "probe:memextract"


# ----------------------------------------------------------------------
# 夹具：假 provider / 假的 spawn 记录器 / 消息构造
# ----------------------------------------------------------------------

class _FakeProvider:
    """脚本化的同步 provider（形状与 `test_stream_brakes._SyncProvider` 一致）。"""

    _model = "probe-model"

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = []

    def chat(self, messages, tools, system=None, thinking=None):
        self.calls.append({"messages": list(messages), "tools": tools,
                           "system": system})
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if isinstance(step, Exception):
            raise step
        return types.SimpleNamespace(
            content=step.get("content", ""),
            tool_calls=step.get("tool_calls"),
            total_tokens=step.get("tokens", 5),
            reasoning_content=None,
        )


class _FakeStreamProvider:
    """脚本化的流式 provider（用来验证流式那条路与同步那条锁步）。"""

    _model = "probe-model"

    def __init__(self, script):
        self._script = list(script)
        self._i = 0

    async def chat_stream(self, messages, tools, system=None, thinking=None):
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if step.get("content"):
            yield StreamChunk(content=step["content"], finish_reason="stop")
        for i, tc in enumerate(step.get("tool_calls") or []):
            yield StreamChunk(tool_call={"id": tc["id"], "type": "function",
                                         "function": tc["function"]},
                              tool_call_index=i)
        yield StreamChunk(
            finish_reason="tool_calls" if step.get("tool_calls") else "stop",
            usage={"prompt_tokens": 1, "completion_tokens": 1})


class _SpawnRecorder:
    """替掉 `ME._spawn`：记录（或用同步方式执行）线程体。"""

    def __init__(self, run=False):
        self.calls = []
        self.run = run

    def __call__(self, *, target, kwargs):
        self.calls.append(kwargs)
        if self.run:
            target(**kwargs)
        return None


class _Ctx:
    """把 `ME._spawn` 换成记录器 + 清一次取消标志，跑完还原。"""

    def __init__(self, run=False):
        self.rec = _SpawnRecorder(run=run)

    def __enter__(self):
        self._old = ME._spawn
        ME._spawn = self.rec
        from core.cancellation import clear
        clear(_SESS)
        ME.forget(_SESS)          # 三格状态跨用例串味会把断言变成假绿
        return self.rec

    def __exit__(self, *exc):
        ME._spawn = self._old
        return False


def _mw_call(name, cid="call_1"):
    return {"id": cid, "type": "function", "function": {
        "name": "memory_write",
        "arguments": json.dumps(
            {"name": name, "description": "一句钩子", "content": "正文"},
            ensure_ascii=False)}}


def _snapshot(session_id=_SESS, tools=None):
    snap = request_snapshot.capture(
        payload=[{"role": "user", "content": "hi"}],
        tools=tools if tools is not None else [{"name": "memory_write"}],
        system="PROBE-SYSTEM", model="probe-model", session_id=session_id)
    request_snapshot.set_current(snap)
    return snap


def _msgs():
    return [{"role": "user", "content": "聊点什么"},
            {"role": "assistant", "content": "好的"}]


def _write_settings(cfg: dict) -> None:
    p = Path("data/settings.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


# ----------------------------------------------------------------------
# 1. 闸序（同步段）—— 每道闸一条
# ----------------------------------------------------------------------

def test_gate_switch_off_stops_everything():
    """① 开关关，连状态都不碰（除了 last_turn_at —— 它是每轮都记的钟）。"""
    with sandbox(), _Ctx() as rec:
        _write_settings({"memory_write": False})
        _snapshot()
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert rec.calls == [], "写记忆开关关着还去 fork 了"
        assert ME._state[_SESS]["turns_since"] == 0


def test_gate_in_flight_skips_this_turn():
    """② 在飞，本轮跳过（排队语义会引入"欠一轮"的状态，简化为跳过）。"""
    with sandbox(), _Ctx() as rec:
        _snapshot()
        ME._state[_SESS] = ME._blank()
        ME._state[_SESS]["in_progress"] = True
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert rec.calls == []
        assert ME._state[_SESS]["turns_since"] == 0, "在飞跳过不该动节流计数"


def test_gate_needs_this_sessions_snapshot():
    """③ 快照缺失 / 属于别的会话，跳过（fork 的同前缀判据拿不到输入）。"""
    with sandbox(), _Ctx() as rec:
        request_snapshot.clear_current()
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert rec.calls == [], "没有快照也 fork 了"
        _snapshot(session_id="别的会话")
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert rec.calls == [], "拿了别的会话的快照 fork 了"


def test_gate_mutex_advances_cursor_but_not_throttle():
    """④ 互斥：主 agent 写过，跳过 + 推游标，节流计数不动。"""
    with sandbox(), _Ctx() as rec:
        _snapshot()
        msgs = _msgs() + [{"role": "assistant", "content": "",
                           "tool_calls": [_mw_call("已经写过了")]}]
        ME.maybe_extract(session_id=_SESS, messages=msgs, provider=object())
        assert rec.calls == [], "主 agent 写过还 fork（互斥失效）"
        st = ME._state[_SESS]
        assert st["cursor_msgs"] == len(msgs), "互斥跳过必须推游标（消息已消化）"
        assert st["turns_since"] == 0, "互斥跳过不动节流计数"


def test_gate_throttle_counts_eligible_turns_only():
    """⑤ 节流：每 N 个合格轮一次；跳过不推游标、不计互斥轮。"""
    with sandbox(), _Ctx() as rec:
        _snapshot()
        #: 这里显式设 3：默认已经改成 20，而这条测的是"节流数对了没有"。
        _write_settings({"memory_extract_every_n_turns": 3})
        for _ in range(2):
            ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert rec.calls == [], "设为 3 轮时，前两轮不该 fork"
        st = ME._state[_SESS]
        assert st["cursor_msgs"] == 0, "节流跳过不许推游标（消息还没被消化）"
        # 第 3 轮：fork，计数归零
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert len(rec.calls) == 1, "第 3 个合格轮该 fork"
        assert ME._state[_SESS]["turns_since"] == 0, "跑完节流计数归零"
        assert ME._state[_SESS]["in_progress"] is True, (
            "_spawn 被替成不执行版 -> in_progress 保持真（由线程体收尾）")


def test_throttle_counter_resets_after_mutex_consumed_turn():
    """互斥"消化"的轮不算合格轮：2 个节流 + 1 个互斥，还差 1 轮才跑。"""
    with sandbox(), _Ctx() as rec:
        _snapshot()
        _write_settings({"memory_extract_every_n_turns": 3})
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        msgs = _msgs() + [{"role": "assistant", "content": "",
                           "tool_calls": [_mw_call("写过")]}]
        ME.maybe_extract(session_id=_SESS, messages=msgs, provider=object())
        assert rec.calls == [], "互斥轮不该触发 fork"
        # 第 4 次调用（turns_since 还是 2，到 3）才跑
        ME.maybe_extract(session_id=_SESS, messages=_msgs(), provider=object())
        assert len(rec.calls) == 1


# ----------------------------------------------------------------------
# 2. 游标 = 条数 + 指纹（两条兜底方向相反）
# ----------------------------------------------------------------------

def test_cursor_fingerprint_detects_rewrite():
    """压缩重写（删掉游标之前的内容），指纹失配，`_window` 回 None。"""
    msgs = _msgs() + [{"role": "user", "content": "第三"},
                      {"role": "assistant", "content": "第四"}]
    st = ME._blank()
    ME._advance(msgs[:2], st)                 # 游标落在第 2 条
    assert ME._window(msgs, st) == msgs[2:], "指纹对上时窗口是游标之后"
    rewritten = [{"role": "user", "content": "摘要"}, msgs[2]]   # 前两条被压成一条
    assert ME._window(rewritten, st) is None, "表被重写，指纹必须失配"


def test_cursor_fallback_directions_are_opposite():
    """失配时两条兜底方向相反（别合并）：
    互斥当"没写过"（回退全量 = 陈年写入永久关掉抽取）；
    计数回退全量（返回 0 = 抽取永久关掉）。"""
    msgs = [{"role": "user", "content": "很久以前"},
            {"role": "assistant", "content": "",
             "tool_calls": [_mw_call("陈年旧账")]},
            {"role": "user", "content": "现在"},
            {"role": "assistant", "content": "好"}]
    st = ME._blank()
    ME._advance(msgs, st)                     # 游标推到表尾（那条旧写入被消化过）
    rewritten = [{"role": "user", "content": "摘要"},
                 {"role": "user", "content": "现在"},
                 {"role": "assistant", "content": "好"}]        # 重写，失配
    assert ME._wrote_memory_since_cursor(rewritten, st) is False, \
        "互斥失配时回退全量 -> 陈年 memory_write 会把抽取永久关掉"
    assert ME._count_new(rewritten, st) == 3, "计数失配时必须回退全量"


# ----------------------------------------------------------------------
# 3. fork 形状：副本语义 / 白名单（执行侧）/ tools 表不摘
# ----------------------------------------------------------------------

def test_spawned_messages_are_a_copy():
    """fork 的消息表是轮末快照（深拷贝）—— 之后原表怎么长都不许影响它。"""
    with sandbox(), _Ctx() as rec:
        _snapshot()
        _write_settings({"memory_extract_every_n_turns": 3})
        ME._state[_SESS] = dict(ME._blank(), turns_since=2)
        live = _msgs()
        ME.maybe_extract(session_id=_SESS, messages=live, provider=object())
        assert len(rec.calls) == 1
        captured = rec.calls[0]["messages"]
        live.append({"role": "user", "content": "后来的话"})
        assert len(captured) == 2, "fork 拿到的是引用，之后的 append 漏进来了"


def test_allowlist_denies_execution_side():
    """白名单外的工具执行侧拒（tools= 照发整表 —— 摘表断缓存，不许摘）。"""
    with sandbox():
        import bus.registry as R
        from core.agent_loop import agent_loop
        from core.cancellation import clear
        R.reset()
        clear(_SESS)
        called = []
        R.register_tool("read_probe_a", lambda **kw: called.append(kw) or "ok",
                        {"description": "probe", "inputSchema": {"type": "object"}})
        R.note_discovered(_SESS, ["read_probe_a"])
        prov = _FakeProvider([
            {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_probe_a", "arguments": "{}"}}]},
            {"content": "收尾"},
        ])
        _out, out_msgs, _c, _t = agent_loop(
            user_message="x", provider=prov, messages=[],
            session_id=_SESS, _tools=[{"name": "read_probe_a"}],
            _system="SYS", _tool_allowlist=frozenset({"memory_read", "memory_search",
                                                      "memory_write"}),
            _emit=None)
        assert called == [], "白名单外的工具被真的执行了"
        tool_msgs = [m for m in out_msgs if m.get("role") == "tool"]
        assert tool_msgs and "only these tools may run" in tool_msgs[0]["content"], \
            "拒绝回执没有说清原因"


def test_allowlist_blocks_prefetch_gap():
    """预取跑在派发链之前 —— 白名单必须也在那里，否则
    "拒绝"之外还有一条先执行完的缝。两个只读工具，整批退回串行，拒。"""
    with sandbox():
        from core.agent_loop import _prefetch_readonly
        import bus.registry as R
        R.reset()
        called = []
        for nm in ("read_probe_a", "read_probe_b"):
            R.register_tool(nm, (lambda n: (lambda **kw: called.append(n) or "ok"))(nm),
                            {"description": "probe", "inputSchema": {"type": "object"}})
            R.note_discovered(_SESS, [nm])
        calls = [{"id": "c%d" % i, "type": "function", "function": {
            "name": nm, "arguments": "{}"}}
            for i, nm in enumerate(("read_probe_a", "read_probe_b"))]
        # 不带白名单：会并发预取（先证明这条缝真的存在 —— 否则下面是空跑）
        pre = _prefetch_readonly(calls, _SESS)
        assert len(pre) == 2, "夹具坏了：不带白名单时本该预取（这条用例在失去靶子）"
        called.clear()
        # 带白名单：整批退回串行，一条都不执行
        pre = _prefetch_readonly(calls, _SESS,
                                 frozenset({"memory_read", "memory_write"}))
        assert pre == {} and called == [], "白名单在预取那道缝上失效了"


def test_allowlist_stream_path_parity():
    """流式那条路也拒（同步/流式锁步；AST 读者在 `test_stream_brakes`，这里是行为件）。"""
    with sandbox():
        import bus.registry as R
        from core.cancellation import clear
        R.reset()
        clear(_SESS)
        called = []
        R.register_tool("read_probe_a", lambda **kw: called.append(kw) or "ok",
                        {"description": "probe", "inputSchema": {"type": "object"}})
        R.note_discovered(_SESS, ["read_probe_a"])
        prov = _FakeStreamProvider([
            {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_probe_a", "arguments": "{}"}}]},
            {"content": "收尾"},
        ])

        async def _drive():
            from core.agent_loop import agent_loop_stream
            last = None
            async for state in agent_loop_stream(
                    user_message="x", provider=prov, messages=[],
                    session_id=_SESS, _tools=[{"name": "read_probe_a"}],
                    _system="SYS",
                    _tool_allowlist=frozenset({"memory_write"}), _emit=None):
                last = state
            return last

        _text, out_msgs, _c, _t = asyncio.run(_drive())
        assert called == [], "流式路绕过了白名单（打开了逐字输出就没围栏了）"
        tool_msgs = [m for m in out_msgs if m.get("role") == "tool"]
        assert tool_msgs and "only these tools may run" in tool_msgs[0]["content"]


# ----------------------------------------------------------------------
# 4. 后台本体：成功 / 失败 / 无新记忆 / 事件
# ----------------------------------------------------------------------

def _run_probe(provider, msgs=None):
    """把 `run_extraction` 直接跑一遍（不走线程）。返回事件总线收到的 memory.saved 列表。"""
    from core import events as E
    got = []
    bus = E.EventBus()
    old = E._bus
    E.set_event_bus(bus)
    sub = bus.subscribe()
    try:
        ME.run_extraction(session_id=_SESS,
                          messages=msgs if msgs is not None else _msgs(),
                          provider=provider,
                          tools=[], system="SYS")
        # 收事件（emit 是同步投递进队列，直接翻）
        while True:
            try:
                ev = sub.q.get_nowait()
            except Exception:  # noqa: BLE001 —— 队列空了
                break
            if ev.type == E.EVT_MEMORY_SAVED:
                got.append(ev)
    finally:
        E.set_event_bus(old)
        bus.close()
    return got


def test_run_happy_path_writes_event_and_advances_cursor():
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        ME._state[_SESS] = ME._blank()
        msgs = _msgs()
        prov = _FakeProvider([
            {"tool_calls": [_mw_call("测试记忆")]},
            {"content": "无新记忆"},
        ])
        got = _run_probe(prov, msgs=msgs)
        # 记忆真的落进了沙箱里的记忆库
        from core import memory_store as MS
        assert (MS.FACTS_ROOT / "workspace" / "测试记忆.md").is_file(), \
            "memory_write 没有落到 facts 目录"
        # 事件：count=1、names 带 slug/name
        assert len(got) == 1 and got[0].data.get("count") == 1, got
        assert got[0].data.get("names") == ["测试记忆"], got[0].data
        # 游标推到表尾（消息被消化）；在飞标志清掉
        assert ME._state[_SESS]["cursor_msgs"] == len(msgs)
        assert ME._state[_SESS]["in_progress"] is False


def test_run_failure_keeps_cursor_and_reports():
    with sandbox():
        ME._state[_SESS] = ME._blank()
        lines = []
        prov = _FakeProvider([RuntimeError("上游炸了")])
        ME.run_extraction(session_id=_SESS, messages=_msgs(), provider=prov,
                          tools=[], system="SYS", status=lines.append)
        assert ME._state[_SESS]["cursor_msgs"] == 0, "失败必须不推游标"
        assert ME._state[_SESS]["in_progress"] is False, "失败也必须清在飞标志"
        assert any("失败" in ln for ln in lines), "失败没有出声"


def test_clear_midflight_does_not_write_into_the_new_session():
    """`/clear` 落在抽取在飞时：线程收尾推的游标该落回它出发时的那份状态，
    不许按会话号现取 —— 那会把旧游标写进新对话那一格。"""
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        ME._state[_SESS] = ME._blank()
        st = ME._state[_SESS]
        prov = _FakeProvider([{"content": "无新记忆"}])
        ME.forget(_SESS)                      # /clear 在飞（登记表里那条被拿掉）
        ME.run_extraction(session_id=_SESS, messages=_msgs(), provider=prov,
                          tools=[], system="SYS", st=st)
        assert _SESS not in ME._state, (
            "线程按会话号重建了一格状态 -> 新对话一开口就继承旧游标")
        assert st["in_progress"] is False, "孤儿状态对象上的在飞标志也该收掉"


def test_run_no_memory_write_no_event():
    with sandbox():
        ME._state[_SESS] = ME._blank()
        msgs = _msgs()
        prov = _FakeProvider([{"content": "无新记忆"}])
        got = _run_probe(prov, msgs=msgs)
        assert got == [], "没写记忆却发了 memory.saved"
        assert ME._state[_SESS]["cursor_msgs"] == len(msgs), "成功（无记忆）也该推游标"


def test_written_collector_counts_only_success():
    """收集判据是回执成功，不是"调用过"（被拒的调用不算数）。"""
    out = [
        {"role": "assistant", "content": "", "tool_calls": [_mw_call("写成", "c1"),
                                                            _mw_call("被拒", "c2")]},
        {"role": "tool", "tool_call_id": "c1",
         "content": json.dumps({"success": True, "slug": "写成"})},
        {"role": "tool", "tool_call_id": "c2",
         "content": json.dumps({"success": False, "error": "超限"})},
    ]
    assert ME._collect_written(out) == ["写成"]


def _history_write(name, cid="old1"):
    """一段已经发生过的成功写入（父会话历史里的），用来钉"新旧之辨"。

    `_msgs()` 里一条历史写入都没有；不自己造这一格的话，
    那条判据会靠夹具的形状通过，而不是因为代码对。
    """
    return [
        {"role": "assistant", "content": "", "tool_calls": [_mw_call(name, cid)]},
        {"role": "tool", "tool_call_id": cid,
         "content": json.dumps({"success": True, "name": name, "slug": name},
                               ensure_ascii=False)},
    ]


def test_history_writes_do_not_make_a_silent_run_look_successful():
    """历史里的成功写入不算这一次抽出来的。

    形状：父会话先自己写过 3 条，随后后台抽取只新写 2 条
    —— 事件却报「已保存 5 条」。
    这条钉的是更坏的那一面：历史里有过写入，哪怕 fork 一条没写，
    事件照样报"已保存"，于是 `"无新记忆"` 那一支永远走不到、
    `memory.saved` 失去判别力。
    """
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        ME._state[_SESS] = ME._blank()
        msgs = _msgs() + _history_write("旧条目")
        prov = _FakeProvider([{"content": "无新记忆"}])
        got = _run_probe(prov, msgs=msgs)
        assert got == [], (
            "fork 一条没写，却因为历史里有旧的成功写入而报了 memory.saved")


def test_only_writes_after_the_fork_are_reported():
    """反过来钉数目：历史 1 条 + fork 新写 1 条，报 1，不是 2。"""
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        ME._state[_SESS] = ME._blank()
        msgs = _msgs() + _history_write("旧条目")
        prov = _FakeProvider([
            {"tool_calls": [_mw_call("新条目", "new1")]},
            {"content": "无新记忆"},
        ])
        got = _run_probe(prov, msgs=msgs)
        assert len(got) == 1, got
        assert got[0].data.get("count") == 1, (
            "把历史里的旧写入也数进来了：%r" % (got[0].data,))
        assert got[0].data.get("names") == ["新条目"], got[0].data


# ----------------------------------------------------------------------
# 5. 节流旋钮 / 事件登记 / 提示词
# ----------------------------------------------------------------------

def test_bg_events_are_registered_for_persistence():
    """登记那半边：两个类型必须在 `__all__` 且在 `DURABLE_TYPES`。

    只登记 `__all__` 是不够的 —— 那样它进不了 jsonl，
    而"算区间重叠"正是这套东西存在的唯一理由。
    """
    from core import events as E
    # `__all__` 按 Python 约定装的是名字字符串，不是值 ——
    #    拿 `E.EVT_BG_START`（= `'bg.start'`）去比会红，而红的是断言不是代码。
    assert "EVT_BG_START" in E.__all__ and "EVT_BG_END" in E.__all__
    assert E.EVT_BG_START in E.DURABLE_TYPES, "bg.start 不落盘 -> 算不出重叠"
    assert E.EVT_BG_END in E.DURABLE_TYPES, "bg.end 不落盘 -> 查不出孤儿"
    # 刻意不进前端的 `EVT_TYPES`（`chat.html`）：它在 `task.end` 之后到达，
    #   若在前端开活动块就会变成没人收口。
    #   这条不在这个文件里钉（跨语言），由 `test_chat_fold` 正向钉住：
    #   「折叠器的 case ⊆ EVT_TYPES」—— 我们不往折叠器加 case，它自然不要求订阅。


def _collect_bg(prov, msgs):
    """跑一遍 `run_extraction`，把 `bg.start` / `bg.end` 两条收回来。"""
    from core import events as E
    got = []
    bus = E.EventBus()
    old = E._bus
    E.set_event_bus(bus)
    sub = bus.subscribe()
    try:
        ME.run_extraction(session_id=_SESS, messages=msgs, provider=prov,
                          tools=[], system="SYS")
        while True:
            try:
                ev = sub.q.get_nowait()
            except Exception:  # noqa: BLE001 —— 队列空了
                break
            if ev.type in (E.EVT_BG_START, E.EVT_BG_END):
                got.append((ev.type, ev.session_key, dict(ev.data)))
    finally:
        E.set_event_bus(old)
        bus.close()
    return got


def test_bg_span_is_a_pair_with_kind_and_session():
    """后台活的时间跨度成对发出，带 `kind` 与会话号。

    它在落盘之后才可算 —— 与 `task.start`/`task.end` 一起读，就能回答
    「后台活跟哪一轮撞过」。
    """
    with sandbox():
        ME._state[_SESS] = ME._blank()
        got = _collect_bg(_FakeProvider([{"content": "无新记忆"}]), _msgs())
        kinds = [t for t, _s, _d in got]
        assert kinds == ["bg.start", "bg.end"], kinds
        for _t, sess, data in got:
            assert sess == _SESS, "会话号没带上 —— 落盘之后就没法归因了"
            assert data.get("kind") == "extract", data
        assert got[1][2].get("duration_ms") is not None, \
            "bg.end 该带 duration_ms（否则还得自己配时间戳）"
        assert got[0][2].get("at") is not None, "bg.start 该带 at"


def test_bg_span_closes_even_when_the_fork_fails():
    """`bg.end` 必须在 `finally` 里 —— 这一条用失败路径钉它。

    为什么：不这样，「有 start 无 end」的孤儿分不清是"还在跑"还是
    "炸了没报"（同 `task.start` 落盘的那条判据）。成功路径谁都会过，
    失败路径才是这条断言存在的理由。
    """
    with sandbox():
        ME._state[_SESS] = ME._blank()
        got = _collect_bg(_FakeProvider([RuntimeError("boom")]), _msgs())
        assert [t for t, _s, _d in got] == ["bg.start", "bg.end"], \
            "fork 炸了，bg.end 没发 —— 那就是一条「有 start 无 end」的孤儿"


def test_throttle_reads_settings_with_default_fallback():
    with sandbox():
        assert ME._throttle_n() == ME.THROTTLE_DEFAULT, "没有 settings 时该回默认"
        _write_settings({"memory_extract_every_n_turns": 7})
        assert ME._throttle_n() == 7
        _write_settings({"memory_extract_every_n_turns": 1})
        assert ME._throttle_n() == 1
        # `0` 是合法值 = 不自动抽取（只靠模型自己写）。
        _write_settings({"memory_extract_every_n_turns": 0})
        assert ME._throttle_n() == 0, "0 应当读成「不自动抽取」"
        for bad in (True, False, "3", 3.0, -1, ME.THROTTLE_MAX + 1, None):
            _write_settings({"memory_extract_every_n_turns": bad})
            assert ME._throttle_n() == ME.THROTTLE_DEFAULT, \
                "脏值 %r 没回默认（isinstance(True,int) 那族坑）" % (bad,)


def test_event_registered():
    from core import events as E
    assert "EVT_MEMORY_SAVED" in E.__all__
    assert E.EVT_MEMORY_SAVED == "memory.saved"
    assert E.EVT_MEMORY_SAVED in E.DURABLE_TYPES, \
        "memory.saved 不落盘 ->「抽取真的发生过」没有可数的记录"


def test_prompt_carries_manifest_and_boundaries():
    p = ME.build_extract_prompt(9, "- [咖啡](咖啡.md) — 用户爱喝手冲")
    assert "9" in p and "咖啡" in p, "提示词里没有 N / 清单"
    assert "memory_write" in p and "不要记" in p
    p2 = ME.build_extract_prompt(3, "")
    assert "[咖啡]" not in p2, "空清单时不该出现清单段"


def test_prompt_has_forget_clause_but_never_promises_deletion():
    """「忘掉」那半句 + 绝不许记凭据。

    判据：用户明确要求记的立刻记；要求忘的要删掉对应条目。
    —— 只采纳了前半句（【不要记】末尾的「用户明确要求记的优先」）。

    后半句不能照字面抄：抽取器没有文件工具，
    `EXTRACT_ALLOWLIST` **只有 memory_read / memory_search / memory_write**
    —— 写 "find and remove" 就是空头支票（不承诺兑现不了的）。
    所以钉的是两件事：忘掉那半句在、且不承诺删除（指向设置页）。
    """
    p = ME.build_extract_prompt(5, "")
    for k in ("别记", "忘掉", "一个字都不许写"):
        assert k in p, "缺了「忘掉」那半句：%s" % k
    assert "没有删除工具" in p and "设置页" in p, \
        "说了忘掉却没说清怎么办 —— 会变成空头支票"
    # 凭据：全禁（任何来源、任何场景都不记）
    assert "凭据" in p and "API key" in p, "缺了凭据排除"
    assert "我们自己的加强" in p, "该如实标注来路（别把加强说成抄来的）"


def test_record_turn_schedules_extract_once():
    """`MemoryManager.record_turn` 里对 `maybe_extract` 的调用恰好一处。

    为什么要源级读者：整段被复制成两份，`maybe_extract` 每轮被调两次
    —— 节流计数翻倍（默认 3 → 实际约每 2 轮触发一次）、互斥轮在第二次调用里
    窗口已空而被误计、还多打一行「在飞跳过」。行为用例碰不到它
    （两条调用都走同一套闸，其中一道闸在测试里常缺席），
    这类"复制块"事故只有源级读者钉得住 —— 按 AST 数调用点。
    """
    import ast

    src = (_PROJECT_ROOT / "core" / "memory" / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "record_turn":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == "maybe_extract":
                    hits.append(sub.lineno)
    assert len(hits) == 1, (
        "record_turn 里 maybe_extract 的调用点有 %d 处（行 %r）—— 每轮只许"
        "调度一次（7b 复制块事故的回归判据）" % (len(hits), hits))


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def test_the_extractor_refuses_speculation_and_secondhand_identity():
    """三条同源的纪律 —— 都是「把不是事实的东西写成了事实」。

    三条都有真实失败案例打底（后台抽取器写的）：

    | | 它写下了什么 | 真相 |
    |---|---|---|
    | 推测 | 「搜不到是因为文件名带了个多余的右括号」 | 那是模型的归因；真因是索引没刷新 |
    | 二手身份 | 「用户自称身份证号为 11010519491231002X…」 | 那是探针里的假号（测试数据）|
    | 单次交互 | 「用户让『读一下报表』时期望结构化要点小结」等两条 | 都来自一次交互（一个样本）|

    这条钉的是"约束还在提示词里"，不是"模型一定照做" ——
    没有任何机械判据能证明模型不会那么写。它是减速带，不是证明（如实标出）。
    """
    from core.memory_extract import build_extract_prompt

    p = build_extract_prompt(10, "")
    for key, why in (
        ("你的推测不是事实", "归因/推测没被禁 —— 错误归因会被当成「已知」复用（#16）"),
        ("二手身份", "二手身份没被禁 —— 工具里读到的身份会被写成「用户自称」（#14）"),
        ("一次交互", "单次概括没被禁 —— 一个样本会被当成长期偏好（#15）"),
    ):
        assert key in p, "★ 提示词里少了这条约束：%s" % why

    # 反面：三条都必须落在【绝不许记】那一段里 ——
    #   那一段的判据逐字是"这一条压倒上面「该记什么」"。放前面就失效了。
    forbid = p.index("【绝不许记】")
    for key in ("你的推测不是事实", "二手身份", "一次交互"):
        assert p.index(key) > forbid, (
            "「%s」落在「绝不许记」**之前** —— 那一段的判据是「压倒上面」" % key)
    print("  [ok] 三条纪律都在，且都在「绝不许记」段里")


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
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
