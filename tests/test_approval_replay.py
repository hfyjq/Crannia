"""审批：同意 = 零消息 + 挂起即停车。

## 这两条治的是什么

判据：同意 = 零消息 ——
工具在派发之前被拦住，放行之后模型只拿到结果，不知道中间有过审批。

反过来的形状：工具**立刻返回 `pending_*`**，这一轮结束，用户的回答
变成下一轮的用户消息，而框架还得写一句
「[系统] 用户已同意… 请重新提交」，是我们主动告诉它的。

挂起之后模型可能做的事有几种：绕开（改口说不需要审批）或
在原地等（说"我需要你的批准"）。这两种机械形状完全一样，
唯一的差别是模型嘴上那句话 —— 判不了。必须从结构上消灭它。

## 两条措施（缺一不可）

* 挂起即停车 —— 本批出现待审批，本轮就地停下（整批跑完再停，
  否则同一批里后面的 `tool_call` 悬空，上游 400）。模型绕不过去，
  也就无处可说。
* 同意 = 零消息 —— 框架替模型把那次调用做完，把真结果
  就地替换回那条 `role=tool` 消息，然后不带用户消息继续
  （`user_message=None`）。拒绝同样只是「那次调用回了个拒绝」。

**前提是「返回 `pending_*` 的那一刻什么都没做」** —— 那是本仓库审批的定义。
重放执行的是那次被拦下来的调用本身，不是第二次。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_approval_replay.py
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import bus.registry as R  # noqa: E402
import core.agent_loop as AL  # noqa: E402
from core.approval import pending_payload  # noqa: E402

SESS = "replay:private:probe"

# 模式影响派发，本文件必须自己钉模式。
# 不钉的话会继承开发机 `data/settings.json` 里的 `default_mode`：
# `auto`，闸去调分类器（吃掉 fake provider 的脚本）· `plan`，写工具被拒。
# 同一份测试在不同设置下结果不同，而它们一个字都不该关心模式。
from _sandbox import neutralise_settings  # noqa: E402
neutralise_settings()

# ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉。

_SCHEMA = {"description": "probe", "inputSchema": {"type": "object"}}


# ------------------------------------------------------------------
# 假 provider（形状照抄 test_agent_brakes.py，不另发明）
# ------------------------------------------------------------------

class _Resp:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
        self.prompt_tokens = 10
        self.completion_tokens = 5
        self.reasoning_content = None

    @property
    def total_tokens(self):
        return self.prompt_tokens + self.completion_tokens


class _Provider:
    """记被调了几次 —— 「循环停没停」最可数的证据就是它。"""

    def __init__(self, script):
        self._script = script
        self.calls = 0

    def chat(self, messages, tools=None, system=None, thinking=None):
        i = self.calls
        self.calls += 1
        return self._script(i, messages)


def _tc(name, args_json="{}", call_id="c1"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args_json}}


# ------------------------------------------------------------------
# 假工具
# ------------------------------------------------------------------

_PLAIN_CALLS = []


def _pending_payload_dict(subject="echo hi"):
    """一条真形状的执行审批载荷（`status` 必须 `pending_` 开头）。"""
    return pending_payload(
        "command", body="Crannia 想执行以下命令：\n%s" % subject,
        footer=False, ask_index=1,
        status="pending_command_approval", subject=subject, detail="",
    )


def _pending_payload(subject="echo hi"):
    """进历史的那份字符串（`role=tool` 的 `content` 就是字符串）。"""
    return json.dumps(_pending_payload_dict(subject), ensure_ascii=False)


_HANDLERS = {}


def _gate_handler(**_kw):
    """会挂起的那一个：返回待审批载荷，什么都不做。

    返回 dict（不是 JSON 串）是刻意的：`as_text` 对不在
    `PLAIN_TEXT_TOOLS` 名单里的工具会把返回 `json.dumps` 一遍 —— 交一个字符串
    出去会被再加一层引号，于是载荷不再以 `{` 开头、检测器认不出来。
    这是这条测试最容易踩的坑。
    """
    return _pending_payload_dict()


def _ok_handler(**_kw):
    _PLAIN_CALLS.append(_kw)
    return {"status": "done"}


def _doc_handler(**_kw):
    """名字落在读家族（`get_`），结果必须被隔离包裹。"""
    return "SECRET BODY"


def _mention_handler(**_kw):
    """正文里恰好提到 `pending_approval` —— 但不是载荷。

    这条是判据的负对照：检测器要是写成子串检查，它就会把这一条当成
    待审批（那正是 `_parse_approval_payload` 的 docstring 警告的形状）。
    """
    return "grep -rn pending_approval ."


_HANDLERS.update({
    "probe_gate": _gate_handler,
    "probe_ok": _ok_handler,
    "get_probe_doc": _doc_handler,
    "probe_mention": _mention_handler,
})

_BOOTED = False


def _boot():
    global _BOOTED
    if _BOOTED:
        return
    for name, fn in _HANDLERS.items():
        R.register_tool(name, fn, _SCHEMA)
    # 执行侧查的是暴露集（`bus.registry.is_tool_active`）——
    # 不登记的话它们一律「没呈现给你」。
    R.note_discovered(SESS, list(_HANDLERS))
    _BOOTED = True


def _tool_msg(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _history(call_id="c1", content=None, name="probe_gate", args="{}"):
    """一条「发出去并挂了起」的历史：assistant(带 tool_calls) + tool(载荷)。"""
    return [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [_tc(name, args, call_id)]},
        _tool_msg(call_id, content if content is not None else _pending_payload()),
    ]


# 1. 挂起，本轮就地停下

def test_a_pending_stops_the_turn():
    """承重：挂起之后不许再调模型 —— 那正是「模型绕过去」的那半。

    负对照是下一条（普通工具不停）。两条成对才说明是挂起让它停的。
    """
    _boot()

    def script(i, _m):
        if i == 0:
            return _Resp(content="", tool_calls=[_tc("probe_gate", "{}", "cg")])
        return _Resp(content="SECOND TURN")

    p = _Provider(script)
    out, msgs, _c, _t = AL.agent_loop(
        "go", p, messages=[{"role": "user", "content": "go"}],
        max_turns=10, session_id=SESS)

    assert p.calls == 1, (
        "挂起之后还是调了 %d 次模型 —— 停车没生效" % p.calls)
    assert out == "", "停车的这一轮不该有正文，实得 %r" % out
    last = msgs[-1]
    assert last.get("role") == "tool", "最后一条该是那条挂起的 tool 结果"
    assert "pending_command_approval" in last.get("content", ""), last


def test_a_plain_tool_does_not_stop_the_turn():
    """负对照：没挂起时循环照常往下走（否则上一条绿的没有意义）。"""
    _boot()
    _PLAIN_CALLS.clear()

    def script(i, _m):
        if i == 0:
            return _Resp(content="", tool_calls=[_tc("probe_ok", "{}", "c9")])
        return _Resp(content="SECOND TURN")

    p = _Provider(script)
    out, _msgs, _c, _t = AL.agent_loop(
        "go", p, messages=[{"role": "user", "content": "go"}],
        max_turns=10, session_id=SESS)
    assert p.calls == 2, "普通工具之后循环没继续，实得 %d 次" % p.calls
    assert out == "SECOND TURN", out
    assert _PLAIN_CALLS, "工具压根没被执行"


def test_merely_mentioning_the_word_does_not_stop_the_turn():
    """判据不是子串检查 —— 正文里出现 `pending_approval` 不算挂起。

    `_parse_approval_payload` 的 docstring 逐字写着这条理由：子串检查会被
    命令输出里恰好提到那个词打中（`grep -rn pending_approval .`）。
    """
    _boot()

    def script(i, _m):
        if i == 0:
            return _Resp(content="", tool_calls=[_tc("probe_mention", "{}", "cm")])
        return _Resp(content="SECOND TURN")

    p = _Provider(script)
    out, _msgs, _c, _t = AL.agent_loop(
        "go", p, messages=[{"role": "user", "content": "go"}],
        max_turns=10, session_id=SESS)
    assert p.calls == 2, (
        "正文里提到 `pending_approval` 被当成了挂起 —— 判据退回子串检查了")
    assert out == "SECOND TURN", out


# 2. 同意 = 把那次调用补上结果

def test_replay_runs_the_call_and_replaces_the_message():
    """承重：重放真的执行了那次调用，结果就地替换回那条消息。"""
    _boot()
    _PLAIN_CALLS.clear()
    msgs = _history(name="probe_ok")

    ok = AL.replay_pending_call(
        msgs, call_id="c1", tool_name="probe_ok", raw_args="{}",
        session_id=SESS, max_chars=6000)

    assert ok is True, "重放该成功"
    assert len(_PLAIN_CALLS) == 1, "那次调用没有被真正执行"
    assert json.loads(msgs[2]["content"]) == {"status": "done"}, (
        "那条 tool 消息没被替换，实得 %r" % msgs[2]["content"])
    assert "pending" not in msgs[2]["content"], "载荷还留在历史里"


def test_the_replay_result_goes_through_the_same_formatter():
    """重放走的是同一个 `_format_tool_result` —— 否则隔离就漏了。

    这条绿 = 「重放不是另开一条渲染路」；红了 = 隔离那条洞在重放这条路上重开
    （一个 `get_` 家族的工具，结果没被 `<user_document>` 包住）。
    """
    _boot()
    msgs = _history(name="get_probe_doc")
    ok = AL.replay_pending_call(
        msgs, call_id="c1", tool_name="get_probe_doc", raw_args="{}",
        session_id=SESS, max_chars=6000)
    assert ok is True
    assert "<user_document" in msgs[2]["content"], (
        "重放的结果没走隔离包裹，实得 %r" % msgs[2]["content"])


def test_deny_replaces_the_same_message():
    """拒绝也不是一条新的用户消息 —— 它是回给那次调用的答复。"""
    _boot()
    msgs = _history()
    ok = AL.deny_pending_call(msgs, call_id="c1", message="[系统] 用户拒绝了。")
    assert ok is True
    assert msgs[2]["content"] == "[系统] 用户拒绝了。"
    assert not any(m.get("role") == "user" and "拒绝" in str(m.get("content"))
                   for m in msgs), "拒绝被写成了用户消息"


def test_replay_reports_failure_when_the_message_is_gone():
    """退路：找不到那条消息，返回 `False`（调用方据此退回旧回执）。

    不许静默成功 —— 那会让「用户批了」变成「什么都没发生」。
    """
    _boot()
    assert AL.replay_pending_call(
        [], call_id="nope", tool_name="probe_ok", raw_args="{}",
        session_id=SESS, max_chars=6000) is False
    assert AL.deny_pending_call([], call_id="nope", message="x") is False


# 3. `user_message=None`，不追加用户消息

def test_none_means_no_user_message_is_appended():
    """判据是 `is None`，不是「假值」 —— 这一条钉的就是那个区别。

    审批回答那一轮的整个意义就是：历史里只有那条补好的结果，
    模型不该再看到任何「你被批准了」的话。
    """
    _boot()
    p = _Provider(lambda i, m: _Resp(content="ok"))
    msgs = _history(name="probe_ok", content="done")
    out, after, _c, _t = AL.agent_loop(
        None, p, messages=msgs, max_turns=5, session_id=SESS)

    users = [m for m in after if m.get("role") == "user"]
    assert len(users) == 1, (
        "`user_message=None` 还是追加了用户消息（%d 条）" % len(users))
    assert users[0]["content"] == "go", users[0]
    assert out == "ok", out


def test_an_empty_string_still_appends():
    """负对照：`""` 是一条空消息，与 `None` 是两件事。

    这条红了说明有人把判据改成了 `if not user_text:` —— 那是顺手扩大
    了一处语义（空消息被吞掉），而它和本节要解决的事毫无关系。
    """
    _boot()
    p = _Provider(lambda i, m: _Resp(content="ok"))
    _out, after, _c, _t = AL.agent_loop(
        "", p, messages=_history(name="probe_ok", content="done"),
        max_turns=5, session_id=SESS)
    users = [m for m in after if m.get("role") == "user"]
    assert len(users) == 2, "空串被当成了 None（判据写成 `if not user_text` 了）"


def test_the_stream_path_matches_the_sync_one():
    """锁步：流式那条路上这两个判据必须逐字同义。

    不跑流式那一整条（它要 `chat_stream` 替身，`test_stream_brakes`
    已经把锁步钉在 AST 上了）—— 这里只把两处的写法对一遍，
    红了说明有人只改了一条路。
    """
    import ast
    src = (Path(_PROJECT_ROOT) / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen = {"user_text_none": 0, "saw_pending": 0, "pending_check": 0}
    for node in ast.walk(tree):
        # 取载荷那一步是赋值（`_pend = _parse_approval_payload(...)`），
        #   不是 `if ... is not None:` —— 调用方要把载荷换掉、而不是置位。
        #判据是那次赋值；两处（sync + stream）少一处就是
        #   "只改了一条路"。
        #   [!] 它必须在 `isinstance(node, ast.If)` 那一支外面 ——
        #   `Assign` 节点永远进不去那一支（插在里面会恒为 0）。
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_pend"
                and isinstance(node.value, ast.Call)
                and ast.unparse(node.value).startswith("_parse_approval_payload(")):
            seen["pending_check"] += 1
        if isinstance(node, ast.If):
            try:
                cond = ast.unparse(node.test)
            except Exception:            # pragma: no cover
                continue
            if cond == "user_text is not None":
                seen["user_text_none"] += 1
            if cond == "_saw_pending":
                seen["saw_pending"] += 1
    assert seen["user_text_none"] == 2, (
        "`user_text is not None` 该在 sync + stream 各出现一次，实得 %d" % seen["user_text_none"])
    assert seen["saw_pending"] == 2, (
        "`_saw_pending` 的停车闸该有 2 处（sync + stream），实得 %d" % seen["saw_pending"])
    assert seen["pending_check"] == 2, (
        "取载荷那一步该有 2 处（sync + stream），实得 %d"
        "（判据：`_pend = _parse_approval_payload(tool_result_content)`）"
        % seen["pending_check"])


# 4. 结算器：把「哪一次调用」交出去

class _S:
    def __init__(self, key=SESS, platform=""):
        self.platform = platform
        self._key = key

    def __str__(self):
        return self._key


def _ctx():
    from core.pipeline.stage import StageContext
    return StageContext(session=_S(), user_message="")


def _pending_command_ctx():
    ctx = _ctx()
    ctx.pending_command = {
        "active": True, "stage": "ask", "kind": "command",
        "subject": "echo hi", "call_id": "c1",
        "tool_name": "run_command", "arguments": '{"command": "echo hi"}',
    }
    ctx.messages = _history(name="run_command", args='{"command": "echo hi"}')
    return ctx


def test_the_settler_hands_the_call_over_to_the_replay():
    """同意，`ctx.approval_replay` 带着那一次调用（名字 + 原参数）。"""
    from core.pipeline.stages.pre_process import _dispatch_approval

    ctx = _pending_command_ctx()
    _dispatch_approval("1", ctx)

    rp = ctx.approval_replay
    assert rp, "同意之后没交出重放信息 —— 退回了「再开一轮」"
    assert rp["call_id"] == "c1", rp
    assert rp["name"] == "run_command", rp
    assert rp["arguments"] == '{"command": "echo hi"}', rp
    # 退路必须同时留着：补不成时它就是旧回执。
    assert rp["fallback"].startswith("[系统] 用户已同意"), rp


def test_the_settler_hands_a_denial_too():
    """拒绝，同样的形状，但带的是 `deny`（不重放）。"""
    from core.pipeline.stages.pre_process import _dispatch_approval

    ctx = _pending_command_ctx()
    _dispatch_approval("3", ctx)

    rp = ctx.approval_replay
    assert rp, "拒绝之后没交出去"
    assert rp["call_id"] == "c1", rp
    assert rp["deny"], "拒绝没带拒绝语"
    assert "name" not in rp, "拒绝不该带 `name` —— 那会让它被当成同意去重放"


def test_the_call_info_is_read_back_from_the_assistant_message():
    """名字与参数只存在于上面那条 `assistant.tool_calls` 里。

    `role=tool` 消息自己只带一个 `tool_call_id`，抠不出来就只能退回旧回执
    （那等于这一批白做）。这条钉住那个往回找的动作。
    """
    from core.pipeline.stages.agent_loop import _call_info_for

    msgs = _history(name="probe_ok", args='{"a": 1}')
    call_id, name, args = _call_info_for(msgs, 2)
    assert (call_id, name, args) == ("c1", "probe_ok", '{"a": 1}'), \
        (call_id, name, args)

    # 负对照：id 对不上，明说抠不出来，不是猜一个。
    msgs[1]["tool_calls"][0]["id"] = "zzz"
    assert _call_info_for(msgs, 2) == ("c1", "", ""), "id 对不上却抠出了东西"


# 5. 重放改的是一条旧消息，常规落库写不出去，必须整表重写

def _temp_memory_manager():
    """一个真的 `MemoryManager`，落在一个临时目录里。"""
    from _sandbox import tempdir
    from core.memory.manager import MemoryManager
    return MemoryManager({"memory": {"storage_path": str(tempdir("lv_replay_"))}},
                         provider=None)


def test_w69_append_only_save_loses_an_edited_old_message(tmp_path=None):
    """先钉住那个洞：常规落库是只追加，改旧消息写不出去。

    这条红了有两种可能，两种都要看：① `repersist_messages` 坏了；
    ② 落库不再是只追加了（那「只追加」这个前提就没了，该回来翻这条与注释）。
    """
    mm = _temp_memory_manager()
    sid = "t:private:w69"
    mm.short_term.init_session(sid)
    pending = json.dumps({"status": "pending_command_approval"}, ensure_ascii=False)
    msgs = _history(name="probe_ok", content=pending)
    mm.short_term.save_messages(sid, msgs, [0] * len(msgs))

    msgs[2]["content"] = "REAL RESULT"          # ← 重放干的就是这一下
    assert mm.short_term.load_messages(sid)[2]["content"] == pending, (
        "改一条旧消息居然自动落库了 —— 那根因（只追加）已经不成立，"
        "请回来重读 `repersist_messages` 的 docstring")

    mm.repersist_messages(sid, msgs)            # ← 这才是修法
    back = mm.short_term.load_messages(sid)
    assert back[2]["content"] == "REAL RESULT", back[2]["content"]
    assert len(back) == 3, "整表重写把行数改了：%d" % len(back)
    assert back[2].get("tool_call_id") == "c1", "`tool_call_id` 在重写里丢了"
    assert back[1].get("tool_calls"), "`assistant.tool_calls` 在重写里丢了"


def test_w69_the_stage_writes_the_replay_back_to_disk():
    """接线：重放之后，库里那条 tool 消息必须是真结果。

    这条是端到端的那一头 —— 它红了 = `AgentLoopStage` 忘了调 `_persist_replay`，
    而症状（下一轮装载回来两条假 pending）在当轮完全看不见。
    """
    import asyncio
    _boot()
    from core.pipeline.stage import StageContext
    from core.pipeline.stages.agent_loop import AgentLoopStage

    mm = _temp_memory_manager()
    sid = "t:private:w69b"
    mm.short_term.init_session(sid)
    pending = json.dumps(_pending_payload_dict(), ensure_ascii=False)
    msgs = _history(name="probe_ok", content=pending)
    mm.short_term.save_messages(sid, msgs, [0] * len(msgs))

    class _Pool:
        def maybe_trim_cold_window(self, _sid, m):
            return m

    ctx = StageContext(session=_S(key=sid), user_message="")
    ctx.config = {}
    ctx.session_id = sid
    # `_persist_replay` 要的是存储键，所以手搭的 ctx
    #    必须把这一格也填上 —— 否则它会静默地不落库（这条测试就是抓它的）。
    ctx.storage_id = sid
    ctx.messages = msgs
    ctx.memory_context = ""
    ctx.session_pool = _Pool()
    ctx.provider = _Provider(lambda i, m: _Resp(content="ok"))
    ctx.memory_manager = mm
    ctx.approval_replay = {"call_id": "c1", "name": "probe_ok",
                           "arguments": "{}", "fallback": "FALLBACK"}

    asyncio.run(AgentLoopStage().process(ctx))

    assert ctx.user_message is None, "重放成功了却还是给了用户消息：%r" % ctx.user_message
    back = mm.short_term.load_messages(sid)
    assert back[2]["content"] != pending, "库里那条还是 pending —— 没写回"
    assert json.loads(back[2]["content"]) == {"status": "done"}, back[2]["content"]


# 6. **没人会回答的 `pending_*` 要收尾**

def _two_pendings_history():
    """一条 assistant 消息里两次调用都撞审批。"""
    return [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "",
         "tool_calls": [_tc("run_command", '{"command": "echo A"}', "cA"),
                        _tc("run_command", '{"command": "echo B"}', "cB")]},
        _tool_msg("cA", _pending_payload("echo A")),
        _tool_msg("cB", _pending_payload("echo B")),
    ]


def test_w68_a_batch_with_two_pendings_neutralizes_the_earlier_one():
    """承重：同批两条挂起，武装最后一条，更早那条换成一句实话。

    倒着扫 + 碰到第一条就停，被武装的必然是 `cB`；而 `cA` 那条
    没有任何东西会回来回答它（它的下标在本轮界线之内，可下一轮只扫
    下一轮新增的）。它就那样躺在历史里冒充"正在等回答"，模型再发一次，多弹一次窗。
    """
    from core.pipeline.stages.agent_loop import (
        NEUTRALIZED_PENDING_NOTE, _check_cross_zone_pending,
    )

    msgs = _two_pendings_history()
    ctx = _ctx()
    ctx.messages = msgs

    neutralized = _check_cross_zone_pending(ctx, 0)

    assert ctx.pending_command.get("call_id") == "cB", (
        "武装的不是最后那一条：%r" % ctx.pending_command.get("call_id"))
    assert msgs[3]["content"] != NEUTRALIZED_PENDING_NOTE, (
        "把**正在等回答**的那一条也中和了 —— 浮窗会跟着变成一句假话")
    assert msgs[2]["content"] == NEUTRALIZED_PENDING_NOTE, (
        "更早那条孤儿 pending 没被收尾，实得 %r" % msgs[2]["content"][:80])
    assert neutralized == [2], neutralized


def test_w68_the_armed_one_survives_even_when_its_call_id_is_lost():
    """`keep_index` 兜底：抠不到 `call_id` 时不许把正主一起中和。

    典型形状是历史被压缩过的老会话：那条 `assistant.tool_calls` 不在了，
    `_note_pending_call` 什么都不写，`ctx.pending_*` 里没有 `call_id`
光看"在不在 live 集合里"会把它判成死的。而它恰恰是唯一活着的那条。
    """
    from core.pipeline.stages.agent_loop import (
        NEUTRALIZED_PENDING_NOTE, _check_cross_zone_pending,
    )

    payload = _pending_payload("echo X")
    msgs = [{"role": "user", "content": "go"},
            _tool_msg("cX", payload)]          # ← 没有对应的 assistant 消息
    ctx = _ctx()
    ctx.messages = msgs

    neutralized = _check_cross_zone_pending(ctx, 0)

    assert ctx.pending_command.get("active") is True, "压根没武装"
    assert msgs[1]["content"] == payload, (
        "把正主也中和了（浮窗还在问，历史里却说没执行）")
    assert neutralized == [], neutralized


def test_w68_a_stale_pending_from_before_the_window_is_neutralized():
    """「非数字 = 打断」丢掉的那条陈旧 pending，同样没人会回答。

    判据不是"在不在本轮"，而是"`ctx.pending_*` 里有没有它" —— 所以这一条
    不需要另写一段代码，它长得和 ① 一模一样。
    """
    from core.pipeline.stages.agent_loop import (
        NEUTRALIZED_PENDING_NOTE, _check_cross_zone_pending,
    )

    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("run_command", "{}", "cOld")]},
            _tool_msg("cOld", _pending_payload("echo old")),
            {"role": "user", "content": "算了，先做别的"},
            {"role": "assistant", "content": "好的"}]
    ctx = _ctx()
    ctx.messages = msgs

    # 本轮只新增了最后一条，界线是 4
    neutralized = _check_cross_zone_pending(ctx, 4)

    assert neutralized == [2], neutralized
    assert msgs[2]["content"] == NEUTRALIZED_PENDING_NOTE, msgs[2]["content"][:80]
    assert not ctx.pending_command.get("active"), "陈旧那条不该被武装"


def test_w68_a_pending_the_framework_still_holds_is_spared():
    """负对照：框架真握着的那条不许动（否则浮窗和历史会互相打脸）。

    与上一条成对才有意义：两条的差别只有 `ctx.pending_*` 里那一个
    `call_id` —— 光看"在不在本轮"是分不出这两条的。
    """
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending

    payload = _pending_payload("echo old")
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("run_command", "{}", "cOld")]},
            _tool_msg("cOld", payload),
            {"role": "assistant", "content": "好的"}]
    ctx = _ctx()
    ctx.pending_command = {"active": True, "stage": "ask", "kind": "command",
                           "subject": "echo old", "call_id": "cOld"}
    ctx.messages = msgs

    neutralized = _check_cross_zone_pending(ctx, 3)

    assert neutralized == [], neutralized
    assert msgs[2]["content"] == payload, "把框架还握着的那条中和了"


def test_w68_the_replacement_is_not_itself_a_payload():
    """换上去的必须是普通文本，不是另一种载荷（否则下一轮又扫到它）。

    同族判据：`_parse_approval_payload` 是"这是不是载荷"的唯一入口，
    子串检查会被命令输出打中（`grep -rn pending_approval .`）。
    """
    from core.pipeline.stages.agent_loop import (
        NEUTRALIZED_PENDING_NOTE, _check_cross_zone_pending,
    )

    assert AL._parse_approval_payload(NEUTRALIZED_PENDING_NOTE) is None, (
        "中和语本身被当成了待审批载荷 —— 会自锁（每轮扫到自己）")
    msgs = _two_pendings_history()
    ctx = _ctx()
    ctx.messages = msgs
    _check_cross_zone_pending(ctx, 0)
    assert AL._parse_approval_payload(msgs[2]["content"]) is None


def test_w68_the_stage_persists_a_neutralized_stale_pending():
    """接线：中和掉的若是界线之前那条旧消息，必须整表重写回库。

    本轮新增的那些走常规落库（只追加）就写得出去了；而旧的那条写不出去
    —— 同一成因（只追加写不出旧消息）。不落库的症状下一轮才看得见：装载回来的
    还是那条假 pending。
    """
    import asyncio
    _boot()
    from core.pipeline.stage import StageContext
    from core.pipeline.stages.agent_loop import AgentLoopStage

    mm = _temp_memory_manager()
    sid = "t:private:w68"
    mm.short_term.init_session(sid)
    pending = _pending_payload("echo old")
    msgs = [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "",
             "tool_calls": [_tc("run_command", "{}", "cOld")]},
            _tool_msg("cOld", pending)]
    mm.short_term.save_messages(sid, msgs, [0] * len(msgs))

    class _Pool:
        def maybe_trim_cold_window(self, _sid, m):
            return m

    ctx = StageContext(session=_S(key=sid), user_message="")
    ctx.config = {}
    ctx.session_id = sid
    # `_persist_replay` 要的是存储键，所以手搭的 ctx
    #    必须把这一格也填上 —— 否则它会静默地不落库（这条测试就是抓它的）。
    ctx.storage_id = sid
    ctx.messages = msgs
    ctx.memory_context = ""
    ctx.session_pool = _Pool()
    ctx.provider = _Provider(lambda i, m: _Resp(content="ok"))
    ctx.memory_manager = mm

    asyncio.run(AgentLoopStage().process(ctx))

    back = mm.short_term.load_messages(sid)
    assert back[2]["content"] != pending, (
        "库里那条还是 pending —— 陈旧 pending 没落库，下一轮装载回来又是它")
    assert not ctx.pending_command.get("active"), "陈旧那条不该被武装"


# ------------------------------------------------------------------

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print("  [ok] %s" % t.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  FAIL  %s: %s" % (t.__name__, e))
    print("\n%d tests: %d passed, %d failed" % (passed + failed, passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
