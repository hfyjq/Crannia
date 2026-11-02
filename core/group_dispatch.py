"""多 agent 群聊 —— 给某个成员派一句话。

群 ≠ 常驻的 `run_subagent`（后者匿名、一次性；群要有名字、可多次调用的成员）。
群 = 新的一层（成员注册表 —— 见 `core/groups.py`）。
**但"给某成员派一句话"这个动作，直接复用 `run_child` 那台 fork 机器**
  （同前缀、只回一段话）—— 不另造一套子调用。

这个模块只做三件事，一件也不多做：

| | |
|---|---|
| ① 把成员的名字变成一个 provider | `ensure_provider` —— 成员那格 `model` 是空的就沿用会话那个；填了就当场把它装起来（框架启动时只实例化当前那个，别的只登记元数据） |
| ② 把"你是谁"写进 directive | `build_member_directive` —— 四段式不重写：它是由 `subagent.build_directive` 拼出来的（一个口径一个地方） |
| ③ 一次派活 | `dispatch` —— 跑一趟 `run_child`，把结论、成员名、模型、token 数一起交回 |

## 把它接成模型碰得到的工具 `ask_member`

判据：只在群会话里发它（缓存按前缀匹配）：

| | |
|---|---|
| 为什么不常驻 | schema 是每条请求的税。普通对话一个字都不该为群聊付钱。 |
| 为什么"只在群会话里发"就够 | 群本来就是独立会话（`session_key`），而且这个判断在会话内是稳定的（同一个群，同一份 `tools=`），前缀只增不改，代价不外溢。 |
| 唯一一处会改写前缀的 | 改成员表（加/删/改成员）。那是一次冷启动，而它本来就是一件"用户改了设置"的显式动作。 |

成员名不进 schema（那是关键的一格）：schema 逐字恒定（一个群、八个群、
零个群都是同一串字节），成员名走工具回执（不传 `name` 就列出来，
形状同 `todo_write`）。加一个成员不会改写 `tools=` 那一段。

## 一层就够

有主理人，主理人派活、成员是 `depth 1`；成员自己不派活，不撞 `can_spawn()`。
这里不放开嵌套：`run_child` 那两道闸（深度 / 平衡）原样生效，一个都不绕。
"""

from __future__ import annotations

from typing import Any, Callable

from core import groups as groups_mod
from core import subagent

#: `dispatch` 失败时的回执形状 —— 与 `run_child` 同族
#: （`{"success", "output", "error"}`），好让上层不必按来源分两种写法。
#: `member` / `model` 两格成功失败都带上（失败时也要说得出是哪一个成员
#: —— 否则回执读起来像"某一个成员失败了"）。


def member_of(group: dict, name: str) -> dict | None:
    """群里叫 *name* 的那个成员。没有回 `None`（同 `groups.get`：
    "没有这个成员"与"这个成员是空的"是两件事）。"""
    want = str(name or "").strip()
    if not want:
        return None
    for m in (group or {}).get("members") or []:
        if str(m.get("name") or "") == want:
            return dict(m)
    return None


def member_names(group: dict) -> list[str]:
    """群里所有成员的名字（给"名字写错了"那条回执当路线用）。"""
    return [str(m.get("name") or "") for m in (group or {}).get("members") or []]


# ----------------------------------------------------------------------
# ① 成员的名字 → provider
# ----------------------------------------------------------------------

def ensure_provider(model_name: str, *, fallback=None, config: dict | None = None):
    """把成员那格 `model` 变成一个能用的 provider 实例。

    三级：
      · 空名，直接给 *fallback*（会话那个）—— 这是多数情况（用户没给这个成员挑模型）
      · 已经注册过，直接用（同一个模型被两个成员选了，不该装两遍）
      · 没注册过，从 `providers/<名>/` 即时装载一次（`load_provider` 会顺带注册）

    为什么需要"即时装载"这一档：`bus/core.py` 启动时
    只实例化当前那个 provider（其余"只登记元数据，留给 Web UI 当目录"）。
成员选了 `anthropic` 而当前是 `deepseek` 时，`get_provider("anthropic")`
    会抛 KeyError —— 那不是"这个模型不能用"，是"我们还没把它装起来"。

    装不上（名字写错 / 目录不在 / 缺 key），抛 `KeyError`，并带上可用名单。
      判据：拒绝要带路线 —— "没有这个模型"与"这个模型还没配过"
      对用户是两件事（要求用户优先在"配置页"已配过）。
    """
    name = str(model_name or "").strip()
    if not name:
        if fallback is None:
            raise KeyError("这个成员没有指定模型，而这次调用也没有会话默认模型可沿用")
        return fallback

    from bus.registry import get_provider, list_providers
    try:
        return get_provider(name)
    except KeyError:
        pass

    # 先认端配置条目（`m` + 8 位）：
    #   端配置页已经有了，所以成员那一格存的是条目 id。
    #   老账本里可能是供应商目录名（`anthropic` 之类），认不出时
    #   继续往下走原来那条路，不破坏旧值。
    #   装不上时拒绝并带路线，绝不静默退回会话模型（那样用户以为
    #   用上了配置的模型，其实没有 —— 「标签在说谎」）。
    try:
        from core import model_configs as _mc
    except ImportError:                                         # pragma: no cover
        _mc = None
    if _mc is not None:
        cid = name if _mc.is_id(name) else ""
        if not cid:
            found = _mc.find_by_ref(name)       # 配置名（撞名回 None，不猜）
            cid = str((found or {}).get("id") or "")
        if cid:
            if not _mc.get(cid):
                raise KeyError(
                    "端配置里已经没有这条模型配置（%s）—— 它可能被删了；"
                    "去「端配置」重建一条，或把这个成员的模型改掉"
                    "（留空 = 沿用当前会话的模型）" % cid)
            why = _mc.preflight(cid)
            if why:
                raise KeyError(
                    "这条模型配置用不了：%s —— 去「端配置」把它修好，"
                    "或换一条（留空 = 沿用当前会话的模型）" % why)
            inst = _mc.instance_for(cid)
            if inst is None:
                raise KeyError(
                    "这条模型配置装不上（%s）—— 去「端配置」检查地址与 api，"
                    "或换一条（留空 = 沿用当前会话的模型）" % cid)
            return inst

    # 没注册过、也不是端配置条目 —— 看看 `providers/` 下有没有它的目录
    # `providers/` 是代码，按安装根找（安装版里 cwd 已经被挪到数据根了）。
    from core import paths as _paths
    entry = _paths.asset_dir("providers") / name
    if not (entry / "manifest.yaml").exists():
        raise KeyError("没有叫 '%s' 的模型/供应商（已注册的：%s）"
                       % (name, "、".join(sorted(list_providers())) or "一个都没有"))

    if config is None:
        from bus.core import load_config
        config = load_config("config.yaml")
    from bus.loaders.provider_loader import load_provider
    load_provider(entry, config)          # 内部会 register_provider
    return get_provider(name)


# ----------------------------------------------------------------------
# ② "你是谁" —— 拼在四段式前面，四段式本身一个字不重写
# ----------------------------------------------------------------------

def build_identity(member: dict, group: dict) -> str:
    """成员的自述段。三条分支，与群聊的两条拓扑一一对应。

    "这个群有没有主理人"只从 `groups.host_of` 问一次 —— 别在这儿
    再遍历一遍 members（那就是第二份判据，会各自漂）。
    """
    name = str(member.get("name") or "")
    role = str(member.get("role") or "").strip()
    gname = str((group or {}).get("name") or "未命名群聊")
    host = groups_mod.host_of(group or {})

    if member.get("host"):
        who = "你是这个群的**主理人** —— 替团队对外说话的那个人。"
        # 主理人要事先问用户需不需要协助分工
        #   （表单入口 + 提示词兜底，两者都要）。
        #   只在职责还没填全时问 —— 都填好了还问一遍就是噪声，
        #   而且这句话按每次派活计费。
        missing = groups_mod.missing_roles(group or {})
        if missing:
            who += ("★ 还有 %d 位成员（%s）没写职责。开工前先问用户一句："
                    "要不要你帮忙把分工定下来？用户说不用就直接开始；"
                    "用户说要，就按名册给每人一条职责建议，等用户确认后再派活 —— "
                    "**不要替用户自己定。**"
                    % (len(missing), "、".join(missing)))
    elif host:
        who = ("这个群的主理人是 **%s** —— 你的结论会回到**它**那里，"
               "用户只看它说的话。" % host)
    else:
        # 措辞要按 `ask_member` 的真实语义：结论回到群会话
        #   （谁调用谁决定要不要展示）。
        #   无主理人的新发言层（`GroupReplyStage`）是另一条路：那里成员回复
        #   直接可见，用的是 `group_reply.build_member_system_block`，
        #   不经过本函数。
        who = "这个群**没有主理人** —— 你的结论会回到群会话。"

    line = "【你是谁】你是群「%s」里的成员 **%s**。" % (gname, name)
    if role:
        line += "（%s）" % role
    return line + who + "\n\n"


def build_member_directive(member: dict, group: dict, task: str) -> str:
    """成员的 directive = 自述段 + `subagent.build_directive(task)`。

    一个字都不许在这里重写那四段（任务 / 别重复父做过的 / 只回一段话 /
    找不到就明说）—— 它们是那一整套判据的载体，抄一份就是两份会漂的东西。
这里的唯一新增是"你是谁"（子 agent 没有名字，群成员有）。
    """
    return build_identity(member, group) + subagent.build_directive(task)


# ----------------------------------------------------------------------
# ③ 一次派活
# ----------------------------------------------------------------------

def _fail(error: str, *, member: str = "", model: str = "") -> dict:
    return {"success": False, "error": error, "member": member, "model": model}


def dispatch(*, group_id: str, member_name: str, task: str,
             snapshot, assistant_msg: dict, provider,
             max_turns: int, config: dict | None = None,
             emit: Callable | None = None) -> dict:
    """给群里的某个成员派一句话，只收它那一段结论。

    返回 `{"success", "output"|"error", "member", "model", "tokens"}`。

    它不碰群会话的 `messages` —— 成员拿的是 `build_fork_prefix` 出来的
    一次性副本，结论回给调用方（谁调用谁决定要不要写进群里）。
    （成员不共用这条 messages。）

    四道拒绝，每道都带路线（拒绝不许悬空）：
    群不存在 / 成员不存在 / 任务为空 / 模型装不上。
    """
    group = groups_mod.get(group_id)
    if not group:
        return _fail("没有这个群聊（%s）—— 它可能已经被解散了；这一条调用已跳过。"
                     % (group_id or "（空 id）"), member=member_name)

    member = member_of(group, member_name)
    if member is None:
        return _fail("群里没有叫 %r 的成员。现有的成员是：%s —— 用其中的一个名字重试。"
                     % (member_name, "、".join(member_names(group)) or "（一个都没有）"),
                     member=member_name)

    text = str(task or "").strip()
    if not text:
        return _fail("派给 %s 的任务是空的 —— 把要做的事写清楚再派。"
                     % member["name"], member=member["name"])

    model = str(member.get("model") or "")
    try:
        member_provider = ensure_provider(model, fallback=provider, config=config)
    except KeyError as exc:
        return _fail(
            "成员 %s 用不了模型 %r：%s —— 去设置页把这个模型配好，"
            "或者把它的 model 改成一个已配过的（留空 = 用当前会话的模型）。"
            % (member["name"], model, exc), member=member["name"], model=model)

    result = subagent.run_child(
        snapshot=snapshot,
        assistant_msg=assistant_msg,
        task=text,                       # 只用于日志/兜底；真正发出去的是下面那份
        directive=build_member_directive(member, group, text),
        provider=member_provider,
        max_turns=max_turns,
        emit=emit,
    )
    out = {"member": member["name"], "model": model or "（会话默认）"}
    if not result.get("success"):
        out.update(success=False,
                   error="成员 %s 没做成：%s"
                         % (member["name"], result.get("error", "未知原因")))
        return out
    out.update(success=True, output=result["output"], tokens=result.get("tokens"))
    return out


# ----------------------------------------------------------------------
# ④ 模型碰得到的那个口：`ask_member`
# ----------------------------------------------------------------------

def roster_text(group: dict) -> str:
    """成员名册（不传 `name` 时的那条回执）—— 成员名只从这里出去。

    它是纯文本、不套 JSON 信封：`ask_member` 在
    `_ISOLATED_DOC_TOOLS` 里，出去时会被 `<user_document>` 包住
一个 JSON 信封会被包成半截。同 `run_subagent` 那条判据。
    """
    host = groups_mod.host_of(group or {})
    lines = ["这个群「%s」有 %d 个成员："
             % ((group or {}).get("name") or "未命名群聊",
                len((group or {}).get("members") or []))]
    for m in (group or {}).get("members") or []:
        bits = [str(m.get("name") or "")]
        if m.get("host"):
            bits.append("（主理人）")
        if m.get("role"):
            bits.append(" — %s" % m["role"])
        if m.get("model"):
            bits.append(" [模型 %s]" % m["model"])
        lines.append("  · " + "".join(bits))
    if host:
        lines.append("要派活就写 `ask_member(name=\"名字\", task=\"做什么\")`。")
        missing = groups_mod.missing_roles(group or {})
        if missing:
            lines.append("★ 还有 %d 位成员没写职责（%s）—— 派活前先问用户要不要你帮忙分工。"
                         % (len(missing), "、".join(missing)))
    else:
        lines.append("这个群**没有主理人** —— 成员回复由群会话层展示。")
    return "\n".join(lines)


#: `ask_member` 的 schema。逐字恒定（判据见模块 docstring 末尾那一段）：
#: 里面不许出现任何成员名 —— 出现了就变成"改成员 = 改写 `tools=` 前缀"。
#: 描述里每个字都按每条群会话请求计费（改措辞必须重量一次）。
ASK_MEMBER_TOOL_SCHEMA: dict = {
    "description": (
        "把一件事派给这个群里的某个成员，只收回它的一段结论（它不是子 agent，"
        "是群里**有名字**的一员；你看不到它的过程）。"
        "**不传 name** 就先列出群里的成员和各自的角色/模型。"
        "★ 只有两件事值得派：读一堆东西只要个结论、几件真能并行的独立小活。"
        "★ 你自己一两步能做完的别派 —— 每次派活都要付一整个上下文的钱。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "成员的名字（省它就只列名册，不派活）"},
            "task": {"type": "string",
                     "description": "要它做什么（写清楚：它只有上下文快照 + 这句话）"},
        },
    },
}


def dispatch_from_session(*, session_id: str, tool_args: dict, snapshot,
                          assistant_msg: dict, provider, max_turns: int,
                          config: dict | None = None,
                          emit: Callable | None = None) -> str:
    """`agent_loop` 那一支的唯一入口 —— 回一句纯文本给模型看。

    为什么是纯文本不是 JSON：本工具在 `_ISOLATED_DOC_TOOLS` 里，出这一支时
    `_format_tool_result` 会把它包进 `<user_document>`，一个 JSON 信封会被
    包成半截（包裹，检测器不认 / 结构没了）。
    判据与 `run_subagent` 那边逐字同源（不另发明一种回执）。

    群 id 从会话键拆（`session_key.group_id_of`，一处定义）——
    不看调用方传了什么，也不从 `tool_args` 里读（那会让模型能指向别的群）。
    """
    from core.session_key import group_id_of

    gid = group_id_of(session_id)
    group = groups_mod.get(gid)
    if not group:
        # 正常到不了（工具只在这个群存在时才发）—— 到得了只有一种情况：
        #   这一轮跑着的时候群被解散了。说实话，别指一条还不存在的路。
        return "这个群已经不在了（被解散了）—— 这一条调用已跳过。"

    name = str((tool_args or {}).get("name") or "").strip()
    if not name:
        return roster_text(group)

    r = dispatch(group_id=gid, member_name=name,
                 task=str((tool_args or {}).get("task") or ""),
                 snapshot=snapshot, assistant_msg=assistant_msg,
                 provider=provider, max_turns=max_turns,
                 config=config, emit=emit)
    if not r.get("success"):
        return "[群] " + str(r.get("error") or "成员没做成")
    return "[%s] %s" % (r.get("member"), r.get("output"))
