# -*- coding: utf-8 -*-
"""`core/hooks.py` 的机械读者。

钉住：

1. 配置形状：事件 → `[{matcher, hooks: [{type: command, ...}]}]`；
   非命令钩子跳过并出声。
2. 匹配：正则；`*`/空 = 全部；工具名两套都试（我们的名字 + 常见别名）。
3. 退出码：2 = 拦下（stderr 当理由）；其他非零 = 不阻塞。
4. JSON 输出：`permissionDecision` / `updatedInput` / `additionalContext`。
5. 项目级信任门：默认不执行，`hooks_allow_project` 为真才执行。
6. Stop：可以拦停，且 `stop_hook_active` 会传下去。
7. 没有配置时 `active()` 为假（调用方据此关掉预取）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_hooks.py
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import hooks as H  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


class _Lab:
    def __init__(self, *, allow_project=False):
        self._tmp = None
        self.allow_project = allow_project

    def __enter__(self):
        from _sandbox import sandbox
        self._ctx = sandbox()
        self.root = self._ctx.__enter__()
        self.ws = H._workspace()
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.root / "data").mkdir(exist_ok=True)
        (self.root / "data" / "settings.json").write_text(
            json.dumps({"hooks_allow_project": bool(self.allow_project)}),
            encoding="utf-8")
        H.reset()
        return self

    def __exit__(self, *a):
        H.reset()
        self._ctx.__exit__(*a)

    def global_hooks(self, data):
        (self.root / "data" / "hooks.json").write_text(
            json.dumps(data), encoding="utf-8")
        H.reset()

    def project_hooks(self, data):
        (self.ws / ".crannia").mkdir(exist_ok=True)
        (self.ws / ".crannia" / "hooks.json").write_text(
            json.dumps(data), encoding="utf-8")
        H.reset()


def _hooks(event, command, matcher="", timeout=None):
    h = {"type": "command", "command": command}
    if timeout is not None:
        h["timeout"] = timeout
    return {"hooks": {event: [{"matcher": matcher, "hooks": [h]}]}}


def test_no_config_means_inactive():
    with _Lab():
        check("没有配置 => active 为假", H.active() is False)
        r = H.pre_tool("s", "read_file", {})
        check("没有配置 => 不拦", r["blocked"] is False and r["args"] is None, r)


def test_block_and_reason():
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "echo nope >&2; exit 2"))
        check("有配置 => active 为真", H.active() is True)
        r = H.pre_tool("s", "read_file", {"file_path": "a"})
        check("退出码 2 => 拦下", r["blocked"] is True, r)
        check("stderr 当理由", "nope" in r["reason"], r)


def test_matcher_and_aliases():
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "exit 2", matcher="Bash"))
        r = H.pre_tool("s", "run_command", {"command": "ls"})
        check("★ 别名匹配：Bash 命中 run_command", r["blocked"] is True, r)
        r = H.pre_tool("s", "read_file", {})
        check("不匹配的工具不受影响", r["blocked"] is False, r)

    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "exit 2", matcher=".*"))
        r = H.pre_tool("s", "anything", {})
        check("`.*` 匹配全部", r["blocked"] is True, r)


def test_updated_input_and_context():
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"file_path": "b.txt"},
    }})
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "cat >/dev/null; echo '%s'" % payload))
        r = H.pre_tool("s", "read_file", {"file_path": "a.txt"})
        check("updatedInput 返回给调用方", r["args"] == {"file_path": "b.txt"}, r)

    payload2 = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": "formatted ok",
    }})
    with _Lab() as lab:
        lab.global_hooks(_hooks("PostToolUse", "cat >/dev/null; echo '%s'" % payload2))
        r = H.post_tool("s", "write_file", {"file_path": "a"}, "done")
        check("additionalContext 收得到", r["context"] == "formatted ok", r)


def test_nonzero_is_not_blocking():
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "echo boom >&2; exit 1"))
        r = H.pre_tool("s", "read_file", {})
        check("退出码 1 => 不拦（非阻塞失败）", r["blocked"] is False, r)


def test_non_command_type_is_skipped():
    with _Lab() as lab:
        lab.global_hooks({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [
            {"type": "prompt", "prompt": "x"}]}]}})
        check("只有非命令钩子 => 视为没有", H.active() is False)
        r = H.pre_tool("s", "read_file", {})
        check("也不拦", r["blocked"] is False, r)


def test_project_trust_gate():
    with _Lab(allow_project=False) as lab:
        lab.project_hooks(_hooks("PreToolUse", "exit 2"))
        check("★ 默认不执行项目级钩子", H.active() is False)
        check("也不拦", H.pre_tool("s", "read_file", {})["blocked"] is False)

    with _Lab(allow_project=True) as lab:
        lab.project_hooks(_hooks("PreToolUse", "exit 2"))
        check("★ 打开信任后执行", H.active() is True)
        check("拦得住", H.pre_tool("s", "read_file", {})["blocked"] is True)


def test_stop_hook_and_flag():
    cmd = ('read x; case "$x" in *stop_hook_active*true*)'
           ' echo \'{"hookSpecificOutput":{"hookEventName":"Stop"}}\' ;;'
           ' *) echo keep-going >&2; exit 2 ;; esac')
    with _Lab() as lab:
        lab.global_hooks(_hooks("Stop", cmd))
        r = H.stop("s", False)
        check("Stop 可以拦", r["blocked"] is True and "keep-going" in r["reason"], r)
        r = H.stop("s", True)
        check("★ stop_hook_active 传下去了（第二次不拦）",
              r["blocked"] is False, r)


def test_timeout_does_not_block():
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "sleep 3; exit 2", timeout=1))
        r = H.pre_tool("s", "read_file", {})
        check("超时 => 不拦", r["blocked"] is False, r)


def test_substitution_of_project_dir():
    with _Lab() as lab:
        lab.global_hooks(_hooks(
            "PreToolUse",
            'test "${CLAUDE_PROJECT_DIR}" = "%s" || exit 2' % str(lab.ws)))
        r = H.pre_tool("s", "read_file", {})
        check("★ CLAUDE_PROJECT_DIR 被替换", r["blocked"] is False, r)


def test_user_prompt_submit():
    with _Lab() as lab:
        lab.global_hooks(_hooks("UserPromptSubmit", "echo 别写这个 >&2; exit 2"))
        r = H.user_prompt_submit("s", "帮我删库")
        check("UserPromptSubmit 可以拦", r["blocked"] is True, r)
        check("理由来自 stderr", "别写这个" in r["reason"], r)

    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "当前分支是 main",
    }})
    with _Lab() as lab:
        lab.global_hooks(_hooks("UserPromptSubmit",
                                "cat >/dev/null; echo '%s'" % payload))
        r = H.user_prompt_submit("s", "继续")
        check("附加的上下文收得到", r["context"] == "当前分支是 main", r)


def test_session_start_once_and_source():
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "项目规则已加载",
    }})
    cmd = "cat >/dev/null; echo '%s'" % payload
    with _Lab() as lab:
        lab.global_hooks(_hooks("SessionStart", cmd))
        r1 = H.session_start("s1", "startup")
        r2 = H.session_start("s1", "startup")
        check("第一次给上下文", r1["context"] == "项目规则已加载", r1)
        check("★ 同一个存储 id 只跑一次", r2["context"] == "", r2)
        H.note_session_source("s2", "clear")
        check("来源可以带出来", H.take_session_source("s2") == "clear")
        check("取一次就没了", H.take_session_source("s2") == "")


def test_post_tool_failure():
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUseFailure",
        "additionalContext": "检查一下编码",
    }})
    with _Lab() as lab:
        lab.global_hooks(_hooks("PostToolUseFailure",
                                "cat >/dev/null; echo '%s'" % payload))
        r = H.post_tool_failure("s", "read_file", {}, ValueError("boom"))
        check("失败后的上下文收得到", r["context"] == "检查一下编码", r)


def test_ask_becomes_pending_approval():
    """`ask` 不是拦下 —— 它变成一次待审批（kind `hook_ask`）。

    判据两半都要：
      · 先挂起、后干活 —— handler 一次都不许跑（跑过再问 = 用户批的
        那一步已经做完了）；
      · 载荷带得动重放需要的东西（`subject` + 改过的 `arguments`）。
    """
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": "这条命令要先确认",
        "updatedInput": {"text": "改过的参数"},
    }})
    called = []

    def handler(**kw):
        called.append(kw)
        return {"success": True}

    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse",
                                "cat >/dev/null; echo '%s'" % payload))
        r = H.pre_tool("s", "probe_tool", {"text": "原参数"})
        check("ask 不是拦下", r["blocked"] is False and r["ask"] is True, r)
        check("理由带得出来", "确认" in r["reason"], r)

        from core import agent_loop as AL
        _result, content, eff = AL._dispatch_generic_tool(
            {"id": "call_1"}, handler, {"text": "原参数"}, {}, [],
            "probe_tool", session_id="s")
        check("★★ 先挂起、后干活：handler 一次都没跑", called == [], called)
        check("用上的是改过的参数", eff == {"text": "改过的参数"}, eff)
        data = json.loads(content)
        check("载荷状态是 hook_ask",
              data.get("status") == "pending_hook_ask_approval", data)
        check("kind 是 hook_ask", data.get("kind") == "hook_ask", data)
        check("被审批物是工具名", data.get("subject") == "probe_tool", data)
        check("★ 改过的参数随载荷走（重放要用它）",
              json.loads(data.get("arguments") or "{}") == {"text": "改过的参数"},
              data.get("arguments"))
        check("正文里有人话理由", "确认" in str(data.get("body")), data.get("body"))

        # 子 agent 里不许挂起 —— 那一轮不存在，挂起 = 父子一起卡死。
        from core import subagent as SA
        with SA._DepthGuard():
            _r2, content2, _eff2 = AL._dispatch_generic_tool(
                {"id": "call_2"}, handler, {"text": "原参数"}, {}, [],
                "probe_tool", session_id="s")
        check("★★ 子在循环里碰到 ask => 拒 + 给路线（不是挂起）",
              "pending_hook_ask_approval" not in content2
              and "[审批结果] unavailable" in content2, content2[:120])
        check("★ 那一支同样没跑 handler", called == [], called)


def test_permission_request_can_answer_for_the_user():
    """`PermissionRequest`：弹窗之前问一次，`allow` / `deny` 都能答。"""
    allow = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "permissionDecision": "allow"}})
    deny = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "permissionDecision": "deny",
        "permissionDecisionReason": "这条不行"}})

    with _Lab() as lab:
        lab.global_hooks(_hooks("PermissionRequest",
                                "cat >/dev/null; echo '%s'" % allow))
        r = H.permission_request("s", "probe_tool", {},
                                 kind="hook_ask", subject="probe_tool")
        check("可以说 allow", r["allow"] is True and r["deny"] is False, r)

    with _Lab() as lab:
        lab.global_hooks(_hooks("PermissionRequest",
                                "cat >/dev/null; echo '%s'" % deny))
        r = H.permission_request("s", "probe_tool", {},
                                 kind="command", subject="ls")
        check("也可以 deny（带理由）",
              r["deny"] is True and "不行" in r["reason"], r)

    with _Lab() as lab:
        lab.global_hooks(_hooks("PermissionRequest", "exit 2"))
        r = H.permission_request("s", "probe_tool", {}, kind="command")
        check("退出码 2 => deny（与其余事件同一条规矩）", r["deny"] is True, r)

    with _Lab() as lab:
        # 负对照：只配了 PreToolUse —— 它那条 allow 不是 PermissionRequest
        lab.global_hooks(_hooks("PreToolUse",
                                "cat >/dev/null; echo '%s'" % allow))
        r = H.permission_request("s", "probe_tool", {}, kind="command")
        check("★ 没配这个事件就不表态（别的事件的 allow 不算）",
              r["allow"] is False and r["deny"] is False, r)


def test_hook_ask_settles_without_granting():
    """结算器：`1` 交给重放、`2` 带 `rejected` 头；两档都不发授权。"""
    from core.pipeline.stages import pre_process as PP

    def _pend():
        return {"active": True, "stage": "ask", "kind": "hook_ask",
                "subject": "probe_tool", "call_id": "c1",
                "tool_name": "probe_tool", "arguments": '{"text": "x"}'}

    class _C:
        user_message = None
        approval_replay = None

    granted = []
    orig = PP._grant_target
    PP._grant_target = lambda *a, **k: granted.append(a)
    try:
        c = _C()
        pend = _pend()
        PP._settle_hook_ask("1", pend, c)
        check("同意 => 交给重放",
              (c.approval_replay or {}).get("name") == "probe_tool",
              c.approval_replay)
        check("参数原样带过去（重放要用）",
              (c.approval_replay or {}).get("arguments") == '{"text": "x"}',
              c.approval_replay)
        check("★ 不发任何授权（重放直调 handler，不再走钩子）",
              granted == [], granted)
        check("挂起状态清空", pend == {}, pend)

        c2 = _C()
        PP._settle_hook_ask("2", _pend(), c2)
        check("不同意 => 带 rejected 头",
              "[审批结果] rejected" in (c2.user_message or ""), c2.user_message)
        check("不同意 => 不重放（是 deny，不是 name/arguments）",
              "deny" in (c2.approval_replay or {})
              and "name" not in (c2.approval_replay or {}), c2.approval_replay)
    finally:
        PP._grant_target = orig


def test_grant_for_kind_knows_its_limits():
    """「钩子替人答应」落成授权时，认不出就不发（调用方退回问人）。"""
    from core import agent_loop as AL

    check("hook_ask 不需要授权", AL._grant_for_kind("hook_ask", {}, "") is True)
    check("★ 认不出的 kind => 不发",
          AL._grant_for_kind("plan", {"subject": "x"}, "") is False)
    check("★ 拿不到被审批物 => 不发（fail-closed）",
          AL._grant_for_kind("command", {}, "") is False)


def test_cron_never_asks_the_permission_hook():
    """`cron` 那一轮不问钩子 —— 那条路已经有一套"没有人可以回答"的处置。"""
    allow = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "permissionDecision": "allow"}})
    called = []

    def handler(**kw):
        called.append(kw)
        return {"success": True}

    with _Lab() as lab:
        lab.global_hooks(_hooks("PermissionRequest",
                                "cat >/dev/null; echo '%s'" % allow))
        from core import agent_loop as AL
        # `hook_ask` 是不需要授权的那一档，非 cron 时它必定表态
        # （换成一个要授权的 kind，`wm` 不在测试里，两条都是 None，
        #  那条断言就恒真了）。
        pend = {"kind": "hook_ask", "subject": "probe_tool"}
        check("★ cron 那一轮不问钩子",
              AL._permission_hook_decide("s", "probe_tool", {}, "", pend,
                                         "cron", handler, [], 100) == (None, None))
        check("负对照：同一个钩子、非 cron 时会问（否则上面那条恒真）",
              AL._permission_hook_decide("s", "probe_tool", {}, "", pend,
                                         "", handler, [], 100)[0] is not None)
        check("负对照：它真的把那一步补跑了", called == [{}], called)


def test_permission_allow_rerun_goes_through_post_hooks():
    """补跑那一次也要过 `PostToolUse` —— 否则格式化钩子会时灵时不灵。"""
    allow = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "permissionDecision": "allow"}})
    post = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "post-ran"}})
    cfg = {"hooks": {
        "PermissionRequest": [{"matcher": "", "hooks": [
            {"type": "command", "command": "cat >/dev/null; echo '%s'" % allow}]}],
        "PostToolUse": [{"matcher": "", "hooks": [
            {"type": "command", "command": "cat >/dev/null; echo '%s'" % post}]}],
    }}
    with _Lab() as lab:
        lab.global_hooks(cfg)
        from core import agent_loop as AL
        content, _res = AL._permission_hook_decide(
            "s", "probe_tool", {}, "", {"kind": "hook_ask", "subject": "probe_tool"},
            "", lambda **k: {"success": True}, [], 6000)
        check("补跑的结果里带着 PostToolUse 的上下文",
              content is not None and "post-ran" in content, content)


def test_trust_needs_a_real_true():
    """信任门只认真布尔 `true`：字符串 `"true"` 不算。

    判据：`_project_allowed()` 是 `is True`。放进一个看起来像真的值
开关显示"已打开"而配置不执行（「标签在说谎」），而这一格说的
      正是"你的仓库里的脚本可以跑" —— 它不许猜。
    """
    import os
    import tempfile

    def _read_with(payload):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            try:
                os.chdir(d)
                if payload is not None:
                    (Path(d) / "data").mkdir()
                    (Path(d) / "data" / "settings.json").write_text(
                        json.dumps(payload), encoding="utf-8")
                H.reset()
                return H._project_allowed()
            finally:
                os.chdir(old)
                H.reset()

    check("没写过 => 不信任（默认关）", _read_with(None) is False)
    check("真布尔 true => 信任", _read_with({"hooks_allow_project": True}) is True)
    check("★ 字符串 \"true\" => **不**信任（别猜）",
          _read_with({"hooks_allow_project": "true"}) is False)
    check("★ 1 => **不**信任（`1 == True` 但 `1 is not True`）",
          _read_with({"hooks_allow_project": 1}) is False)


def test_special_tools_go_through_hooks():
    """特判工具（`run_subagent` / `run_workflow` / `ask_member`）也过钩子。

    它们不走 `_dispatch_generic_tool`（要快照 / provider / 那条 assistant 消息），
    而 `run_subagent` 的别名正是 `Task` / `Agent` —— 用户最常挂的那一格。
    """
    deny = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "不许派子"}})
    upd = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "updatedInput": {"task": "改过的"}}})
    post = json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": "post-ran"}})
    ran = []

    def _run(args):
        ran.append(args)
        return "子 agent 的结论"

    from core import agent_loop as AL

    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse",
                                "cat >/dev/null; echo '%s'" % deny, matcher="Task"))
        content, _args = AL._special_tool_result(
            "run_subagent", {"task": "x"}, "c1", "s", _run)
        check("★ 拦下 => **不派发**（子 agent 一次都没起）",
              ran == [] and content.startswith("[hook]"), (ran, content))

    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse",
                                "cat >/dev/null; echo '%s'" % upd, matcher="Task"))
        content, args = AL._special_tool_result(
            "run_subagent", {"task": "x"}, "c1", "s", _run)
        check("★ 别名匹配（`Task` → `run_subagent`）且 `updatedInput` 传下去",
              ran == [{"task": "改过的"}] and args == {"task": "改过的"},
              (ran, args))
        check("回执原样交回", content == "子 agent 的结论", content)

    with _Lab() as lab:
        lab.global_hooks({"hooks": {"PostToolUse": [{"matcher": "", "hooks": [
            {"type": "command", "command": "cat >/dev/null; echo '%s'" % post}]}]}})
        ran.clear()
        content, _args = AL._special_tool_result(
            "run_subagent", {"task": "x"}, "c1", "s", _run)
        check("★ `PostToolUse` 也过（追加的上下文进得去）",
              ran and "post-ran" in content, content)


def test_async_hook_never_blocks_and_never_decides():
    """`async: true`，起了就走：拦不住、也送不回上下文。

    判据两半：
      · 一条会退出码 2 的钩子标了 async，`pre_tool` 必须立刻回"不拦"，
        且不许等它（拿墙钟判：它睡 1.2 秒，而我们 0.5 秒内要拿到结果）；
      · 它真的跑了（留了个文件）—— 否则"不阻塞"可以靠"干脆不跑"骗过去。
    """
    import time as _t

    def _async_hooks(event, command, matcher=""):
        return {"hooks": {event: [{"matcher": matcher, "hooks": [
            {"type": "command", "command": command, "async": True}]}]}}

    with _Lab() as lab:
        marker = lab.root / "async_marker"
        lab.global_hooks(_async_hooks(
            "PreToolUse", 'sleep 1.2; echo ran > "%s"; exit 2' % marker))
        t0 = _t.monotonic()
        r = H.pre_tool("s", "probe_tool", {})
        elapsed = _t.monotonic() - t0
        check("★★ 不等它（< 0.5 秒就回来了）", elapsed < 0.5, "%.3fs" % elapsed)
        check("★★ 而且**不拦**（退出码 2 在异步这条路上不算数）",
              r["blocked"] is False and r["ask"] is False, r)
        # 等后台那一条跑完（守护线程，绝不等太久）
        for t in list(H._async_threads):
            t.join(5.0)
        check("★ 它**真的跑了**（否则'不阻塞'可以是靠不跑骗来的）",
              marker.exists(), str(marker))

    # 负对照：同一条钩子不写 async，真的拦（证明上面那条判的是 async）
    with _Lab() as lab:
        lab.global_hooks(_hooks("PreToolUse", "exit 2"))
        check("负对照：同步那一条照旧拦得下", H.pre_tool("s", "probe_tool", {})["blocked"] is True)


def test_session_start_forces_sync():
    """`SessionStart` 标了 async 也照样等 —— 它的上下文要进模型。"""
    payload = json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart", "additionalContext": "项目规则"}})
    cfg = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "cat >/dev/null; echo '%s'" % payload,
         "async": True}]}]}}
    with _Lab() as lab:
        lab.global_hooks(cfg)
        r = H.session_start("s1", "startup")
        check("★★ 异步也等得到上下文（强制同步）",
              r["context"] == "项目规则", r)


def test_both_loops_ask_the_permission_hook():
    """锁步：两条循环都要过同一条 `_permission_hook_apply`。"""
    import ast

    src = (_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    text_of = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            text_of[node.name] = ast.get_source_segment(src, node) or ""
    for fn in ("agent_loop", "agent_loop_stream"):
        check("%s 过了 PermissionRequest" % fn,
              text_of.get(fn, "").count("_permission_hook_apply(") == 1,
              text_of.get(fn, "").count("_permission_hook_apply("))
        check("%s 仍然**重判**挂起（补跑也可能自己再挂起）" % fn,
              "_pend" in text_of.get(fn, ""))
        # 特判那三支（子 agent / 工作流 / 群成员）各自过同一个外壳。
        check("%s 的特判工具也过钩子（3 支）" % fn,
              text_of.get(fn, "").count("_special_tool_result(") == 3,
              text_of.get(fn, "").count("_special_tool_result("))


def main():
    for fn in (test_no_config_means_inactive,
               test_block_and_reason,
               test_matcher_and_aliases,
               test_updated_input_and_context,
               test_nonzero_is_not_blocking,
               test_non_command_type_is_skipped,
               test_project_trust_gate,
               test_stop_hook_and_flag,
               test_timeout_does_not_block,
               test_substitution_of_project_dir,
               test_user_prompt_submit,
               test_session_start_once_and_source,
               test_post_tool_failure,
               test_ask_becomes_pending_approval,
               test_permission_request_can_answer_for_the_user,
               test_hook_ask_settles_without_granting,
               test_grant_for_kind_knows_its_limits,
               test_cron_never_asks_the_permission_hook,
               test_permission_allow_rerun_goes_through_post_hooks,
               test_trust_needs_a_real_true,
               test_special_tools_go_through_hooks,
               test_async_hook_never_blocks_and_never_decides,
               test_session_start_forces_sync,
               test_both_loops_ask_the_permission_hook):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
