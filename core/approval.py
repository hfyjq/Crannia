"""审批选项的单一来源。

## 为什么要有这个模块

审批有两个方向：给用户渲染选项，以及把用户的回答解析回某一项。两者必须由
同一张纯数据表派生 —— 分成两处实现时，任何一侧改了措辞或可接受答案，
另一侧都会漂移：

* 同一段提示语如果有多份副本，改一次措辞要记得改每一处，而且副本之间会不一致
  （换行、标点这类细节最先分叉）。
* 回答端如果按同义词表认（`"同意"` / `"是"` / `"可以"` / `"ok"` / `"yes"`），
  它甚至可能认不出提示语自己写出来的 `"1. 同意"`。

把"选项是什么"收成一个纯数据表，渲染与解析都从它派生。

## 这个模块不做什么

不碰 wm / ctx / IO，不认识会话。它只回答两个问题：
  * 这一次该给用户看哪几项？——:func:`options_for`
  * 用户回的这串字符对应哪一项？——:func:`parse_reply`

"用户回的不是数字怎么办""要不要发授权"这些策略留在调用方
（`core/pipeline/stages/pre_process.py` 的统一派发器）。

## 一条判据

`parse_reply` 允许"数字 + 该项自己的标签"，但数字 + 别人的标签要拒
（`"1. 不同意"` 是歧义输入，不能猜）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 界面语言。延迟 import 用不上 —— `core.i18n` 不 import 任何 core 模块。
#: 这里的模块级常量（选项表 / 默认正文 / 问题表）一律保持中文原样：
#:   `tr()` 只许在取用那一刻调（import 时翻会把语言定死，后端 `_VERBS` 同一个坑）。
from core.i18n import t as tr
from core.i18n import tr_fmt as trf

__all__ = [
    "GRANT_NONE", "GRANT_ONCE", "GRANT_SESSION", "GRANT_PERSIST",
    "ESCALATE_AT", "Option",
    "options_for", "render_prompt", "parse_reply", "find_option",
    "pending_payload", "ASK_COUNTED_KINDS",
    "REPLY_STALE", "build_payload", "reply_decision",
    "new_request_id", "substate_question",
    "REPLY_OK", "REPLY_NOT_A_CHOICE", "REPLY_BAD_CHOICE", "REPLY_STALE",
    # 出网与命令两套审批选项表
    "EGRESS_OPTIONS", "COMMAND_OPTIONS",
    # 兜底闸（不可逆工具）的选项表
    "DESTRUCTIVE_OPTIONS",
    # 计划批准 / ICE 真值放行 / 接管应用
    "PLAN_OPTIONS", "ICE_REVEAL_OPTIONS", "COMPUTER_APP_OPTIONS",
    "compact_file_list", "render_delete_body", "FILE_LIST_LIMIT",
    # 兜底闸的「被审批物」按工具定
    "APPROVAL_SCOPES", "subject_for", "allowlist_kind_for",
    # 删除那一次授权的「被审批物」
    "delete_subject",
    # 审批回传的四条固定语义
    "APPROVAL_OUTCOMES", "outcome_line",
]


# 兜底闸的「被审批物」与它对应的持久白名单域
#
# ## 判据：被审批物是目标，不是工具名
#
# `core/shell.py` 里 `subject = " ".join(raw.split())` —— 归一化后的命令原文。
# 为什么不是"工具名"？用工具名做键时，「总是允许」会变成一张空头支票：
# 批一次 `send_email` = 本会话内所有邮件都不再问，包括发给用户从没同意过的人。
#
# ## 两张表合并成一张
#
# 「被审批物是哪个参数」与「哪个持久白名单域能放它」是同一个决定的两面 ——
# 拆成两张表就会漂，而漂掉的那一支是"白名单加了地址却仍然每次都问"
# （用户看得见，但没人知道该去哪查）。
#
# | 工具 | 被审批物 | 持久白名单域 |
# |---|---|---|
# | `send_email` | `send_email:<收件人>` | `email_to`（skill 页里加，永不再问） |
# | 其余 | 工具名（默认） | 无 |
#
# 取不到参数，退回工具名 —— 那是更严的那一侧（键变了，
# 之前那条会话授权不再命中，照旧问）。fail-closed 的一侧。
APPROVAL_SCOPES: dict = {
    "send_email": ("to", "email_to"),
    #: 删记忆的"一次性"授权按条目标题：批了 A 这一条，不等于
    #:   这一轮里 B 也免问（否则"一次性"会变成"本会话所有删除都免问"）。
    #:   第二格是持久白名单域 —— 这里给空串：记忆删除没有"永远允许"。
    "memory_delete": ("name", ""),
    #: 出站按收件人授权：批一次 = 本会话内发给这个收件人不再问，
    #:   换收件人重新问。第二格给空串：出站没有"永远允许"的持久域。
    "send_outbound": ("to", ""),
}

#: 被审批物里参数与键之间那个分隔符。取一个不会出现在地址里的字符 ——
#: `:` 会（`mailto:`、地址里不会，但 URL 形式的收件人可能带），用不可见的那类风险更大。
#: 选 `\x1f`（ASCII Unit Separator）：它不可能出现在用户输入里（模型传不进来，
#: 参数是 JSON 字符串），而"两段拼起来"与"某一段恰好是另一段"因此分得开。
_SUBJECT_SEP = "\x1f"


def subject_values(tool_name: str, tool_args=None) -> list:
    """这次调用里构成被审批物的那几个值（`send_email` 的收件人）。

    单独一个函数：白名单要拿它逐个比对，而 `subject_for` 要的是拼好的串 ——
    两份"从参数里取哪几格"的代码会漂，而漂掉的那一支是
    "白名单里明明加了地址，却仍然每次都问"。只定义一次（这里）。
    """
    scope = APPROVAL_SCOPES.get(tool_name)
    if not scope or not isinstance(tool_args, dict):
        return []
    keys = scope[0]
    out = []
    for k in (keys if isinstance(keys, (list, tuple)) else (keys,)):
        v = tool_args.get(k)
        if isinstance(v, (list, tuple)):        # `to` 可能是一串
            out.extend(str(x).strip() for x in v if str(x).strip())
        elif v is not None and str(v).strip():
            out.append(str(v).strip())
    return out


def subject_for(tool_name: str, tool_args=None) -> str:
    """这一次调用的被审批物（会话授权与审批载荷共用的那个键）。

    `send_email`，`send_email␟<收件人>`；其余，工具名（与按工具名授权时一致）。
    """
    vals = subject_values(tool_name, tool_args)
    if not vals:
        # 取不到，退回工具名（更严的一侧）：键一变，之前那条会话授权
        #    就不命中了，照旧问。不编一个空收件人出来。
        return tool_name
    return tool_name + _SUBJECT_SEP + ",".join(vals)


# 删除类工具的审批对象按目标计算，不能收成一个布尔值：布尔语义下的“一次性”
# 实际等于本轮内所有删除全部免问 —— 手动模式第二个删除不再弹窗，自动模式
# 连分类器都不过。subject 因此是本次调用的参数摘要。
#
# subject 取原始请求参数，不取展开后的文件表：重放时拿的是原始参数，两边才一致；
# 展开后的清单索引可能刷新，在那里算会漂移。file_paths 需要排序，因为它的顺序
# 来自模型，不携带语义。
#
# 取不到时返回空串，调用方必须按 fail-closed 处理。pattern 分支带前缀，避免与
# 恰好同名的文件撞键。存储侧（kind="delete"）在 core/workspace.py 的两个适配器
# 里；本模块不 import 它，以避开循环依赖。
def command_subject(raw: str) -> str:
    """run_command 的被审批物：命令原文折叠空白后的字符串。

    三个读者共用它做查表键，且必须逐字相同：core/shell.py 决定是否弹窗，
    core/agent_loop.py::_pre_grant_for_auto 写入一次性授权，
    core/auto_review.py::skip_reason 判断是否已批准。键差一个字节即失配，
    症状是“已预授权但仍弹窗”。命令带换行或连续空白时必然不一致。

    折叠空白而非保留原文：换行与空格语义等价，同一批次的多条命令应合并为同一个
    键。调用点不要再自行折叠一次，统一走这里。
    """
    return " ".join(str(raw or "").split())


def delete_subject(tool_args=None) -> str:
    """`delete_files` 这一次调用的被审批物（判据见上）。取不到，`""`。"""
    if not isinstance(tool_args, dict):
        return ""
    pat = str(tool_args.get("pattern") or "").strip()
    if pat:
        return "pattern" + _SUBJECT_SEP + pat
    fps = tool_args.get("file_paths")
    if isinstance(fps, (list, tuple)):
        vals = sorted({str(x).strip() for x in fps if str(x).strip()})
    elif fps is None or not str(fps).strip():
        return ""
    else:                                   # 单个路径直接给（不是列表）
        vals = [str(fps).strip()]
    return _SUBJECT_SEP.join(vals)


def allowlist_kind_for(tool_name: str) -> str:
    """这个工具有没有持久白名单域；没有返回 `""`（`agent_loop` 据此决定查不查）。"""
    scope = APPROVAL_SCOPES.get(tool_name)
    return scope[1] if scope else ""

# 授权档位
GRANT_NONE = "none"        # 不授权（拒绝 / 进子状态）
GRANT_ONCE = "once"        # 一次性授权（消费型）
GRANT_SESSION = "session"  # 本次对话内不再询问（会话级）
#: 写进 `data/allowlist.json` 的持久档。
#:
#: 只有 `egress` 用它 —— 而且这是对一条既有规矩的破例：
#:   「对话里点出来的东西不写 `data/allowlist.json`」是有意为之
#:   （`core/allowlist.py` 的文件头 + 选项 5 的注释都写着），
#:   理由是"永久放行只能是用户在设置页明确做的"。
#:
#: 破例的理由：命令那条"总是允许"做不了前缀，批过 `git status` 之后
#:   `git status --short` 照样问（前缀匹配做不到），所以它那条"永久"本来就是空头支票；
#:   而站点是个稳定、会复现的身份，持久信任在这一格是有内容的。
#:破例是有账的（含推翻条件）。
#: 别的 kind 收到它 = 配置错误，一律拒（见 `_settle_target` 里那一支）。
GRANT_PERSIST = "persist"

# 回传的四条固定语义
#
# 审批"没成"时的回传，头一句永远是这四条之一，后面才接一句按 kind 的路线。
# （"同意"那一侧不需要 —— 它只有一个含义。不对称是刻意的，
#   见下「为什么只做拒绝这一侧」。）
#
# 判据：模型必须能分清「人说不行」与「没人可问」
# —— 后者它该换个做法（走别条路），前者它该停。否则这两件事长得一样。
#
# 面板与回传要一起分形状：
# 面板那一半早就按 kind 分表了（`_TABLES` 里每个 kind 各一套选项与升级点），
# 而回传如果七个 `_settle_*` 各写一句自己的中文，"人拒了"与"没人可问"
# 在字面上分不出来。回传也必须由一张表 + 一个拼装处产出。
#
# 为什么只做拒绝这一侧：拒绝时模型分不清两种语义是真实风险；批准那一侧
# 加一个 `approved` 只是一个同义反复（路线里本来就写着「用户已同意…」），
# 却要给每一句都加一遍税。
#: 审批回传那串头的字面量 —— `outcome_line` 是唯一拼装处。
#: 它还有一个读者：`core.auto_review` 的转写 outcome 检测 ——
#:    它靠这一串头认出"上一次调用是被拒了"。
#:放在这里而不是各写一遍，正是「一个口径一个地方」。
#:    头前面还可以有一层信封（`[系统] ` / `Error: ` / `[框架] `，见
#:    `test_approval_outcome` 第 5 条），读的人先剥信封再比。
APPROVAL_HEAD = "[审批结果]"

APPROVAL_OUTCOMES: dict[str, str] = {
    # 人拒了，模型该停。
    "rejected": "人拒了",
    # 被打断，用户给了别的话（那句话就接在路线里），不是拒绝。
    "cancelled": "被打断，不是拒绝",
    # 没人可问，模型该换个做法（走别条路），不是停下来干等。
    "unavailable": "没有可问的通道，**不是**用户的决定",
    # 框架没办成（工具基础设施故障），退赛，不替用户做拒绝决定。
    "recused": "框架这边没办成，**不算**用户拒绝",
}


def outcome_line(outcome: str, route: str) -> str:
    """审批没成时的回传头 —— 全仓唯一一处（不许各处自己写）。

    形状：[审批结果] <语义>（<白话>）—— <按 kind 的路线>
    括号那一截白话是给不认识这四个英文词的模型看的 ——
    它（而不是那个英文 token）才是"分得清"的保证。

    认不出来的 `outcome` 当场炸，不许静默回落成一句自造的话：
    那正是这一条要治的病 —— 损失不是「未知」，而是「一个确定的错误语义」。
    """
    gloss = APPROVAL_OUTCOMES.get(outcome)
    if gloss is None:
        raise KeyError(
            "未知的审批结果语义：%r（只许这四条：%s）"
            % (outcome, sorted(APPROVAL_OUTCOMES)))
    return "%s %s（%s）—— %s" % (APPROVAL_HEAD, outcome, gloss, route)


#: 第几次被问起才出现"升级项"（跨区的第 5 项）。
#: 这是疲劳加速器 —— 不能在用户最想赶紧点完的时候，把范围最大的选项递过去。
#: 两条零成本缓解：永不默认选中它 且 排在最后（`options_for` 保持表里的顺序）。
ESCALATE_AT = 3


@dataclass(frozen=True, slots=True)
class Option:
    """一项可选的回答。

    *key*      —— 用户该回的那个数字（IM 通道只认数字，见决策 2）
    *label*    —— 面板/文本里显示的那句话
    *grant*    —— 选了它发哪种授权
    *stage*    —— 非空，进入"等用户补一句话"的子状态（自由文本，不受数字规则约束）
    *escalate_at* —— 0 = 总是出现；N = 第 N 次被问起才出现
    """
    key: str
    label: str
    grant: str = GRANT_NONE
    stage: str = ""
    escalate_at: int = 0

    @property
    def is_escalation(self) -> bool:
        return self.escalate_at > 0


# 选项表（唯一拼标签的地方）
#
# 加一项之前先问三件事：
#   1. 它的 `grant` 是什么？——"不授权"也是一个明确的答案，别默认补 GRANT_ONCE
#   2. 键必须稳定：第 5 项在前两次不出现，但键永远是 "5" ——
#      用户在第二次输 5 要得到"这个数字不在选项里"，而不是被当成别的
#   3. 顺序就是显示顺序；升级项永远排最后

CROSS_ZONE_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "同意，并且我可以将范围缩小至某个区域"
                "（告诉Crannia文件可能在哪）", stage="await_location"),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
    Option("5", "本次对话内自动放行", GRANT_SESSION, escalate_at=ESCALATE_AT),
)

DELETE_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "同意，本次会话内不再询问删除操作", GRANT_SESSION),
    Option("3", "不同意"),
)

# 批量删除的硬闸
#
# 被审批物与 `delete` 同一个（一次删除动作；个数与阈值写在 `file_list` 的
# 抬头里），但只有 1 / 2 两项，而且故意没有「本次会话内不再询问」：
#
# 带着那一项，用户点一次就把 `_session_skip_delete_approval` 种下了
#本次会话后面再删 500 个也不再问，闸会把自己关掉 ——
#    而那正是这个闸存在的理由。
#
# 它自己有独立的一格（`pending_delete_bulk`），不与 `delete` 共用。
#    两套表的键语义相反：`delete` 的 `"2"` = 同意且本次会话不再询问，
#    这里的 `"2"` = 不同意，结算器也不许复用
#    （复用会把"不同意"读成"同意"），所以 `pre_process._settle_delete_bulk`
#    是单独写的。一格只许一个 kind 用（同族判据见 `_TABLES`）。
#
# 也没有「我有别的话说...」 —— 理由：这一步问的是"这个批量动作
#    要不要做"，不是"你想要什么"。
DELETE_BULK_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意"),
    Option("2", "不同意"),
)

# 出网
#
# 被审批物是目标（域名 / 收件人地址），载体在 `pend["subject"]` 里。
# 与删除那条的差别：删除的"记住"是整类（本次会话内不再问删除），
# 而这里的"记住"是按目标（本次会话内不再问这个域名）——
#第 2 项与第 5 项粒度不同，别读混：
#      2 = 只放行这一个目标（会话级）
#      5 = 放行全部目标（会话级，升级项）
#
# 第 5 项的 `escalate_at=2`：第一次问不给，第二次才给。
# 理由同 `CROSS_ZONE_OPTIONS` 的第 5 项：升级项要等人有判断力了再给；
# 第一次就给「don't ask again for {hostname}」是疲劳加速器。
EGRESS_OPTIONS: tuple[Option, ...] = (
    #: 标签写「同意本次」：`"同意"` 两个字歧义太大 —— 用户会以为它是
    #: "以后都同意"，而它只管这一轮（下一轮同一个域名照样问）。
    #:把"一次"写在脸上可以避免这种误读。
    #: 键位一个字没动 —— 它只是显示文本，回传语义仍是 `"1"`。
    Option("1", "同意本次", GRANT_ONCE),
    Option("2", "同意，本次对话内不再询问此目标", GRANT_SESSION),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
    Option("5", "同意，且本次对话内所有目标自动放行", GRANT_SESSION, escalate_at=2),
    # 唯一一处从弹窗写进 `data/allowlist.json` 的档。
    #
    # 键位取 `"6"` 而不是插在中间 —— 键位是回传的语义，
    #   把 `"3"`（不同意）往后挤会让老习惯点错：用户记得"3 = 不同意"，
    #   而重编号之后那一下会变成永久信任这个站点。那是危险的那一侧。
    #新选项一律追加在末尾，宁可让列表出现跳号。
    #
    # `escalate_at` 不设 = 第一次就给 —— 这里与选项 5 的规矩相反，
    #   是有意的例外，理由：命令的"总是允许"是空头支票（前缀匹配做不到），
    #   而站点是个稳定身份，持久信任在出网这一格有内容。
    #推翻一条既有决定要留账（含推翻条件）。
    #: 开头那个"同意"是有意的：它让这一档在视觉上与 `1`/`2` 同族
    #:   （都是"同意 + 附加范围"），而不是一个新物种。
    Option("6", "同意，且信任此站点，不再询问", GRANT_PERSIST),
)

# 执行命令
#
# 与出网同形（被审批物 = `pend["subject"]` 里的命令）。
COMMAND_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "同意，本次对话内不再询问此命令", GRANT_SESSION),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
    # 第 5 项是会话级，不落盘（`/clear` 就清掉），标签照实说「本次对话内」。
    # 如果它落盘，标签就会说谎：用户以为只活这一段对话，实际穿过 `/clear` 与重启。
    # 「全部」= 该 kind 的 `"*"`，写了它之后同 kind 的任何目标都放行
    # （这正是它比选项 2 强的地方：2 只记一条）。
    Option("5", "同意，且本次对话内所有指令自动放行",
           GRANT_SESSION, escalate_at=2),
)

# 工作区之外写文件
#
# 被审批物 = 一个目录（`pend["subject"]`，绝对路径）。
#
# 补的是"区外写"的另一半：默认情况下"工作区外写"是硬拒
# （`READ_ONLY` + 非 read/copy，`SecurityError`），但一个动作如果能过审批，
# 就不该被硬拒，改成一道审批。
#
# 上面对称的还有 (a)：「这一段对话选定的目录，免问」——
#   那一半不走这里（它是 `core/security.py::_is_session_workspace`，
#   一个免问的旁路）。两张表不重叠：能到这一格的，一定不是选定目录、
#   也不在工作区列表里。
#
# 档位：只有会话档，不给「永久」档 ——
#   理由与选项 5 那条同族：这一格天然有一条"永久"的路（把那个目录
#   加进设置页的「工作区列表」），持久放行只能是用户在设置页明确做的，
#   不许从一次弹窗里点出来。本 kind 进 `allowlist.SESSION_KINDS`、
#   不进 `KINDS`（`add()` 会拒，它永远写不进 `data/allowlist.json`）。
#
# 第 5 项与出网那条同形但更关键："本次对话内所有目标自动放行"翻成
#   本 kind 就是"以后往哪儿写都不问了"。它 `escalate_at=2`（第 2 次被问起
#   才出现）—— 与出网/执行两条同一条判据（升级项是疲劳加速器，
#   不能让它在第一次就出现）。
WRITE_OUTSIDE_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "同意，本次对话内不再询问此目录", GRANT_SESSION),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
    Option("5", "同意，且本次对话内所有目录自动放行", GRANT_SESSION, escalate_at=2),
)

# 计划批准
#
# 批准只走专用工具（`exit_plan_mode`），不许用文本或提问面板问 ——
# 所以它必须有一张自己的选项表，而不是复用别人的。
#
# 与出网 / 执行 / 兜底闸三处都不同：
#   · 那三条的被审批物是 `pend["subject"]`（目标 / 工具名），按目标授权；
#   · 这一条的"批准"动作是翻模式（退出计划模式），不是放行某个目标。
#结算器 `_settle_plan` 不复用 `_settle_target`（键位含义对不上，
#       复用会把"不同意"读成"同意"）。
#
# 没有升级项（`escalate_at` 全 0）：「本次对话内所有计划自动批准」
# 这句话没有意义 —— 计划本来一次只批一份。也不进 `ASK_COUNTED_KINDS`。
#
# 一份完整的"批准之后用哪个模式继续"（自动 / 放行编辑 / 跳过…）表对这里
# 是多余的：本仓库的模式轴只有手动 / 自动 / 计划三档，而**退出时照进入前那个
# 模式还原**，只留必要三项；将来若要"批准后直接切自动"，加在这里（一处）。
# ICE：把句柄背后的真值放行给模型
#
# 这个弹窗存在的理由，是"先把一条静默的缝堵上，再给一个显式的门"：
#   如果模型能把真值写进回复，前面的保护就全白做 —— 所以 ICE 会从那一刻起
#   永久失效，而且一行日志都没有。堵缝 + 显式门。
#
# 编号刻意保持（1 同意 / 2 同意+本会话降级 / 3 不同意）。
#   但按反疲劳判据看，第 2 项是"范围最大的那个"且第一次就给 ——
#     与 `CROSS_ZONE_OPTIONS` 第 5 项的做法相反。这里保留现有顺序；
#     要挪到最后 + `escalate_at=2` 是一行的事，但那属于产品决定。
ICE_REVEAL_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意，这次把真实内容给它", GRANT_ONCE),
    Option("2", "同意，并且本次对话内不再做这项保护", GRANT_SESSION),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
)

PLAN_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意，按计划开始执行", GRANT_ONCE),
    Option("2", "不同意，继续规划"),
    Option("3", "我有别的话说...", stage="await_other"),
)

# 兜底闸：不可逆的工具
#
# 与出网 / 执行同形（被审批物 = `pend["subject"]` 里的工具名），
# 走的也是同一套派发/结算代码（`pre_process._settle_target`）——
# 这正是不另开一套流程的收益。
#
# 但判据的来路完全不同，别读混：
#   · `egress` / `command` —— "这个目标用户批过没有"（目标来自工具参数）
#   · `destructive`     —— "这个工具没人给它做过审批发射点"
#     （见 `core/agent_loop.py::_APPROVAL_CAPABLE_TOOLS` 的 docstring）
#
# 它是一道兜底：只有没有专用审批发射点的不可逆工具会走到这里 ——
# 另外两个判为不可逆的（`delete_files` / `run_command`）各自有发射点、
# 已在能力名单里。「以后新加一个会删东西的工具，登记那一刻就被问」。
DESTRUCTIVE_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "同意，本次对话内不再询问这个操作", GRANT_SESSION),
    Option("3", "不同意"),
    Option("4", "我有别的话说...", stage="await_other"),
    # 升级项 `escalate_at=2` —— 与出网/执行同一档（不是第一次就给）。
    # 第 5 项放行的是这一类（该 kind 的 `"*"`），比第 2 项（只记这一个工具）宽得多。
    Option("5", "同意，且本次对话内所有这类操作自动放行",
           GRANT_SESSION, escalate_at=2),
)

# 命令审批只有一个 kind（`command`）
#
# 命令的 tier 仍然分三档（`deny` / `guarded` / `allow`），但 `guarded` 与
# `allow` 走同一张审批表 —— tier 只影响提示语正文（说清为什么危险），
# 不再影响给哪几个选项。
#
# 实现上是把多余的 kind 整个删掉，而不是"两张表写成一样"。
# 两个 kind 共用同一个待审批格时，"格的键"与"公开的 kind"变成两个东西，
# 每个读者都得记得去载荷里取，否则会同时坏三处：
#
#   ① 浮窗永远不出现（用格的键渲染）
#   ② 失效信号发的是错的 kind
#   ③ 「不同意」被读成「同意」，且授权发到错的 kind，无限审批循环
#
#一格只许一个 kind 用；删掉一个 kind 是从构造上消灭这一类错误。
#
# 危险并没有消失，只是换了载体：
#   · `deny` 档原样保留 —— 出网类命令（curl/wget/ssh…）仍然是
#     确定性拒绝，连问都不问，也仍然不受「关闭安全询问」开关影响
#   · "这条为什么危险"仍然照说 —— 在提示语正文里（`detail`），
#     见 `core/shell.py::_approval_prompt`

# 接管 —— 逐应用授权
#
# 逐应用 · 每会话授一次；没有"逐动作"那一档。
#
# · 「允许（本次对话）」—— 只把这一个 exe 记进接管的会话授权表
#   （`core/computer/session.py::authorize`），`/clear` 不清（接管本身也不清）。
# · 「不允许」—— 那一次工具调用回一句拒绝，不重试。
#
# 这一格的 `grant` 不用 `ctx.target_grants` 那套（那套是"按目标授权"，
#   由 `_grant_target` 写）；这里的授权真值在接管会话里
#   （`core/computer/session.py::authorize`）—— 结算器单独写一个。
COMPUTER_APP_OPTIONS: tuple[Option, ...] = (
    Option("1", "允许（本次对话）", GRANT_SESSION),
    # 永久批准 —— 写进 `data/allowlist.json` 的 `computer_app` 格；
    #   设置页「对话设置 → 接管应用」可加可删。
    #   与 `egress` 选项 6 同一个形状（代价：看得见、撤得掉）。
    Option("2", "同意，且永久批准此应用", GRANT_PERSIST),
    Option("3", "不允许", GRANT_NONE),
)

#: 外部命令钩子要求人工确认（`PreToolUse` 的 `permissionDecision: "ask"`）。
#:
#: 被审批物是这一次工具调用（`subject` = 工具名）。选项刻意只有两项：
#: 钩子问的是"这一步要不要做"，不是"你想要什么"，所以没有自由文本项、
#: 也没有任何会话级/持久放行 —— 钩子每问一次就是一次独立判断
#: （它自己的配置随时会变，替它记一个"以后都别问"会架空那个钩子）。
HOOK_ASK_OPTIONS: tuple[Option, ...] = (
    Option("1", "同意", GRANT_ONCE),
    Option("2", "不同意"),
)

_TABLES: dict[str, tuple[Option, ...]] = {
    "cross_zone": CROSS_ZONE_OPTIONS,
    # 钩子要求确认 —— 自己一格（`pending_hook_ask`）。
    "hook_ask": HOOK_ASK_OPTIONS,
    "delete": DELETE_OPTIONS,
    # 工作区之外写文件。自己一格（`pending_write_outside`）
    # —— 判据同下面那段（一格只许一个 kind 用）。
    "write_outside": WRITE_OUTSIDE_OPTIONS,
    # 批量删除的硬闸。它有自己的格（`pending_delete_bulk`）——
    # 判据同上面那段：一格只许一个 kind 用。
    "delete_bulk": DELETE_BULK_OPTIONS,
    "egress": EGRESS_OPTIONS,
    "command": COMMAND_OPTIONS,
    "destructive": DESTRUCTIVE_OPTIONS,
    # 计划批准：被审批物是计划本身，不是目标。
    "plan": PLAN_OPTIONS,
    # ICE：放行句柄背后的真值。
    "ice_reveal": ICE_REVEAL_OPTIONS,
    # 提问面板。
    # 空表是刻意的 —— 它的选项是模型在调用时给出的（2–4 项），
    #    不可能预先列进一张静态表。真正的那张表随挂起载荷走
    #    （`options=` 参数，见 `options_for` / `build_payload`）。
    # 这一格必须存在：`tests/test_approval_events.py` 按本表反查
    #    `_PENDING_STORES` —— 缺一格 = "浮窗永远不出现"。
    "ask_user": (),
    # 接管 —— 逐应用授权。自己一格
    # （`pending_computer_app`）；一格只许一个 kind 用（同 `delete_bulk`）。
    "computer_app": COMPUTER_APP_OPTIONS,
}

#: 各流程默认的开场白（调用方可以传 `body=` 覆盖，比如删除要带文件列表）。
_DEFAULT_BODY: dict[str, str] = {
    "cross_zone": "Crannia 没有在工作区找到您需要的文件，即将进行全局搜索——",
    "delete": "Crannia 想删除以下文件：",
    # 这一格必须写。缺一格的后果不是"回退到 delete 那句"
    # （那是好结果），而是 `build_payload` 走到 `_DEFAULT_BODY.get(kind, "")`
    #空正文的浮窗（"冒号后面啥也没有"）。
    # 措辞刻意与 `delete` 不同：要说清这次为什么还要问。
    "delete_bulk": "Crannia 想**批量**删除以下文件（数量超过了你设的阈值）：",
    "egress": "Crannia 想访问以下目标 ——",
    "command": "Crannia 想执行以下命令：",
    "destructive": "Crannia 想执行一个**不可逆**的操作：",
    # `plan` 这一格永远不会被用到 —— `exit_plan_mode` 每次都自带 `body`
    # （计划正文），而 `body` 非空时 `render_prompt` / `build_payload` 都不看默认值。
    # 仍然写上：缺一格的默认值会让 `pending_payload` / `build_payload`
    # 悄悄产出一个空正文的浮窗。
    "plan": "Crannia 写好了计划，请你过目：",
    # 这一格只在极端情况下会被用到 —— 生产那条路自带 `body`
    # （"想写到 <目录>，它在工作区之外"）。仍然写上：缺一格的默认值会让
    # 浮窗冒号后面啥也没有（`delete_bulk` 那条同款）。
    "write_outside": "Crannia 想在工作区之外写文件：",
    # 同上：`ice_reveal` 每次都自带 `body`（带句柄与后果说明）——
    # 这里仍然写一格，防的是"缺一格，浮窗正文为空"那个形状。
    "ice_reveal": "Crannia 想读取一个受保护的值：",
    # 生产那条路自带 body（带应用名与 exe）；这一格防的是"缺一格，浮窗正文为空"。
    "computer_app": "Crannia 想控制这个应用：",
    # 同上：`hook_ask` 每次都自带 body（带钩子给的理由与参数）——
    # 这一格防的是"缺一格，浮窗冒号后面啥也没有"。
    "hook_ask": "有外部命令钩子需要确认",
}

#: 文本通道的收尾提示。数字规则要写在提示语里 —— 同义词
#: （是/可以/行/好/ok/yes）全废了，只认数字；不写清楚用户会以为随便答都行。
_FOOTER = "回复数字即可。"


# 渲染

#: 选项 2 在命令那条上会记成一条规则 ——
#: 有规则时它的标签就是那条规则。没有规则（拿不准，只能整条匹配）时
#: 仍说老实话。
_COMMAND_RULE_LABEL = "同意，以后凡是 `%s` 都放行"


def _dynamic_options(options) -> tuple[Option, ...]:
    """模型给的那张选项表 → `Option` 元组（只读、不改语义）。

    只取 `key` / `label` —— 说明（`description`）由浮窗照 `build_payload`
    原样带出去，不进 `Option`（它是显示用的，不参与结算）。
    """
    out: list[Option] = []
    for i, raw in enumerate(options or ()):
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or (i + 1)).strip()
        label = str(raw.get("label") or "").strip()
        if key and label:
            out.append(Option(key, label))
    return tuple(out)


def options_for(kind: str, ask_index: int = 1, *,
                rule: str = "",
                options=None) -> tuple[Option, ...]:
    """这一次该给用户看哪几项。

    *ask_index* 是这是第几次问（从 1 起）。升级项按 `escalate_at` 过滤。

    *rule* 只有命令那条会用：非空，第 2 项的标签换成那条规则。
    标签必须与行为一致（标签不许说谎）—— 所以规则**由工具那侧
    算好带过来**，这里不自己推（推的话两处会漂）。
    """
    #: 给了动态表就用它 —— 提问面板的选项是模型在调用时给出的，
    #:   `_TABLES` 里不可能有那一张（那是固定审批表）。
    if options is not None:
        return _dynamic_options(options)
    table = _TABLES.get(kind)
    if not table:
        return ()
    n = max(1, int(ask_index or 1))
    opts = tuple(o for o in table
                 if o.escalate_at == 0 or n >= o.escalate_at)
    r = " ".join(str(rule or "").split())
    if r and kind == "command":
        # `tr()` 只换句子、不碰 `%s`，先翻模板再填规则（`core/i18n.py` 判据 2）。
        label = tr(_COMMAND_RULE_LABEL) % r
        opts = tuple(
            Option(o.key, label, o.grant, o.stage,
                   o.escalate_at) if o.key == "2" else o
            for o in opts)
    return opts


def find_option(kind: str, key: str, ask_index: int = 1,
                *, rule: str = "", options=None) -> Option | None:
    """按当前这次真实给出去的选项找 —— 没给出去的键不算数。

    `rule` 只影响标签，不影响键 —— 所以结算那侧不传它也找得到。
    *options* 非 `None` 时按那张动态表找（提问面板）。
    """
    for o in options_for(kind, ask_index, rule=rule, options=options):
        if o.key == key:
            return o
    return None


def render_lines(kind: str, ask_index: int = 1, *, rule: str = "") -> list[str]:
    """选项块（`1. 同意` 那几行）。只此一处拼标签。"""
    return [f"{o.key}. {tr(o.label)}"
            for o in options_for(kind, ask_index, rule=rule)]


def render_prompt(kind: str, ask_index: int = 1, *,
                  body: str = "", footer: bool = True, rule: str = "") -> str:
    """文本通道看到的整段提示语（编号菜单 + 收尾提示）。

    *body* 为空时用该流程的默认开场白；传了就用传的（删除那条要带文件列表，
    列表本身也是这一处拼的，见 `render_delete_body`）。
    """
    # 正文可能已经是拼好的（删除那条带文件列表、计划那条带计划正文）——
    #   `tr()` 查不到整段就原样返回，所以这一行对动态正文是零改动。
    head = tr(body if body else _DEFAULT_BODY.get(kind, ""))
    parts: list[str] = []
    if head:
        parts.append(head)
    parts.extend(render_lines(kind, ask_index, rule=rule))
    if footer:
        parts.append("")
        parts.append(tr(_FOOTER))
    return "\n".join(parts)


def pending_payload(kind: str, *, body: str = "", footer: bool = False,
                    ask_index: int = 1, rule: str = "", **extra) -> dict:
    """工具返回 `pending_approval` 时的统一载荷。

    所有调用点都从这里出，于是：

    * `kind` 一定在 —— `agent_loop` 靠它数"被问了几次"，浮窗靠它选选项表
    * `prompt` 一定由 :func:`render_prompt` 产出（唯一那个拼标签的地方）
    * `ask_index` 一定在 —— 浮窗要用它决定给哪几项、默认选谁

    *ask_index* 是第几次被问起（从 1 起）。调用方从 `wm.approval_ask_counts`
    那个投影上取 `+1` —— 工具没有会话身份，真值在调度器上，由 `agent_loop`
    在工具返回 `pending_approval` 时推进。
    """
    n = max(1, int(ask_index or 1))
    head = tr(body if body else _DEFAULT_BODY.get(kind, ""))
    return {
        "status": "pending_approval",
        "kind": kind,
        "ask_index": n,
        # 正文要和提示语一起带出去。
        #
        # 浮窗的渲染源是 `scheduler.pending_approval` → `build_payload(kind,
        # body=…)`，它拿不到这里拼出来的那句正文 —— 不带上它就只能退到
        # `_DEFAULT_BODY[kind]`，于是命令那条只显示到冒号就没了
        # （"冒号后面啥也没有"）。
        #
        # 修法不是让浮窗自己再拼一遍 —— 那就是"第二渲染源"
        # （同一个授权在两条通道上长得不一样）。把它带出去。
        "body": head,
        "prompt": render_prompt(kind, n, body=body, footer=footer, rule=rule),
        # 规则原样带出去 —— 浮窗那一侧（`build_payload`）
        #   靠它把第 2 项换成那条规则。空串也要带：读的人据此知道
        #   "这条没有前缀规则"，而不是"这一格没人填"。
        "rule": rule,
        **extra,
    }


#: 哪些流程的提问要计入 `ask_index`（"被问了几次"）。
#:
#: 只有 `escalate_at` 非 0 的那一档需要。删除 / 计划的选项表里
#: `escalate_at` 全是 0 —— 让它们也 +1 会污染这个数，把别人的第 5 项
#: 提前顶出来（那正是"疲劳加速器"）。
#:
#: 计数必须按 `kind` 分开：出网 / 执行 / 跨区的升级点不同
#: （出网与执行是 `escalate_at=2`、跨区是 3），共用一个标量会让
#: 批两个域名之后，跨区的第 5 项在第 1 次被问时就冒出来。
#:载体是 `dict[kind, int]`（`ctx.approval_ask_counts`）。
#:
#: 读者：`core/pipeline/stages/agent_loop.py::_check_cross_zone_pending`。
#: 改这里必须同步改 `pending_payload` 的 `kind` 取值。
ASK_COUNTED_KINDS = frozenset({"cross_zone", "egress", "command", "destructive",
                               # 它的选项表里第 5 项
                               # `escalate_at=2`，必须计数，否则那一项
                               # 永远不会出现（缺的是"升到最后一项"这条路）。
                               "write_outside"})


# 浮窗
#
# 浮窗从 state 渲染，不从事件重建。
#
# `approval.request` / `approval.resolved` 只是"去重取一次"的失效信号；
# 真渲染源是 `GET /api/approval` → `scheduler.pending_approval(session_key)`
# → 下面这个 `build_payload`。理由见 `core/events.py` 的规矩 3
# （存活性只能来自当前进程观察）—— 从事件重建的话，进程重启后日志里那两条
# 会把一个幽灵浮窗复活出来，而对应的 pending 早就不在了。

#: 中间态（等用户补一句自由文本）的问题文本。
#:
#: 放在这里而不是各自的子状态处理器里：那是同一个东西的第二份副本，
#: 而"抄成两份会各自漂移"正是本模块存在的理由（见文件头）。
_SUBSTATE_QUESTIONS: dict[tuple[str, str], str] = {
    ("cross_zone", "await_location"):
        "请告诉 Crannia 文件可能在什么位置？（例如：D盘、桌面、下载文件夹）",
    ("cross_zone", "await_other"): "有什么需要告诉 Crannia 的？",
    # 出网 / 执行：`await_other` 是同一个中间态，问题也同一句。
    # 逐个列出来是刻意的 —— `substate_question` 按 `(kind, stage)` 精确查表，
    # 不写就没有问题文本，浮窗会显示空输入框（静默的坏形状）。
    ("egress", "await_other"): "有什么需要告诉 Crannia 的？",
    ("command", "await_other"): "有什么需要告诉 Crannia 的？",
    ("destructive", "await_other"): "有什么需要告诉 Crannia 的？",
    ("plan", "await_other"): "有什么需要告诉 Crannia 的？",
}


def substate_question(kind: str, stage: str) -> str:
    """中间态该问什么 —— 文本通道与浮窗共用这一句。"""
    return tr(_SUBSTATE_QUESTIONS.get((kind, stage), ""))


# ── 不要往 `ctx.response` 里写"给用户看的审批提示"
#
# 同一条 `Option` 表在两条通道上要两种呈现 —— 文本通道要整段
# （`render_prompt`），web 交给浮窗，别两边都贴
# （否则用户会看到提示语滚一遍、浮窗再补一遍，像问了两遍）。
#
# 载体不能是 `ctx.response`：那个格子里装的是模型自己的正文。
# 写进去 = 把模型这一轮的回复整条顶掉。
#
#web 上什么都不写：浮窗从 `GET /api/approval` 的 state 渲染、
# 由 `approval.request` 事件触发，不依赖聊天流里那句话。
# 非 web 通道仍印 `prompt` 整段 —— 那边没有浮窗。


def new_request_id(kind: str) -> str:
    """一次待审批的身份。

    每次发问都必须不同 —— 浮窗靠它挡双击 / 双发 / 刷新后重放：
    陈旧请求返回 `stale`，前端关掉浮窗重取一次即可。
    """
    import uuid
    return f"{kind}-{uuid.uuid4().hex[:12]}"


def build_payload(kind: str, ask_index: int = 1, *,
                  body: str = "", stage: str = "",
                  request_id: str = "", file_list: str = "",
                  rule: str = "", options=None, multi: bool = False,
                  title: str = "", ask_question: str = "") -> dict:
    """浮窗的渲染源。纯函数：同样的入参出同样的 dict。

    这里不拼字符串 —— 那是 :func:`render_prompt` 给文本通道的事。
    浮窗拿到的是结构（哪几项、默认选谁），自己决定怎么摆。
    同一份 `Option` 表，两种渲染。
    """
    n = max(1, int(ask_index or 1))
    opts = options_for(kind, n, rule=rule, options=options)
    q = substate_question(kind, stage)
    if kind == "delete" and not body:
        body = render_delete_body(file_list)
    # 中间态不给选项：那一步用户该做的是"说一句话"，不是"选一项"。
    # 给了也是死数据（前端在 `expect === 'text'` 时根本不渲染选项），
    # 而"载荷里有一份谁也不读的选项表"正是本模块要消灭的那种副本。
    if q:
        opts = ()
    #: 动态表那一路把每项的说明也带上（浮窗要显示第二行）。
    _desc = {str(r.get("key")): str(r.get("description") or "")
             for r in (options or ()) if isinstance(r, dict)}
    return {
        "request_id": request_id or new_request_id(kind),
        "kind": kind,
        "ask_index": n,
        "body": tr(body or _DEFAULT_BODY.get(kind, "")),
        #: 提问面板：允许多选（浮窗据此把单选改成多选）。
        "multi": bool(multi),
        # 选项按表里的顺序（升级项永远最后）
        "options": [{"key": o.key, "label": o.label,
                     "description": _desc.get(o.key, ""),
                     "is_escalation": o.is_escalation} for o in opts],
        # 永不默认选中升级项：它在最想赶紧点完的时候把范围最大的选项
        # 递过去，是疲劳加速器。排在最后 + 默认选第一个
        # 是两条零成本缓解，其中"排最后"由 `options_for` 保证。
        "default_key": next((o.key for o in opts if not o.is_escalation), ""),
        # 非空，中间态：浮窗显示输入框（问题用 `question`），选项不显示
        "expect": "text" if q else "",
        #: 提问面板的原始问题与小标题。
        #:   不传的话浮窗会退到 `body`（那段带编号的菜单）—— 于是
        #:   问题那一行把选项又抄了一遍、标题变成默认的「Crannia 想问你」。
        #:   `ask_question or q`：中间态那条还是走 `q`（`expect` 与它同源）。
        "question": ask_question or q,
        "header": title,
    }


def reply_decision(*, request_id: str, current_request_id: str,
                   choice: str = "", text: str = "",
                   kind: str = "", ask_index: int = 1,
                   options=None) -> tuple[str, str]:
    """浮窗回传的唯一判定。返回 `(结果, 要转发给管线的那串文本)`。

    * 陈旧 —— `request_id` 与当前挂着的那个对不上，`(REPLY_STALE, "")`。
      挡的是双击 / 双发 / 刷新后重放：用户看到了一个已经不存在的浮窗。
    * 带了 `choice` —— 校验它在这次真给出的选项里（`find_option`），
      不在就 `(REPLY_BAD_CHOICE, "")`。不猜。
    * 只带了 `text` —— 中间态的自由文本，`(REPLY_OK, text)`。

    返回的那串直接喂给 `pre_process` 的派发器 —— 管线里只有那一个
    解析器。浮窗不自己结算，它只负责把结构化的选择变成一行文本。
    """
    if not request_id or request_id != (current_request_id or ""):
        return REPLY_STALE, ""
    if choice:
        # 提问面板允许多选，`choice` 可能是 `"1,3"`。
        #   逐个键都必须在这次真给出的表里（一个不对就整条拒 —— 不猜）。
        keys = [k.strip() for k in re.split(r"[,，、\s]+", str(choice)) if k.strip()]
        if not keys:
            return REPLY_BAD_CHOICE, ""
        for key in keys:
            if find_option(kind, key, ask_index, options=options) is None:
                return REPLY_BAD_CHOICE, ""
        return REPLY_OK, ",".join(keys)
    if text:
        return REPLY_OK, text
    return REPLY_NOT_A_CHOICE, ""


#: 被审批物超过几条就折叠、折叠时留几条。
#: 判据见下面 `compact_file_list` 的 docstring。
FILE_LIST_LIMIT = 10
FILE_LIST_PREVIEW = 5


def compact_file_list(lines, *, total_note: str = "",
                      limit: int = FILE_LIST_LIMIT,
                      preview: int = FILE_LIST_PREVIEW) -> str:
    """把已经排好版的每行文件描述收成一个字符串；超限就折叠并明说省了几个。

    这是"被审批物列表"的唯一排版策略。

    为什么要单独一个函数：选项已经收成了单一来源，正文也必须如此 ——
    同一个删除授权如果两条通道各拼一套，就会互相不一致
    （一方折叠、另一方全列）。同一句话由两处引用时，容易只有一处是真的。

    折叠时必须显式标注省略了多少：
    "被审批的文本就是被同意的文本"，藏掉尾巴等于让用户同意他读不到的东西。
    所以本函数只折叠、不静默丢弃 —— 省掉几个、总共几个，一定写在正文里。

    *lines* 是每行已经排好版的描述（两条通道能拿到的信息不同：
    文本通道有大小、`scheduler` 只有路径，所以排版留在各自那儿）。
    本函数只管收多少、怎么标注。
    """
    lines = list(lines)
    if len(lines) <= limit:
        return "\n".join(lines)
    head = "\n".join(lines[:preview])
    more = trf("... 以及另外 {n} 个", len(lines) - preview)
    return f"{head}\n  {more}{total_note}"


def render_delete_body(file_list: str) -> str:
    """删除那条的开场白 —— 带文件列表。

    单独一个函数是因为列表也是提示语的一部分：内容无界会把按钮顶出视口，
    而判据是"被审批的文本就是被同意的文本，藏掉尾巴等于让用户同意他读不到
    的东西"。截断策略因此属于渲染层，并且要显式标注省略了多少
    （调用方拼 `file_list` 时已经这么做）。

    末尾那个 `\n` 不是手滑：`render_prompt` 还会在 body 与选项块之间补一个
    换行，而排版是空一行再接选项（`...{file_list}\\n\\n1. 同意`）。
    补上它才能保持这个逐字形状（有断言钉着）。
    """
    head = tr("Crannia 想删除以下文件：")
    return f"{head}\n{file_list}\n"


# 解析
#
# 三种结果，调用方据此决定说什么：
#   REPLY_OK            认出来了，`key` 是选项键
#   REPLY_NOT_A_CHOICE  根本不是一次选项选择（非数字 / 空）
#   REPLY_BAD_CHOICE    是数字，但不在这次给出的选项里（含"数字+别人的标签"）

REPLY_OK = "ok"
REPLY_NOT_A_CHOICE = "not_a_choice"
REPLY_BAD_CHOICE = "bad_choice"
#: 浮窗专用：`request_id` 与当前挂着的那个对不上（双击 / 双发 / 刷新后重放）。
#: 文本通道不会有这个结果 —— 它没有 request_id 的概念。
REPLY_STALE = "stale"

#: 全角数字 -> 半角。中文输入法下很容易打出全角，那不是用户的错。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

#: 数字后面允许的分隔符（`1` / `1.` / `1、` / `1，` / `1 同意`）
_SEP = r"[\s.、,，:：]*"


def parse_reply(text: str, kind: str,
                ask_index: int = 1) -> tuple[str, str]:
    """把用户回的那串字符解析成 `(结果, 选项键)`。

    只认数字（决策 2：非 Web 通道也走这里，同义词全部作废）。
    允许数字后面跟该项自己的标签（`1. 同意` ✅），
    但不许跟别人的标签（`1. 不同意` ❌ —— 那是歧义输入，不能猜）。
    """
    raw = (text or "").strip()
    if not raw:
        return REPLY_NOT_A_CHOICE, ""
    raw = raw.translate(_FULLWIDTH_DIGITS)

    m = re.match(r"^(\d+)" + _SEP + r"(.*)$", raw, re.S)
    if not m:
        return REPLY_NOT_A_CHOICE, ""

    key, rest = m.group(1), m.group(2).strip()
    opt = find_option(kind, key, ask_index)
    if opt is None:
        return REPLY_BAD_CHOICE, key
    # 标签原样与译名都认：界面语言换了之后，用户照着屏幕抄的可能是译名
    #   （`1. Allow once`）—— 只认中文会让那条回话变成"歧义输入"。
    if rest and rest not in (opt.label, tr(opt.label)):
        return REPLY_BAD_CHOICE, key
    return REPLY_OK, key
