# -*- coding: utf-8 -*-
"""闸门机制（出网 + 执行两个策略域共用一套）。

这个文件存在的理由：机制是新的，而且它有两个"读反了方向相反"的陷阱。

## 三个陷阱

1. 键位含义按 `kind` 走。`COMMAND_GUARDED_OPTIONS` 的 `2` 是不同意，
   而 `COMMAND_OPTIONS` 的 `2` 是同意并且不再问。
   复用 `_settle_target` 会把用户的"不"读成"是" —— 方向正好相反。
2. 计数必须按 `kind` 隔离。出网与执行都是 `escalate_at=2`、跨区是 3；
   共用一个计数器会让它们互相顶早（`core/approval.py::ASK_COUNTED_KINDS`
   注释里那条警告说的就是这个）。
3. 危险档不许有「别再问」。判据：不给推荐保存项
   （不鼓励把危险命令存成长期规则），且高危必须永远拒绝，
   即使人明确要求也一样。

## 跑法

    .venv/Scripts/python.exe tests/test_gate_mechanism.py

输出一律 ASCII（Windows 控制台 GBK）。不需要 pytest。
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sandbox import tempdir                                            # noqa: E402

from core import approval as A                                          # noqa: E402
from core.workspace import WorkspaceManager                             # noqa: E402
from core.session_key import session_key_for, session_key_of_ctx        # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# 夹具

def _sess():
    """一个真 MessageSession（群聊，键带 id，
    与"私聊共用那一条"区分开）—— 属主/作用域两侧都从 `session_key_of_ctx` 推导。"""
    from channels.base import MessageSession, MessageType
    return MessageSession("test", MessageType.GROUP, "hm")


SESS_KEY = session_key_for(_sess())      # = "workspace:group:hm"


class _Ctx:
    """够结算器用就行 —— 它只碰这几格。"""

    def __init__(self, session=None):
        self.session = _sess() if session is None else session
        self.session_target_grants: dict = {}
        self.metadata: dict = {}
        self.user_message = ""
        self.response = ""
        self.approval_ask_counts: dict = {}


def _RealWM():
    """构造一个只带授权状态的真 `WorkspaceManager`。

    用真类而不是替身：`has_target_grant` 的属主比对就写在它身上，
    替身会把那半测没（`test_delete_approval_scope.py` 的同一条理由）。
    构造走 `__new__` 是为了避开 `__init__` 里整套文件系统/设置加载 ——
    这里要测的是授权状态机，不是工作区。
    """
    wm = WorkspaceManager.__new__(WorkspaceManager)
    wm._target_once = {}
    wm._target_session = {}
    wm._approval_scope = ""
    return wm


# 1. 选项表：形状

def test_tables_shape() -> None:
    print("\n-- 1. 三张新表的形状 --")
    for kind in ("egress", "command"):
        opts = A.options_for(kind, 1)
        check("%s 第 1 次问有选项" % kind, len(opts) >= 2,
              "得到 %r" % [o.key for o in opts])

    # 升级项：第 1 次不在，第 2 次在
    for kind in ("egress", "command"):
        esc = [o for o in A._TABLES[kind] if o.escalate_at]
        check("%s 有且只有一个升级项" % kind, len(esc) == 1,
              "得到 %d 个" % len(esc))
        check("  %s 的升级项在第 2 次问时出现" % kind,
              esc[0].key in [o.key for o in A.options_for(kind, 2)])
        check("  ★ %s 第 1 次问时**不出现**" % kind,
              esc[0].key not in [o.key for o in A.options_for(kind, 1)])

    # `command_guarded` 这个 kind 已取消 —— 见下面第 2 条。
    # `plan` 随旧 plan 引擎整体删除后，由专用工具 + 专用 kind
    #   （`exit_plan_mode` 发 `pending_plan_approval`，被审批物是计划本身，
    #   结算翻的是模式）重新引入。
    # `destructive` —— 兜底闸
    #   （不可逆、而工具自己没有审批发射点）。它借的就是这套现成的派发/结算代码。
    # `ice_reveal` —— 键位含义与其余都不同：记的是"放行一个值"，
    #   第 2 项还顺带关掉本会话的信息层。
    # `delete_bulk` —— 批量删除的硬闸。
    #   它是"同一件事的另一个对象"，不是"同一件东西的另一个档位" ——
    #   两者的区别正是判据：危险档（`command_guarded`）被删，因为
    #   「同一种东西的分档」要统一；而批量的被审批物是"一次批量动作"。
    #   它自己占一格（`pending_delete_bulk`），不共用 `delete` ——
    #   共用是 `command_guarded` 栽三次的形状。
    check("★ 选项表里只有 12 个 kind（命令一种档位、计划重建、兜底闸、ICE 放行、批量硬闸、区外写、提问、逐应用授权、钩子要求确认）",
          set(A._TABLES) == {"cross_zone", "delete", "egress", "command",
                             "destructive", "plan", "ice_reveal", "delete_bulk",
                             # 工作区之外写文件。
                             "write_outside",
                             # 提问面板 —— 空表
                             #   （选项是模型在运行时给的，随挂起载荷走）。
                             "ask_user",
                             # 接管的逐应用授权。
                             "computer_app",
                             # 外部命令钩子要求人工确认（`PreToolUse` 的 ask）。
                             "hook_ask"},
          repr(sorted(A._TABLES)))


def test_command_has_exactly_one_tier() -> None:
    """命令的审批只有一个档位。

    这条测试取代了原来的「危险档不许有『别再问』」（判据 3）。

    那条判据有出处（危险档不给保存入口 + 高危永远拒绝），
    它被推翻了，理由两条：两档便于后续维护，且各家 agent
    开启 auto mode 后都是全自动批准，无论什么指令。

    为什么是删掉一个 kind 而不是"把两张表写成一样"：
    危险档存在的那一天，在同一个形状上栽了三次 ——
    `command` 与 `command_guarded` 共用 `_pending_command` 一格，
    于是"格的键"与"公开的 kind"是两个东西，每个读者都得记得去载荷里取。
    删掉一个，这个坑从构造上消失。

    危险并没有消失，只是换了载体 —— 下面几条钉住它：
    """
    print("-- 2. ★★ 命令只有一个审批档位（危险档取消）--")
    from core.pipeline.stages.pre_process import _SETTLERS

    check("★ `_TABLES` 里没有 command_guarded",
          "command_guarded" not in A._TABLES, repr(sorted(A._TABLES)))
    check("★ `COMMAND_GUARDED_OPTIONS` 这个常量也没了",
          not hasattr(A, "COMMAND_GUARDED_OPTIONS"), "")
    check("★ `_SETTLERS` 里也没有它",
          "command_guarded" not in _SETTLERS, repr(sorted(_SETTLERS)))
    check("★ `ASK_COUNTED_KINDS` 里也没有",
          "command_guarded" not in A.ASK_COUNTED_KINDS,
          repr(sorted(A.ASK_COUNTED_KINDS)))

    # 下面三条是"危险没消失"那一半
    from core.command_policy import (
        TIER_DENY, TIER_GUARDED, classify_command,
    )
    t, reason = classify_command("git status")
    check("★ 危险命令**仍然被认出来**（tier 还在，只有审批档位没了）",
          t == TIER_GUARDED, t)
    check("  而且理由文本还在（它进提示语正文）", "git" in reason,
          reason[:60])
    check("★ `deny` 档**原样保留**：出网类仍然是确定性拒绝，连问都不问",
          classify_command("curl https://x.com")[0] == TIER_DENY,
          classify_command("curl https://x.com")[0])
    check("  而且**不受**「关闭安全询问」开关影响",
          classify_command("curl https://x.com", relax_guarded=True)[0]
          == TIER_DENY,
          classify_command("curl https://x.com", relax_guarded=True)[0])


def test_key_2_means_the_same_everywhere() -> None:
    """`2` 的含义到处一致 —— 这正是取消危险档换来的东西。

    原来危险档的 `2` 是不同意，而普通档的 `2` 是「同意并不再问」——
    方向正好相反。失败形状：

        「用户点的是『不同意』，而系统记的是『已同意』」

现在两档合一，`2` 只有一种含义。这一条驱动真的结算器钉住它。
    """
    print("-- 3. ★★ `2` 的含义到处一致（原危险档那个反方向没了）--")
    from core.pipeline.stages.pre_process import _settle_command

    for label in ("git status", "cat core/security.py", "ls -la"):
        ctx = _Ctx()
        pend = {"active": True, "stage": "ask", "kind": "command",
                "subject": label}
        _settle_command("2", pend, ctx)
        check("%r 选 `2` -> **同意**（不是拒绝）" % label,
              "已同意" in ctx.user_message, ctx.user_message[:50])
        check("  且发会话级放行（只记这一条）",
              ctx.session_target_grants.get("command") == {label},
              repr(ctx.session_target_grants))
        check("  载体被清空", pend == {})

    # 反向：`3` 才是拒绝
    ctx2 = _Ctx()
    _settle_command("3", {"active": True, "stage": "ask",
                          "kind": "command", "subject": "git status"}, ctx2)
    check("★ 而 `3` 是拒绝（与 `2` 不混）", "拒绝" in ctx2.user_message,
          ctx2.user_message[:50])
    check("  拒绝不发任何放行", not ctx2.session_target_grants,
          repr(ctx2.session_target_grants))

    # 规则由载荷带下来（`pend["rule"]`），
    #    不是这里现算的 —— 而它空串（`guarded` / 拿不准）时退回命令原文。
    ctx3 = _Ctx()
    _settle_command("2", {"active": True, "stage": "ask", "kind": "command",
                          "subject": "npm run build", "rule": "npm run *"}, ctx3)
    check("★★ 有规则时，**载体里落的是规则**（不是命令原文）",
          ctx3.session_target_grants.get("command") == {"npm run *"},
          repr(ctx3.session_target_grants))
    check("★ 而**本轮那次重提交**那一半不进载体（它在 wm 的一次性表上）",
          "npm run build" not in (ctx3.session_target_grants
                                  .get("command") or set()),
          repr(ctx3.session_target_grants))
    ctx4 = _Ctx()
    _settle_command("2", {"active": True, "stage": "ask", "kind": "command",
                          "subject": "git status", "rule": ""}, ctx4)
    check("★ 规则为空（`guarded` 拿不到前缀）=> 退回整条精确匹配",
          ctx4.session_target_grants.get("command") == {"git status"},
          repr(ctx4.session_target_grants))


# 4. 计数按 kind 隔离 —— 驱动真检测器

class _PendCtx:
    """够 `_check_cross_zone_pending` 跑就行。

    `_is_web(ctx)` 会读 `ctx.session.platform` —— 给个非 web 的值，
    让 `ctx.response` 走"印整段提示语"那条分支
    （web 那条什么都不写：不许占模型自己的正文）。
    """

    def __init__(self):
        self.messages: list[dict] = []
        self.pending_cross_zone: dict = {}
        self.pending_delete: dict = {}
        self.pending_egress: dict = {}
        self.pending_command: dict = {}
        self.approval_ask_counts: dict = {}
        self.response = ""
        self.user_message = ""
        self.metadata: dict = {}
        self.session = type("S", (), {"platform": "cli"})()


def _feed(ctx, **payload):
    """往 `ctx.messages` 里塞一条工具结果（检测器就是扫这个）。"""
    import json as _json
    ctx.messages.append({"role": "tool", "tool_call_id": "c1",
                         "content": _json.dumps(payload, ensure_ascii=False)})


def test_detector_increments_per_kind() -> None:
    """驱动真的检测器 —— 不是我自己在字典上做加法。

    只做加法会假绿：它自己在 `ctx.approval_ask_counts` 上
    累加，然后断言累加的结果 —— 测的是"字典长什么样"，而真正的推进写在
    `core/pipeline/stages/agent_loop.py::_check_cross_zone_pending` 里，
    一行都没被碰过。那是「绿了但没测到」的形状之一。
    """
    print("\n-- 4. ★ 计数按 kind 隔离（驱动真检测器）--")
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending

    ctx = _PendCtx()

    # ① 出网问两次
    for i in range(2):
        ctx.pending_egress = {}
        ctx.messages = []
        _feed(ctx, status="pending_egress_approval", kind="egress",
              subject="d%d.com" % i, prompt="[提示]")
        _check_cross_zone_pending(ctx, 0)
    check("egress 被问了 2 次", ctx.approval_ask_counts.get("egress") == 2,
          "得到 %r" % (ctx.approval_ask_counts,))

    # ② 执行问一次
    ctx.pending_command = {}
    ctx.messages = []
    _feed(ctx, status="pending_command_approval", kind="command",
          subject="ls", prompt="[提示]")
    _check_cross_zone_pending(ctx, 0)
    check("command 被问了 1 次", ctx.approval_ask_counts.get("command") == 1,
          "得到 %r" % (ctx.approval_ask_counts,))

    # ③ 跨区一次都没问过 —— 不能被别人的计数带上
    check("★ cross_zone **一次都没被问**（没被别人的计数带上）",
          ctx.approval_ask_counts.get("cross_zone", 0) == 0,
          "得到 %r" % (ctx.approval_ask_counts,))
    check("★ 于是跨区第 1 次问时**没有**第 5 项",
          "5" not in [o.key for o in A.options_for(
              "cross_zone", ctx.approval_ask_counts.get("cross_zone", 0) or 1)])
    check("★ 而出网第 2 次问时**有**第 5 项",
          "5" in [o.key for o in A.options_for(
              "egress", ctx.approval_ask_counts.get("egress", 0) or 1)])

    # ⑤ 载体真的被设上了（不只是计数）
    check("pending_command 被设成 active", ctx.pending_command.get("active") is True)
    check("  且 kind 是 command", ctx.pending_command.get("kind") == "command")
    check("  且 request_id 在（浮窗靠它挡重放）",
          bool(ctx.pending_command.get("request_id")))

    # `destructive` 也有升级项（第 5 项
    #   「本次对话内所有这类操作自动放行」，`escalate_at=2`），它必须计数。
    # `write_outside` 也在里面 —— 它的选项表第 5 项
    # `escalate_at=2`，不计数那一项就永远不出现。
    check("ASK_COUNTED_KINDS 是那五个（命令只有一种档位了 + 兜底闸 + 区外写）",
          A.ASK_COUNTED_KINDS == frozenset(
              {"cross_zone", "egress", "command", "destructive", "write_outside"}),
          "得到 %r" % (sorted(A.ASK_COUNTED_KINDS),))


def test_detector_rejects_unknown_kind() -> None:
    """不认识的 `kind` 不许静默设上 pending。

    静默的失败方向很不安全：一个拼错的 kind 会让浮窗拿到一张空表，
    而"载荷里有一份谁也不读的选项表"正是 `core/approval.py` 要消灭的那种副本。
    """
    print("\n-- 4b. ★ 未知 kind 不设 pending（不静默）--")
    from core.pipeline.stages.agent_loop import _check_cross_zone_pending

    ctx = _PendCtx()
    _feed(ctx, status="pending_egress_approval", kind="egres",   # 拼错
          subject="x.com", prompt="[提示]")
    _check_cross_zone_pending(ctx, 0)
    check("拼错的 kind => pending_egress 仍是空的",
          ctx.pending_egress == {}, "得到 %r" % (ctx.pending_egress,))
    check("  ★ 也没被计数", not ctx.approval_ask_counts)


# 5. 按目标的授权（两半 + 通配 + 属主）

def test_target_grants() -> None:
    print("\n-- 5. 按目标的授权：一次性 / 会话级 / 通配 / 属主 --")
    wm = _RealWM()
    wm._approval_scope = SESS_KEY

    check("什么都没批 => 不放行", wm.has_target_grant("egress", "a.com") is False)

    wm.grant_target_once("egress", "a.com", SESS_KEY)
    check("一次性 + 属主对得上 => 放行",
          wm.has_target_grant("egress", "a.com") is True)
    check("  别的目标仍然不放行",
          wm.has_target_grant("egress", "b.com") is False)
    check("  ★ 别的 kind 的同一目标也不放行（kind 是键的一部分）",
          wm.has_target_grant("command", "a.com") is False)

    # 属主
    wm._approval_scope = "sess-B"
    check("★ 属主对不上 => **不**放行",
          wm.has_target_grant("egress", "a.com") is False)
    wm._approval_scope = SESS_KEY
    check("  属主换回来 => 又放行了", wm.has_target_grant("egress", "a.com") is True)

    # 会话级
    wm.set_target_session("egress", {"c.com"})
    check("会话级集合里的目标放行",
          wm.has_target_grant("egress", "c.com") is True)
    check("  ★ 会话级**不比属主**（它是每轮投影，天然只属于本会话）",
          wm.has_target_grant("egress", "c.com") is True)

    # 通配
    wm.set_target_session("egress", {"*"})
    check("★ `*` 通配 => 任何目标都放行",
          wm.has_target_grant("egress", "any-random.example") is True)
    check("  ★ 但不跨 kind",
          wm.has_target_grant("command", "any-random.example") is False)

    # 整组替换（投影必须能收回）
    wm.set_target_session("egress", set())
    check("★ 投影是**替换**不是合并 => 能被收回",
          wm.has_target_grant("egress", "any-random.example") is False)

    # 轮末清一次性那一半
    wm.grant_target_once("egress", "z.com", SESS_KEY)
    check("清之前放行", wm.has_target_grant("egress", "z.com") is True)
    wm.reset_target_grants()
    check("★ 轮末清掉一次性 => 不再放行", wm.has_target_grant("egress", "z.com") is False)
    wm.set_target_session("egress", {"s.com"})
    wm.reset_target_grants()
    check("  ★ 但**不动投影**（它每轮开局会重新投影）",
          wm.has_target_grant("egress", "s.com") is True)


# 6. 结算器写两处：wm（本轮）+ 载体（落盘）

def test_settle_writes_both_halves() -> None:
    print("\n-- 6. 结算写两处：wm（本轮工具看得到）+ 载体（落盘）--")
    import bus.registry as REG
    from core.pipeline.stages.pre_process import _settle_egress

    wm = _RealWM()
    wm._approval_scope = SESS_KEY
    REG.register_workspace_manager(wm)
    # 必须重定向白名单路径：这一节会跑到选项 5，而它现在写真文件。
    # 不重定向的话 `data/allowlist.json` 会被创建成
    # `{"egress": ["*"], "command": []}` —— 那等于在本机上把出网闸门关掉了。
    # （`check_test_pollution` 也看不见它：那个检查器比的是"一次运行内的前后指纹"，
    #   而这个文件是运行之前就被造出来的。）
    _with_tmp_allowlist()
    try:
        ctx = _Ctx()
        pend = {"active": True, "stage": "ask", "kind": "egress",
                "subject": "example.com"}

        _settle_egress("2", pend, ctx)
        check("载体里有这个目标", ctx.session_target_grants.get("egress") == {"example.com"},
              "得到 %r" % (ctx.session_target_grants,))
        check("★ wm 的一次性授权也发了（本轮工具靠它放行）",
              wm.has_target_grant("egress", "example.com") is True)
        check("  载体**没有**写通配（`2` 只管这一个）",
              "*" not in (ctx.session_target_grants.get("egress") or set()))

        ctx2 = _Ctx()
        pend2 = {"active": True, "stage": "ask", "kind": "egress",
                 "subject": "other.com"}
        _settle_egress("5", pend2, ctx2)
        # 选项 5 是会话级。
        #它写会话载体，不碰那张持久表（下面 test 9 验那半）。
        check("★ `5` 写会话载体的通配 `*`（本会话内所有目标）",
              "*" in (ctx2.session_target_grants.get("egress") or set()),
              "得到 %r" % (ctx2.session_target_grants,))
        check("  但本轮的一次性授权照发（本轮的重提交要能过）",
              wm.has_target_grant("egress", "other.com") is True)

        ctx3 = _Ctx()
        pend3 = {"active": True, "stage": "ask", "kind": "egress",
                 "subject": "no.com"}
        _settle_egress("3", pend3, ctx3)
        check("★ `3` 拒绝 => 载体**一条都不加**",
              not ctx3.session_target_grants, "得到 %r" % (ctx3.session_target_grants,))
        check("  且给模型的话是'没有发出'",
              "没有被发出" in ctx3.user_message, "得到: %r" % ctx3.user_message)
    finally:
        REG._registry.pop("workspace_manager", None)


# 7. 子状态：`4` 进中间态，且不给选项

def test_substate_gives_text_not_options() -> None:
    print("\n-- 7. `4 我有话说` 进中间态：要文本、**不给选项** --")
    from core.pipeline.stages.pre_process import _SETTLERS

    for kind in ("egress", "command"):
        ctx = _Ctx()
        pend = {"active": True, "stage": "ask", "kind": kind, "subject": "x"}
        _SETTLERS[kind]("4", pend, ctx)
        check("%s：进了 await_other" % kind, pend.get("stage") == "await_other")
        check("  框架接管（给了 response）", bool(ctx.response))
        payload = A.build_payload(kind, 1, stage="await_other")
        check("  ★ 浮窗**不给选项**（该做的是说一句话）", payload["options"] == [])
        check("  ★ 但**有问题文本**（空问题 = 静默的坏输入框）",
              bool(payload["question"]), "得到 %r" % payload["question"])


# 8. 持久白名单（选项 5 / 设置页 / 手改文件 —— 一个真相源）

def _with_tmp_allowlist():
    """把白名单文件指到临时目录 —— 不许碰生产 `data/allowlist.json`。"""
    import core.allowlist as WL
    import pathlib
    td = tempdir("crannia_wl_") / "allowlist.json"
    WL._PATH = td
    return WL, td


def test_persistent_allowlist() -> None:
    print("\n-- 8. ★ 持久白名单：一个真相源、三处来源 --")
    import json
    WL, p = _with_tmp_allowlist()

    # 判据不是"恰好两个"——是"每个已知 kind 都在"。
    # 写死 `{"egress", "command"}` 的话，新增一个 kind 那天它就假红一次
    #    —— 而它真正要防的（"调用方不用处理 KeyError"）一个字没变。
    check("空文件 => 空表（不问就是「全放行」，方向不对）",
          WL.load() == {k: [] for k in WL.KINDS})
    check("★ 未配置时**每个 kind 的键都在**（调用方不用处理 KeyError）",
          set(WL.load()) == set(WL.KINDS))

    check("add 返回 True", WL.add("egress", "bilibili.com") is True)
    check("★ add 幂等（第二次返回 False）", WL.add("egress", "bilibili.com") is False)
    check("★ `command_guarded` 加不进去（危险档不进表）",
          WL.add("command_guarded", "rm -rf /") is False)
    check("  且它没被写进文件", "command_guarded" not in json.dumps(WL.load()))

    # 三处来源写同一个文件，互相看得见
    WL.add("egress", "*")                       # 模拟"对话里选项 5"
    check("★ 手改文件之后 load() 立刻看得见（每轮现读，不缓存）",
          "*" in WL.list_for("egress"))
    check("remove 能删掉它（= 关掉「全部自动放行」）",
          WL.remove("egress", "*") is True)
    check("  删掉之后就不在了", "*" not in WL.list_for("egress"))
    check("remove 幂等（本来没有 => False）", WL.remove("egress", "*") is False)

    # 坏文件必须朝"全问"倒
    p.write_text("{ 这不是 json", encoding="utf-8")
    check("★ 文件损坏 => **空表**（= 全都问），不是抛异常",
          WL.load() == {k: [] for k in WL.KINDS})
    p.write_text('[1,2,3]', encoding="utf-8")
    check("★ 顶层不是对象 => 也是空表", WL.load()["egress"] == [])
    # 单个 kind 坏了不影响另一个
    p.write_text(json.dumps({"egress": "不是列表", "command": ["ls"]}),
                 encoding="utf-8")
    got = WL.load()
    check("★ 某个 kind 坏了 => 只丢它，另一个留着",
          got["egress"] == [] and got["command"] == ["ls"], "得到 %r" % (got,))

    # 原子写：不留 .tmp
    p.unlink(missing_ok=True)
    WL.add("egress", "x.com")
    check("★ 写完不留临时文件（原子写，tmp + os.replace）",
          not p.with_suffix(".json.tmp").exists())


def test_option5_is_session_scoped_and_never_touches_the_file() -> None:
    """选项 5 的落点 —— 会话级。

    若这条测试断言相反的事（"选项 5 真的写进了持久表"），
    它就会照着一个说谎的标签通过下去：

        标签：同意，并且本次对话内全部自动放行
        实现：写 `data/allowlist.json`（穿过 `/clear` 与重启）

结果：清空之后同一目标没有重新询问（权限残留）。
改法：标签改成「同意，且本次对话内所有指令自动放行」，
      并保留会话级别、清理时移除权限。

    这一条现在钉的是两件相反的事：
      ① 载体里有 `"*"`（本会话内所有目标都放行）
      ② 持久表里没有它（`/clear` 才能真的清掉）
    """
    print("\n-- 9. ★★ 选项 5 = 会话级，且**绝不**碰持久表 --")
    import bus.registry as REG
    from core.pipeline.stages.pre_process import _settle_egress

    WL, _ = _with_tmp_allowlist()
    wm = _RealWM()
    wm._approval_scope = SESS_KEY
    REG.register_workspace_manager(wm)
    try:
        ctx = _Ctx()
        _settle_egress("5", {"active": True, "stage": "ask",
                             "kind": "egress", "subject": "a.com"}, ctx)
        check("★ 会话载体的 egress 里出现了通配 `*`",
              "*" in (ctx.session_target_grants.get("egress") or set()),
              "得到 %r" % (ctx.session_target_grants,))
        check("★★ **持久表里一条都没有**（写了它就穿过 /clear，"
              "而标签说的是「本次对话内」）",
              WL.list_for("egress") == [],
              "得到 %r —— 这正是用户实测撞到的那个「权限残留」"
              % (WL.list_for("egress"),))
        check("  本轮的一次性授权照发（重提交要能过）",
              wm.has_target_grant("egress", "a.com") is True, "")

        # 选项 2 相反：只记这一个目标，不是 `*`
        ctx2 = _Ctx()
        _settle_egress("2", {"active": True, "stage": "ask",
                             "kind": "egress", "subject": "b.com"}, ctx2)
        check("选项 2 进会话载体",
              ctx2.session_target_grants.get("egress") == {"b.com"},
              "得到 %r" % (ctx2.session_target_grants,))
        check("★ 选项 2 **不进**持久表（标签写的是「本次对话内」）",
              "b.com" not in WL.list_for("egress"))
    finally:
        REG._registry.pop("workspace_manager", None)


def test_clear_really_drops_the_session_grants() -> None:
    """`/clear` 之后「本次对话内不再询问」必须真的失效 —— 走整条链。

    这一条是被一次"空跑"逼出来的。按清单验第 7 条
    （`/clear` 之后同一条命令应当重新问），结果它没问 ——
    但那不是权限没清：模型压根没调工具（日志 `tools=0`）。
    根因是那一问的答案就在 system prompt 里 ——
    `core/prompt_assembler.py` 的 `<git_status>` 段每轮都带着
    `Recent commits: 58149cb …`，而问的正是"最近的 git 版本号"。

权限那一半从来没被那次实测覆盖过。所以在这里补上：
    直接驱动真的 `PreProcessStage.process('/clear')`，再走一遍
    调度器的轮末收回 + 轮首投影，最后问 `has_target_grant` ——
    那才是工具下次提交时真正问的问题。
    """
    print("\n-- 11. ★★ `/clear` 真的清掉按目标的会话级放行 --")
    import asyncio
    import bus.registry as REG
    from core.pipeline.scheduler import PipelineScheduler, _PENDING_STORES
    from core.pipeline.stage import StageContext
    from core.pipeline.stages.pre_process import PreProcessStage

    wm = _RealWM()
    REG.register_workspace_manager(wm)
    try:
        class _Pool:
            def new_session(self, key):
                return "sid-new", []

        class _S:
            platform = "cli"

            def __str__(self):
                return "sess-A"

        ctx = StageContext(session=_S(), user_message="/clear")
        ctx.session_pool = _Pool()
        # 上一段对话批过的（选项 2）：一条出网、一条命令
        ctx.session_target_grants = {"egress": {"example.com"},
                                     "command": {"ls -la"}}
        asyncio.run(PreProcessStage().process(ctx))

        # 承重的 setup 断言：不先确认 `/clear` 真的走到了，
        # 下面几条就是重言式（同 `test_approval_dispatch` 那条的规矩）。
        # 判"走没走到"看标记，不看回话的措辞（回话会改语言）。
        check("setup: /clear 真的走了",
              ctx.metadata.get("cleared") is True,
              repr(ctx.response)[:60])
        check("  载体被清空", not ctx.session_target_grants,
              repr(ctx.session_target_grants))
        check("  cwd 投影也清了", not wm.session_cwd,
              repr(wm.session_cwd))

        # 调度器那半：轮末收回 → 轮首投影
        sched = PipelineScheduler.__new__(PipelineScheduler)
        # 照 `_PENDING_STORES` 建那几格（别手写 —— 手写的名单会烂）
        for _k, _attr in _PENDING_STORES:
            setattr(sched, _attr, {})
        sched._session_target_grants = {"sess-A": {"egress": {"example.com"}}}
        sched._session_skip_delete_approval = set()
        sched._session_cross_zone_auto = set()
        sched._approval_ask_counts = {}
        sched._session_cwd = {"sess-A": "/somewhere"}

        # 轮末：调生产代码（`persist_turn_carriers`）——
        # 不要抄那 60 行去断言，那是"第二份"（两份会各自漂）。
        # 抽成方法之后，这一句问的是真的那份。
        sched.persist_turn_carriers(ctx, "sess-A")
        check("  轮末把这一格的会话真值删掉了",
              "sess-A" not in sched._session_target_grants,
              repr(sched._session_target_grants))

        # 轮首：投影
        sched._project_session_flags("sess-A")
        check("** `/clear` 之后同一个目标重新要问（这正是用户那条测不到的）",
              wm.has_target_grant("egress", "example.com") is False,
              "★ 授权穿过了 /clear -- 形状（复用 key 就漏清）")
        check("  命令那条也一样",
              wm.has_target_grant("command", "ls -la") is False, "")
    finally:
        REG._registry.pop("workspace_manager", None)


def test_projection_unions_both_sources() -> None:
    """投影把两组来源合流：会话级 ∪ 持久表。"""
    print("\n-- 10. ★ 投影合流：本次对话的 ∪ 落盘的 --")
    import bus.registry as REG
    from core.pipeline.scheduler import PipelineScheduler, _PENDING_STORES

    WL, _ = _with_tmp_allowlist()
    WL.add("egress", "persisted.com")
    wm = _RealWM()
    REG.register_workspace_manager(wm)
    try:
        sched = PipelineScheduler.__new__(PipelineScheduler)
        sched._session_target_grants = {"sess-A": {"egress": {"session.com"}}}
        sched._session_skip_delete_approval = set()
        sched._session_cross_zone_auto = set()
        sched._approval_ask_counts = {}
        # 命令的工作目录（「框架记住 cwd」）。投影读它，所以这个
        # 部分构造的替身也得有 —— 少一格就是 `AttributeError`，
        # 而那正好说明"投影真的在读它"（比静默容忍好）。
        sched._session_cwd = {}
        sched._project_session_flags("sess-A")

        check("★ 落盘的那条进来了", wm.has_target_grant("egress", "persisted.com") is True)
        check("★ 本次对话的那条也在", wm.has_target_grant("egress", "session.com") is True)
        check("  没批过的仍然要问", wm.has_target_grant("egress", "other.com") is False)

        # 手改文件之后，下一轮就该生效
        WL.save({"egress": ["handedited.com"], "command": []})
        sched._project_session_flags("sess-A")
        check("★ 手改文件 => 下一轮投影就生效（不缓存）",
              wm.has_target_grant("egress", "handedited.com") is True)
        check("  而文件里删掉的那条也跟着没了",
              wm.has_target_grant("egress", "persisted.com") is False)

        # 另一个会话不该拿到这个会话的会话级放行
        sched._project_session_flags("sess-B")
        check("★ 别的会话拿不到 A 的会话级放行（投影按 key）",
              wm.has_target_grant("egress", "session.com") is False)
        check("  但**持久表是全局的**，B 也有",
              wm.has_target_grant("egress", "handedited.com") is True)
    finally:
        REG._registry.pop("workspace_manager", None)


def main() -> int:
    global _FAIL
    print("=" * 62)
    print("W19b/shared approval gate (egress + command)")
    print("=" * 62)

    # 自带机检（同 `test_office_write` 的做法）
    #
    # 首尾各拍一次生产的 `data/allowlist.json`。
    # 为什么必须自带：这个文件的污染不会被 `check_test_pollution` 抓到
    # —— 那个检查器比的是"一次运行内的前后指纹"，而污染一旦在某次运行里
    # 落盘，之后每次运行的前后指纹都是同一个被污染的值，于是永远 PASS。
    # 踩过：`{"egress": ["*"]}` 被留在生产目录里 = 出网闸门被静默关掉。
    import hashlib
    from core.allowlist import path as _wl_path

    # 路径在开头就钉死，不能在结尾再取一次 ——
    # `_with_tmp_allowlist()` 会把 `WL._PATH` 指向临时目录，结尾再取就取到
    # 临时路径上去了（ABSENT -> 临时文件哈希，假红）。
    _wl_prod = _wl_path()

    def _fingerprint():
        if not _wl_prod.exists():
            return "ABSENT"
        return hashlib.sha256(_wl_prod.read_bytes()).hexdigest()

    _fp_before = _fingerprint()
    try:
        _run_all()
    finally:
        _fp_after = _fingerprint()

    print("\n" + "=" * 62)
    if _fp_before != _fp_after:
        _FAIL += 1
        _FAILED.append("生产白名单被这次测试改动了")
        print("!! 生产 %s 被这次测试改动了：%s -> %s"
              % (_wl_prod, _fp_before[:12], _fp_after[:12]))
        print("   这不是小问题 —— 白名单里的 `*` 等于把闸门关掉。")
    else:
        print("污染自查: 生产白名单未被触碰（%s）" % _fp_before[:12])
    return _finish()


def _run_all() -> None:
    test_tables_shape()
    test_command_has_exactly_one_tier()
    test_key_2_means_the_same_everywhere()
    test_detector_increments_per_kind()
    test_detector_rejects_unknown_kind()
    test_target_grants()
    test_settle_writes_both_halves()
    test_substate_gives_text_not_options()
    test_persistent_allowlist()
    test_option5_is_session_scoped_and_never_touches_the_file()
    test_projection_unions_both_sources()
    test_clear_really_drops_the_session_grants()


def _finish() -> int:
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
