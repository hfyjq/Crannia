"""兜底闸的机械读者。

## 它是什么

```
destructive（第二根轴）且 不在 _APPROVAL_CAPABLE_TOOLS（自己会碰到审批）
派发前先问用户，工具这一次不执行
```

* 不在能力名单，它没有自己的发射点，不问就真的做了（这是要治的那个洞）
* 在能力名单，它自己的路上会碰到一次审批，这里再问就是双问

## 这个文件里最要紧的一条

`test_the_payload_survives_the_format_layer` —— **载荷必须过
`_format_tool_result` 那一层**，不许手工构造了直接喂检测器。

理由：手工构造载荷喂检测器会跳过 `_format_tool_result` ——
而坏掉的正是那一层。
「跑过」≠「在生产会走的那条路上跑过」。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_destructive_gate.py
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import tool_permission as TP                     # noqa: E402
from core.agent_loop import (                              # noqa: E402
    _APPROVAL_CAPABLE_TOOLS, _destructive_gate_needed,
    _destructive_pending_payload,
)

_SCHEMA = {"description": "p", "inputSchema": {"type": "object", "properties": {}}}
_CELLS = ("tools", "tool_perms", "tool_perm_sources",
          "tool_destructive", "tool_destructive_sources", "tool_owners")

_BOOTED = False


def _boot_once():
    global _BOOTED
    if not _BOOTED:
        from scripts.measure_request import boot_registry
        boot_registry()
        _BOOTED = True


def _snap_all():
    import bus.registry as R
    return {k: dict(R._registry[k]) for k in _CELLS} | {
        "tool_schemas": list(R._registry["tool_schemas"])}


def _restore_all(snap):
    import bus.registry as R
    for k in _CELLS:
        R._registry[k].clear()
        R._registry[k].update(snap[k])
    R._registry["tool_schemas"][:] = snap["tool_schemas"]


class _FakeWM:
    """测试替身：只回答"这个目标批过没有"。"""

    def __init__(self, granted=()):
        self.approval_ask_counts: dict = {}
        self._granted = set(granted)

    def has_target_grant(self, kind: str, subject: str) -> bool:
        return (kind, subject) in self._granted


def _with_wm(wm, fn):
    """把 `bus.registry.get_workspace_manager` 换掉跑一段。"""
    import bus.registry as R
    saved = R.get_workspace_manager
    try:
        R.get_workspace_manager = lambda: wm
        return fn()
    finally:
        R.get_workspace_manager = saved


# 1. 判据：两根轴的差集

def test_a_read_tool_never_asks():
    """不是不可逆，不问。"""
    _boot_once()
    assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed("s", "read_file")) is False


def test_a_capable_tool_never_asks_twice():
    """在能力名单里，不问 —— 那是"双问结构性不可能"那半条。

    这三个工具的兑现路径上各会遇到一次审批（`shell.py` / `file_parser`），
    闸再问一次就是同一个问题问两遍 —— 而用户看到的是"点了同意它又问一遍"。
    """
    _boot_once()
    for name in sorted(_APPROVAL_CAPABLE_TOOLS):
        assert _with_wm(_FakeWM(), lambda n=name: _destructive_gate_needed("s", n)) is False, (
            "%s 在能力名单里，闸却又问了一次（双问）" % name)


def test_a_destructive_noncapable_tool_asks():
    """自造靶子：`destructive` 且不在能力名单，必须问。

    这条就是整个第 6 步的判据本身。用合成的工具名造靶子，
    不依赖今天恰好只有 `send_email` 那一个（那由另一条测试钉着）。
    """
    _boot_once()
    import bus.registry as R
    snap, allsnap = TP.snapshot(), _snap_all()
    try:
        TP.restore({})
        TP.declare_destructive("probe_gate_yes", True)
        R.register_tool("probe_gate_yes", lambda **k: 1, _SCHEMA)
        assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed("s", "probe_gate_yes")) is True
        # 反方向：同一个名字、只是没声明不可逆，不问（兜底 fail-open）
        TP.declare_destructive("probe_gate_no", False)
        R.register_tool("probe_gate_no", lambda **k: 1, _SCHEMA)
        assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed("s", "probe_gate_no")) is False
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


def test_a_grant_stops_the_asking():
    """批过就不再问 —— 一次性（本轮重提交）与会话级都走 `has_target_grant`。

    少了这一条，"同意"之后模型重提交会被再问一遍，无限审批循环
    （连点四次，每次都"同意"，每次都再弹）。
    """
    _boot_once()
    import bus.registry as R
    snap, allsnap = TP.snapshot(), _snap_all()
    try:
        TP.restore({})
        TP.declare_destructive("probe_gate_g", True)
        R.register_tool("probe_gate_g", lambda **k: 1, _SCHEMA)
        wm = _FakeWM(granted={("destructive", "probe_gate_g")})
        assert _with_wm(wm, lambda: _destructive_gate_needed("s", "probe_gate_g")) is False, (
            "已经批过的工具又被问了一次")
        # 反方向：批的是别的工具，照样问（授权是按工具的，不是整类）
        wm2 = _FakeWM(granted={("destructive", "probe_gate_other")})
        assert _with_wm(wm2, lambda: _destructive_gate_needed("s", "probe_gate_g")) is True
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


def test_no_workspace_manager_means_ask():
    """拿不到 wm，问（fail-closed）。

    这条路上"不问"是不可逆的那一侧 —— 与 `destructive` 那一根轴的兜底
    （fail-open）方向相反，是刻意的：那一根决定"要不要进这道闸"，
    这一条决定"进了闸还能不能跳过去"。
    """
    _boot_once()
    import bus.registry as R
    snap, allsnap = TP.snapshot(), _snap_all()
    try:
        TP.restore({})
        TP.declare_destructive("probe_gate_nw", True)
        R.register_tool("probe_gate_nw", lambda **k: 1, _SCHEMA)
        assert _with_wm(None, lambda: _destructive_gate_needed("s", "probe_gate_nw")) is True
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


# 2. 载荷：能被认出来，而且过了格式化层还认得出来

def test_the_payload_is_recognizable():
    """`status` 以 `pending_` 开头 + 识别要用的四格一个不少。"""
    import core.agent_loop as AL
    raw = _destructive_pending_payload("send_email")
    data = AL._parse_approval_payload(raw)
    assert data is not None, "检测器认不出这个载荷：%r" % (raw[:160],)
    for k in ("status", "kind", "ask_index", "subject"):
        assert k in data, "载荷缺了识别/结算要用的那一格：%s" % k
    assert data["status"] == "pending_destructive_approval"
    assert data["kind"] == "destructive"
    assert data["subject"] == "send_email"


def test_the_payload_carries_what_is_being_approved():
    """要批准的那件事本身必须送出去。

    只给工具名的形状：用户看到的是
    「Crannia 想执行一个不可逆的操作：`send_email`」——
    而他批的正是"发给谁、发什么"。"要不要批准这件事"里，"这件事"没送出去。

    同一条判据也在 `core/shell.py` 上：命令原文整个放进 `subject` ——
    「被审批物（命令原文）要整个装进审批载荷才送得到你手上」。
这条读者钉的是：它现在也送出去了。
    """
    import core.agent_loop as AL
    args = {"to": "someone@example.com", "subject": "月度报表",
            "body": "见附件。"}
    raw = _destructive_pending_payload("send_email", args)
    data = AL._parse_approval_payload(raw)
    assert data is not None
    body = str(data.get("body") or "")
    for needle in ("someone@example.com", "月度报表"):
        assert needle in body, (
            "参数没有进载荷正文 -> 用户是在**不知道自己在批准什么**的情况下点的同意\n"
            "  body=%r" % (body[:300],))
    # `subject` = 门槛那一侧用的同一个键（它决定授权命中不命中）。
    # 判据不是"它等于工具名" —— `send_email` 的键是 `send_email␟<收件人>`
    #    （「这次发给 A 选不再询问，本会话内就不再询问去 A 的邮件」）。
    #    这条读者真正要防的：两处各算一遍，授权永远不生效。
    #钉同源（拿门槛那边那个函数算一遍比），不钉一个字面量。
    assert data["subject"] == AL._subject_of_destructive("send_email", args), (
        "载荷里的 subject 与门槛那边用的**不是同一个键** —— "
        "用户批的那条与工具下一次提交的对不上，授权**永远不生效**")
    assert "someone@example.com" in data["subject"], (
        "★ 键里必须认得出收件人 —— 否则「不再询问」是本会话**所有**邮件的空头支票")


def test_credentials_in_the_args_never_reach_the_payload():
    """凭据一个字节都不许进载荷 —— 否则历史那一层的打码当场作废。

    理由：这段正文随载荷进 `messages`，落库、出网
    （凭据必须挡在历史之外）。渲染参数时必须先过
    `SecurityGuard.redact_credential_args` —— 同一个打码器，不是第二份。
    """
    import core.agent_loop as AL
    from core.security import SecurityGuard
    secret = "a1b2c3d4e5f6g7h8"          # 邮箱授权码的真实形状（无格式特征）
    raw = _destructive_pending_payload(
        "send_email",
        {"to": "someone@example.com", "password": secret,
         "nested": {"smtp_password": secret}})
    assert secret not in raw, (
 "凭据原文进了审批载荷 -> 落库 + 出网，白做了")
    assert SecurityGuard.REDACTED_ARG in raw, (
        "没有看到打码占位符 —— 要么没打码，要么整个参数没被渲染")
    data = AL._parse_approval_payload(raw)
    assert data is not None, "打码之后载荷坏了（它必须仍是合法 JSON）"


def test_overlong_args_are_bounded_and_the_cut_is_declared():
    """参数超预算，在源头裁，并且说出来。

    为什么必须在源头裁：`body` 不在 `_APPROVAL_SHRINKABLE` 里
超了没有东西会替它缩，整个载荷会超预算
    （半截载荷，检测器不认，审批静默消失）。
    为什么裁了要说：被审批的文本就是被同意的文本，藏掉尾巴等于让用户
    同意他读不到的东西，那句话要给出出路（回 4 = 我有别的话说）。
    """
    import core.agent_loop as AL
    big = {"to": "someone@example.com", "body": "x" * 20_000}
    raw = _destructive_pending_payload("send_email", big)
    assert len(raw) < 8_000, (
 "参数没有在源头被裁 -> 载荷超预算，会被按字符切（原始形状）：%d 字符"
        % len(raw))
    assert "只看前" in raw or "只列前" in raw, (
        "裁了却没说 —— 用户会以为自己看到的是全部")
    assert AL._parse_approval_payload(raw) is not None, "裁完载荷坏了"


def test_the_payload_survives_the_format_layer():
    """载荷过完 `_format_tool_result` 仍然能被认出来 —— 本文件最要紧的一条。

    失败形状：手工构造载荷喂检测器，
    跳过了 `_format_tool_result` —— 而坏掉的正是那一层。
所以这里从发射点拿（`_destructive_pending_payload`），过一个真实的预算，
    再喂真检测器。三种预算都过：宽裕 / 刚好 / 紧到必须按字段缩。
    """
    import core.agent_loop as AL
    from _sandbox import prod_diff, prod_snapshot, sandbox
    # 载荷带着参数，这条测试也带（否则它测的是一个更小的载荷，
    #   而"预算刚好 / 紧到必须按字段缩"那两档在新形状下才是真实的）。
    raw = _destructive_pending_payload(
        "send_email", {"to": "someone@example.com", "subject": "月度报表",
                       "body": "见附件。"})
    # 最后那档会落盘（`_spill_oversized_approval` 把超限载荷存到
    # `data/workdata/tool_results/`），必须关进沙箱，否则测试写生产目录。
    # 顺序：`_boot_once()` 那些不能在沙箱里跑（chdir 之后 `skills/` 没了）。
    before = prod_snapshot()
    with sandbox():
        for budget in (10_000, 600, 220, 90):
            out = AL._format_tool_result(raw, "send_email", max_chars=budget)
            data = AL._parse_approval_payload(out)
            assert data is not None, (
                "过了格式化层就认不出来了（budget=%d）—— 审批会**静默消失**\n"
                "  产出: %r" % (budget, out[:200]))
            for k in ("status", "kind", "ask_index", "subject"):
                assert k in data, (
                    "budget=%d 时 `%s` 被裁掉了 —— 识别/结算要用它，**不许裁**"
                    % (budget, k))
            # 同源比对（键里带收件人）。
            assert data["subject"] == AL._subject_of_destructive(
                "send_email", {"to": "someone@example.com", "subject": "月度报表",
                               "body": "见附件。"}), (
                "budget=%d 时 subject 变了 -> 用户批的那条与工具下一次提交的对不上，"
                "授权**永远不生效**" % budget)
    leaked = prod_diff(before, prod_snapshot())
    assert not leaked, "这条测试往生产目录写了东西：%r" % (leaked,)


def test_the_detector_sets_the_pending_slot():
    """检测器把载荷落到 `ctx.pending_destructive`（按 `kind` 分派那条路）。"""
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending
    raw = _destructive_pending_payload("send_email")
    ctx = types.SimpleNamespace(
        messages=[{"role": "tool", "content": raw}],
        pending_cross_zone={}, pending_delete={}, pending_egress={},
        pending_command={}, pending_destructive={},
        approval_ask_counts={}, response="",
        session=types.SimpleNamespace(platform="cli"),
    )
    _check_cross_zone_pending(ctx, 0)
    pend = ctx.pending_destructive
    assert pend.get("active") is True, "闸挂起时没有落到那一格: %r" % (pend,)
    assert pend.get("stage") == "ask"
    assert pend.get("kind") == "destructive", (
 "**公开的 kind 取自载荷** —— 用错的 kind 结算会读反")
    assert pend.get("subject") == "send_email"
    assert pend.get("request_id"), "没有 request_id -> 浮窗挡不住双击/重放"
    assert ctx.approval_ask_counts.get("destructive") == 1, (
        "被问了几次没记 —— 升级项（第 5 项）会在第一次问时就冒出来")
    assert ctx.response, "文本通道没有收到提示语"


# runner

# 6. 「被审批物」按工具定

def test_the_subject_carries_the_recipient():
    """被审批物 = `send_email␟<收件人>`；其余工具仍然是工具名。

    判据：这次发给 A 选了不再询问，本会话内就不再询问去 A 的邮件，
    与命令那边一样（那边的 subject 是归一化后的命令原文）。
    """
    import core.agent_loop as AL
    from core.approval import subject_for
    a = subject_for("send_email", {"to": "a@b.com"})
    b = subject_for("send_email", {"to": "b@b.com"})
    assert a != b, "两个收件人算出同一个键 —— 「不再询问」会变成空头支票"
    assert "a@b.com" in a, "键里认不出收件人：%r" % a
    assert a == AL._subject_of_destructive("send_email", {"to": "a@b.com"}), (
        "两个入口（`approval.subject_for` 与 `agent_loop._subject_of_destructive`）"
        "算出了不同的键 —— 授权永远不生效")
    # 负对照：别的工具一个字没变
    assert subject_for("delete_files", {"pattern": "*"}) == "delete_files"
    assert subject_for("send_email", {}) == "send_email", (
        "取不到收件人时该退回工具名（更严的一侧），不该编一个空键出来")


def test_a_granted_recipient_is_not_asked_again_but_another_one_is():
    """这一条就是目标行为。

    批过 A，本会话内发给 A 不再问；发给 B 照问。
    没有后半句，这条挡不住"把键退回工具名"那种改法（那条会让它整段失效）。
    """
    _boot_once()
    from core.approval import subject_for
    mail_a = {"to": "a@b.com", "subject": "s", "body": "b"}
    mail_b = {"to": "b@b.com", "subject": "s", "body": "b"}
    wm = _FakeWM({("destructive", subject_for("send_email", mail_a))})
    assert _with_wm(wm, lambda: _destructive_gate_needed("s", "send_email", mail_a)) is False, (
        "批过的收件人又被问了 —— 用户要的是「本会话内不再询问去 A 的邮件」")
    assert _with_wm(wm, lambda: _destructive_gate_needed("s", "send_email", mail_b)) is True, (
        "换了个收件人却没问 —— 批一次放行了本会话所有邮件（空头支票）")


def _with_allowlist(addrs):
    """把 `data/allowlist.json` 种成 `email_to = addrs`，跑完还原。

    必须chdir 进沙箱再种：`core/allowlist.py` 的路径是相对 cwd 的
       （与 `data/settings.json` 同一条规矩），不换目录就是写生产文件。
    """
    import contextlib
    import json as _json
    import os
    import tempfile

    @contextlib.contextmanager
    def _ctx():
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "data"), exist_ok=True)
            with open(os.path.join(td, "data", "allowlist.json"), "w",
                      encoding="utf-8") as f:
                _json.dump({"egress": [], "command": [], "email_to": list(addrs)}, f)
            os.chdir(td)
            try:
                yield
            finally:
                os.chdir(cwd)
    return _ctx()


def test_the_persistent_allowlist_skips_the_ask():
    """skill 页里加的地址，根本不问（持久，跨会话）。

    判据：邮件白名单写在 skill 页的邮箱 skill 里 ——
    用户可以往里面加不询问的地址。
    """
    _boot_once()
    mail = {"to": "a@b.com", "subject": "s", "body": "b"}
    with _with_allowlist(["a@b.com"]):
        assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed(
            "s", "send_email", mail)) is False, "名单里的地址还是被问了"
    with _with_allowlist([]):
        assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed(
            "s", "send_email", mail)) is True, (
            "名单**空**的时候也没问 —— 空表不等于全放行")


def test_the_allowlist_requires_all_recipients():
    """反面：3 个收件人里只有 1 个在名单里，照问。

    判据是"全都在"不是"至少一个" —— 剩下那两个是模型挑的，
    用户从没同意过。
    """
    _boot_once()
    mail = {"to": ["a@b.com", "x@b.com", "y@b.com"], "subject": "s"}
    with _with_allowlist(["a@b.com"]):
        assert _with_wm(_FakeWM(), lambda: _destructive_gate_needed(
            "s", "send_email", mail)) is True, (
            "只有 1/3 在名单里却放行了 —— 另外两个收件人从没被同意过")


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    _ = tests
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
