"""投入档（`effort_level`）—— 一处定义。

## 它是什么

一条从「更快」到「更智能」的档位轴。它不是"一个 1–5 的开关"，
而是若干根轴；本模块不新增散轴，而是把「管能力与代价」的那几根收进
一张具名的表，避免同类设置散落在多个平面。

分界线（不许越）：档位管能力与代价（子 agent · 并发 · 审批严格度 ·
轮次预算 · 思考模式），不管用户口味（逐字输出 · 记忆接续 · 主题）——
那几根是用户自己的开关，调档不许顺手改掉它们。

## 三条硬约束（进缓存键，不是普通设置）

实际证据：把 effort 设成 `low` 会导致缓存命中量出现 45 倍尖峰 ——
因为 thinking config 是缓存键的一部分。第二处独立证据（侧问那条路）：
**不要覆写 thinkingConfig：thinking 是 API 缓存键的一部分，与主线程的配置
分叉会让提示缓存失效。**
判据是「任何 fork 出来的调用（含辅助调用）也不许改档」。

1. 档位在会话内必须稳定 —— 它进缓存键，不是普通设置；
2. 改档 = 一次明确的冷启动，且要出声；
3. 绝不每请求重算档位。

落实方式是冻结：`current_level(session_id)` 第一次被问时读一次设置，之后
钉死在这个会话上（改设置只影响下一个会话），`/clear` 才清 —— 与
`<git_status>` 的每会话快照同一个形状（进程内冻结 + `/clear` 清）。
别给它加 TTL：TTL 小于「两次请求的间隔」就等于没有缓存，大于则收益归零、
代价照付。

## 拒，不静默

| | 情形 | 行为 | 为什么 |
|---|---|---|---|
| 读不到，默认 | 文件没有 / 没有这个键 / 文件坏了 | 回 :data:`DEFAULT_LEVEL` | 一个坏文件不许让一轮对话失败 |
| 读到了但做不到，拒 | 值不合法 / 档 6 未实现 | 抛 :class:`TierUnsupported` | 这里我们知道用户要什么，做不到就必须出声 |

不采用的两个反例：配置非法被静默吞掉 · 配置非法被静默夹取。
其余做法都拒。

## 只有两格随档

工具面 · 子 agent 数/深度 · maxTurns · 压缩阈值 · 超时 · 并发，这几根
没有一根有证据支持"它该随档变化"。

> "手段随档增长"是本框架独有的设计，外面找不到可比的先例。

结论：档表里只留两格随档变，别的每一档都相同（= 它们不是档位的一部分，
只是恰好也放进了这张表，好处是"这一轮用了哪些值"仍然只有一处可查）：

| 随档变的 | 为什么是它 |
|---|---|
| `subagent_slots` | 子 agent 的个数 —— 本框架的发明 |
| `effort`（挂在 `thinking` 上） | `effort` 本身就是「档」的本体 —— 六档全开，差别只在那一个字符串（档名） |

其余七根（`subagent_turns` · `tool_concurrency` · `tool_result_max_chars` ·
`max_turns` · `command_timeout_ms` · `approval_ceiling` · `tool_profile`）
六档全部相同，值见 :data:`TOOL_CONCURRENCY` 那一组常量（每一个都对着生产
里的真身，有断言钉着）。

只留两格的理由不是"想少了"，是"零先例 + 没有证据"：其余旋钮的差别是
草案数，没有任何读数支持；**做法上宁可少动一格，
也不要多动一格没法证伪的。**

## 各旋钮的值与出处（每个数字都要说得出它是哪来的）

| 旋钮 | 值 | 出处 |
|---|---|---|
| `tool_concurrency` | 10 | 没有出处：沿用接入时的取值 |
| `subagent_turns` | 8 | `core/subagent.py`（`SUBAGENT_MAX_TURNS`） |
| `max_turns` | `None` = 不设限 | 转圈由重复/循环断路器管，不该由轮数上限兼职管"任务多大" |
| `tool_result_max_chars` | 50000 | 唯一源就是本文件的常量（`config.yaml:162` 那个值已不生效，阶段会打一行出声）。对齐常见的 50k |
| `command_timeout_ms` | 30000 | `core/shell.py::DEFAULT_TIMEOUT_MS` —— 实际中位 39ms，120 秒的默认值从没派上用场，撞上就是白等两分钟 |
| `thinking` | 六档全开，`effort` = 档名 | 见 :class:`ThinkingSpec` |

读一个配置项时，先问"这个值生产从哪儿取"，别读它的默认参数 ——
默认参数是那条路的兜底，不是它的现状。同类现象：窗口的声明值与真实值
可能差 8 倍；探针也可能喂错了源。

没有进表的（各有理由，别照「档位管一切」推理）：
`_REPEAT_TOOL_LIMIT`（那是刹车，与投入无关，调它会改安全语义）·
输入校验常量（不是成本旋钮）· 防滥用那两句（`subagent` 的工具描述 + directive）——
后者是常量，不进档表：防滥用写在提示词/工具描述里，与档位无关。
"""

from __future__ import annotations

import json
import threading
from dataclasses import MISSING, dataclass, fields
from pathlib import Path

# 名字与常量

#: 设置里的键名。对外叫「投入档」，对内/落盘叫 `effort_level`
#: （它不只管推理，所以不能叫 `reasoning_effort`：档位还管子 agent 位、
#: 并发、超时这些"手段"。档名是框架自己拥有的标识符，不绑任何一家
#: provider 的参数名）。
SETTING_KEY = "effort_level"

#: 默认档（= 当前生产默认）。用户没表态时用它。
DEFAULT_LEVEL = 3

#: 设置文件的相对路径。与 `core/pipeline/stages/agent_loop.py::_stream_enabled` /
#: `core/allowlist.py` 同一条规矩：相对路径可被测试的 `sandbox()` chdir 钉住，
#: 绝对路径会改到生产设置。
_SETTINGS_PATH = Path("data/settings.json")

#: 受管旋钮的唯一一份名单。档表是它的视图，不是反过来。
#: 判据：一条测试反查「每一个旋钮在每一档里都有定义」——
#: 漏一个的后果是那个旋钮静默用上一次的值（用户看不到任何异常）。
MANAGED_KNOBS: tuple[str, ...] = (
    "subagent_slots",        # 每轮允许派几个子 agent（0 = 不给这个工具）
    "subagent_turns",        # 每个子 agent 的轮次预算
    "tool_concurrency",      # 只读工具并发上限（1 = 全串行）
    "tool_result_max_chars", # 单条工具结果进上下文的上限
    "max_turns",             # 一轮对话的主循环上限
    "command_timeout_ms",    # `run_command` 单次超时（0 = 不给这个工具）
    "thinking",              # 思考模式（步 0 探针验证过的那根）
    "approval_ceiling",      # 审批最多能放宽到哪一档（只能收窄）
    "tool_profile",          # 常驻集档（步 2 定义三个集合）
)

#: `TierSpec` 里不是旋钮的那几格。存在的意义：任何一格都必须被分类
#: —— 新增字段忘了归类，测试红（否则它就是「一个没人读的字段」）。
METADATA_FIELDS: tuple[str, ...] = ("level", "name", "implemented")

#: `approval_ceiling` 的取值（越小越严）。只能收窄，不能放宽 ——
#: 落点是 :func:`effective_approval`。
APPROVAL_ALWAYS_ASK = 0   # 每次都问，且不给「别再问」
APPROVAL_REMEMBER = 1     # 可以记住
APPROVAL_RELAX = 2        # 设置页那个「关闭安全询问」开关可以生效

#: `tool_profile` 的取值。这里只声明"是哪一档"，不声明"剔掉谁"。
PROFILE_MINIMAL = "minimal"
PROFILE_READ = "read"
PROFILE_FULL = "full"


class TierUnsupported(RuntimeError):
    """拒：这一档不合法，或还没实现。

    消息自带完整交代（档号 + 为什么 + 现在能用什么）—— 因为唯一的读者是
    用户：写入路径（`/api/settings` 的 POST）把它原样放进 400 的响应里，
    阅读路径（步 2 起）把它原样变成一句可见的拒绝。**别在这里只写一句
    「invalid tier」** —— 那样"出声"就等于没出声。
    """


# 表

@dataclass(frozen=True)
class ThinkingSpec:
    """思考模式 —— 一根轴、一个 wire 值。

    ## 它长这样

    · `enabled` —— 六档全开。判据是实际三次对比：思考关着时三次死循环、
      思考开着时一次正常（`reasoning_content` 19 条 / 61,404 字 vs 0 条）。
    · `effort` —— 档名本身就是那个串。这才是"档"的本体：它只改这一个
      字符串，别的手段随档增长没有先例。
    · ~~`budget_tokens`~~ —— 删。它的用法与本模块
      相反（它夹在 `max_tokens-1` 之内 —— 当上限的是 `max_tokens`，
      不是它）。写 1024、上游跑了约 4,500，把它当上限用并不生效。

    ## `thinking` 与 `reasoning_effort` 要「一起发」

    测"思考强度"时，`thinking` 这个键必须同时发出去 —— 在"思考关着"的前提下
    去测强度，必然量不出差别（构造性错误）。**`thinking` 与
    `reasoning_effort` 是「一起发」的**。

    ## 谁负责"这家认不认"—— 不在这里

    档表只说我们要什么；认不认由 provider 侧查表决定
    （`core/window_estimate.py::lookup_reasoning` → provider 的 `_thinking_body`）。
    为什么是「能力声明 + 发送前拦」而不是"撞 400 再降级"：撞到 400 的时候，
    一次本可以预防的错误已经变成用户可见的失败，而那一轮已经花掉了。
    实际佐证：`models.dev` 给 `deepseek-v4-flash` 声明的是
    `["low","high","max"]`，而**逐档试过去
    `none/minimal/low/medium/high/xhigh/max/ultra` 全部 `ok`
那个 `values` 是文档，不是闸；真正的闸只有"这家声明了 `effort` 吗"。
    """
    enabled: bool
    #: 线上真发的那个串。`None` = 这一档不发 `reasoning_effort`。
    effort: str | None = None


@dataclass(frozen=True)
class TierSpec:
    """一档的全部取值。

    每一格都不许有默认值（`tests/test_tiers.py` 有断言）：有默认值就等于
    给了「忘记给某一档写这一格」一条静默的路 —— 而"静默用上一次的值"正是
    要防的东西。结构性禁止比"记得写"可靠。
    """
    # 元数据（`METADATA_FIELDS`）
    level: int
    name: str
    implemented: bool

    # 受管旋钮（`MANAGED_KNOBS`，一个不多一个不少）
    subagent_slots: int
    subagent_turns: int
    tool_concurrency: int
    tool_result_max_chars: int
    #: `None` = 不设限（理由见 `MAX_TURNS` 那段）。
    max_turns: int | None
    command_timeout_ms: int
    thinking: ThinkingSpec
    approval_ceiling: int
    tool_profile: str


#: 不随档的那些从哪来 —— 它们必须与生产里的常量同源，
#: 否则"档表"就成了一句空话。`tests/test_tiers.py::test_default_tier_equals_today`
#: 把这几条钉死在生产常量的真身上。
#:
#: | 旋钮 | 值 | 生产里那个常量 |
#: |---|---|---|
#: | `tool_concurrency` | 10 | 没有出处：沿用接入时的取值 |
#: | `subagent_turns` | 8 | `core/subagent.py`（`SUBAGENT_MAX_TURNS`） |
#: | `max_turns` | `None` = 不设限 | 见下面 `TIERS` 之前那一段 |
#: | `tool_result_max_chars` | 50000 | 唯一源是本文件的常量 —— `config.yaml:162` 那个值已不生效（阶段会打一行出声）。对齐常见的 50k |
#: | `command_timeout_ms` | 30000 | `core/shell.py::DEFAULT_TIMEOUT_MS` —— 实际中位 39ms，120 秒的默认值从没派上用场 |
TOOL_CONCURRENCY = 10
SUBAGENT_TURNS = 8
#: `max_turns` 的默认不设限 —— 值写在 `TIERS` 那六格里（都是 `None`）。
#:
#: ## 为什么
#:
#: 一道任务跑到 30 轮被掐时，模型自己的交代可能是「还有题没动」—— 它**没转圈、
#: 没截断、没报错，只是还有活没干完**，撞到上限掐掉的是"用户的活"，
#: 不是"这一次循环"。
#:
#: 那个 `30` 是残值：
#: `steps` 来自已删除的 plan 引擎，恒空，恒 30；这个数没有经过重估。
#:
#: ## 为什么不影响"防跑疯"
#:
#: 「转圈」本来就有专闸 —— 重复/循环断路器，而且是硬停。
#: 提醒而不停下不叫闸：它把判断推回给模型，而模型正是那个在转圈的。
#:该管"转没转圈"的那根闸已经在位；`max_turns` 不该兼职管"任务多大"。
#: 另有压缩阈值（85% 窗口）与 `/stop` 兜着。
#:
#: ## 为什么写 `None` 而不是把旋钮删掉
#:
#: `MANAGED_KNOBS` + `_KNOB_CONSUMERS` + 两条读者一个字不用动；
#: 而且"不设限"从此是一个写着口径的声明，不是"这里刚好没有那行代码"。
#: 想给某台部署加回一个兜底轮数，六档那六格一起填（见 `TIERS`）。
#:
#: 不设模块级常量 `MAX_TURNS = None` —— 那条 census 判据
#: （`X = None` = 空槽等着被写）抓过真缺陷，而 `MAX_TURNS` 从来不写、也不该写
#:要么误报、要么往登记表里塞一条不是状态的。直接写进下面每一格。
#: （值仍是 `None`：它要进 `EVT_TURN_START` 那个事件，而 `None` 序列化成 `null`，
#: 换自造哨兵就得在这里再分一次叉。）
#:
#: `tool_result_max_chars = 50000`。
#:
#: 判据是6000 太小，取 50k。
#:
#: 而它会放大一条活锁：单条小，同样的数据要发更多次调用，
#: 更快撞上"工具结果条数"那条触发线（148 次调用、其中 99 次是同一件读表）。
#:这条和 tool_result_prune 是一对，别只看一头。
#:
#: 仍然缺一层：「单条消息内 tool_result 总量」那道闸本框架没有。
#: 抬了单条不补总量，一轮 4 个调用最多能带 200k 字符进上下文。
TOOL_RESULT_MAX_CHARS = 50_000
#: `command_timeout_ms = 30000`（`core/shell.py::DEFAULT_TIMEOUT_MS`
#: 是同一个数的另一个名字，两处一起改）。
#: 一句话：中位 39ms 的活儿不该有一个 120 秒的默认值。
#: 六档仍然全部相同（"六档该相同"那条判据照旧成立）——
#: 它现在比的是"每一档都等于本文件的这个常量"，不是某个写死的数。
COMMAND_TIMEOUT_MS = 30_000

#: 第 6 档那个"∞"的落法 = 35 的硬顶。
#:
#: 仍然是一个整数，只是从 `1_000_000`（惰性声明）改成真能生效的 35。
#: 35 同时也是 `core/workflow.py::MAX_PARALLEL` —— 一处数字、两个读者
#: （直接 `run_subagent` 与工作流里的并行）。
#: "无上限"是模型看到的资源口径，不是框架的安全口径：35 就是上限。
#: 每一档 +3（原 3/3/6/8/10/32 → 6/6/9/11/13/35）。
ULTRA_SLOT_CAP = 35


#: 整张档表 —— 唯一一处定义。别在任何别处再写一份「档 3 是什么」
#: （两处定义必然漂移；靠纪律去同步的覆盖语义不采用 —— 纪律没有读者）。
TIERS: dict[int, TierSpec] = {
    1: TierSpec(
        level=1, name="low", implemented=True,
        # 只有两格随档变：
        #   ① `subagent_slots`（子 agent 的个数）
        #   ② `thinking`（`effort` 本身就是它，这才是"档"的本体）
        # 其余每一档都相同，理由见模块 docstring 的"只有两格随档"。
        subagent_slots=6,
        subagent_turns=SUBAGENT_TURNS,               # 不随档（"个数"才是那根）
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        # 六档全开，`effort` = 档名。
        thinking=ThinkingSpec(enabled=True, effort="low"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
    2: TierSpec(
        level=2, name="medium", implemented=True,
        subagent_slots=6,
        subagent_turns=SUBAGENT_TURNS,
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        # 六档全开，`effort` = 档名。
        thinking=ThinkingSpec(enabled=True, effort="medium"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
    3: TierSpec(
        # `thinking` 那一格不等于生产的硬编码默认值 ——
        #   生产里 provider 层是硬编码关，而本表六档全开（判据见
        #   `ThinkingSpec`）。这是有意的偏离；
        #   `tests/test_tiers.py::test_default_tier_equals_today` 已按新契约重写
        #   （那里记着证据）。其余每一格仍等于生产里那个常量。
        level=3, name="high", implemented=True,
        subagent_slots=9,
        subagent_turns=SUBAGENT_TURNS,
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        # 六档全开，`effort` = 档名。
        thinking=ThinkingSpec(enabled=True, effort="high"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
    4: TierSpec(
        level=4, name="xhigh", implemented=True,
        subagent_slots=11,
        subagent_turns=SUBAGENT_TURNS,
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        # 六档全开，`effort` = 档名。
        thinking=ThinkingSpec(enabled=True, effort="xhigh"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
    5: TierSpec(
        level=5, name="max", implemented=True,
        subagent_slots=13,
        subagent_turns=SUBAGENT_TURNS,
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        # 六档全开，`effort` = 档名。
        thinking=ThinkingSpec(enabled=True, effort="max"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
    6: TierSpec(
        # ultra = xhigh 的全套参数 + 工作流编排。
        #强度那格是 `"xhigh"`（不靠档名卖"更强"）；
        #   与 xhigh 的唯一差别是 `subagent_slots=35` 与**多一件
        #   `run_workflow` 工具**（后者不是档表字段，见 `bus/registry.py::_SESSION_TOOLS`）。
        level=6, name="ultra", implemented=True,
        subagent_slots=ULTRA_SLOT_CAP,               # 35
        subagent_turns=SUBAGENT_TURNS,
        tool_concurrency=TOOL_CONCURRENCY,
        tool_result_max_chars=TOOL_RESULT_MAX_CHARS,
        max_turns=None,
        command_timeout_ms=COMMAND_TIMEOUT_MS,
        thinking=ThinkingSpec(enabled=True, effort="xhigh"),
        approval_ceiling=APPROVAL_RELAX,
        tool_profile=PROFILE_FULL,
    ),
}


#: 档名的顺序表（档 1 → 档 6）。从表里推出来，不是另抄一份 ——
#: 两处定义必然漂移，而这里连一条测试都不需要。
#: 名字与常见的 effort 档对齐（low → ultra）；`ultra` 已实现
#: （= xhigh 参数 + 工作流编排），不是"列得出、设不了"。
LEVEL_NAMES: tuple[str, ...] = tuple(TIERS[i].name for i in sorted(TIERS))


# 判定：这个档能不能用

def _require_level_number(value: object) -> int:
    """唯一一处「档号必须是一个真整数」的判定。

    `bool` 必须先挡掉：`isinstance(True, int)` 在 Python 里是 True
不挡的话 `effort_level: true` 会被当成档 1，而且一声不吭。

    `3.0` 也必须挡掉：`TIERS.get(3.0)` 能取到档 3
    （dict 的键比较用 `==`，而 `hash(3.0) == hash(3)`），光靠"键在不在表里"
    查不出浮点。这类值的来源只有一个（手改 settings.json），严格拒比
    "悄悄接受"好。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TierUnsupported(
            "投入档必须是一个**整数**档号，收到 %r（%s）"
            % (value, type(value).__name__)
        )
    return value


def spec_for(level: int) -> TierSpec:
    """取某一档的取值表 —— 不是整数 / 不合法 / 未实现，抛。

    两条不同的拒绝要说两句话：
    「没有这一档」是配置写错了；「这一档还没实现」是功能还没做。
    """
    spec = TIERS.get(_require_level_number(level))
    if spec is None:
        raise TierUnsupported(
            "没有这一档：%r —— 可选 1–%d" % (level, max(TIERS))
        )
    if not spec.implemented:
        raise TierUnsupported(
            "档 %d（%s）**尚未实现** —— 这次先换成 1–%d 里的某一档；"
            "它不会被静默降级成上一档。" % (spec.level, spec.name, _max_implemented())
        )
    return spec


def _max_implemented() -> int:
    return max(lvl for lvl, s in TIERS.items() if s.implemented)


def level_of(name: object) -> int:
    """把 `/effort high` 里的那个词（或一个数字）变成档号 —— 不合法就拒。

    它不吞错：`/effort hgih` 必须被拒（而不是静默落回默认档）——
    用户打错字却"看起来成功了"是最坏的一种成功。
    """
    if isinstance(name, str):
        key = name.strip().lower()
        if key.isdigit():
            return validate_level(int(key))
        if key in LEVEL_NAMES:
            return validate_level(LEVEL_NAMES.index(key) + 1)
        raise TierUnsupported(
            "没有这个档名：%r —— 可选 %s（或 1–6）"
            % (name, " / ".join(LEVEL_NAMES))
        )
    return validate_level(name)


def validate_level(value: object) -> int:
    """把用户给的值变成一个可用的档号，或拒（写入路径的唯一入口）。"""
    level = _require_level_number(value)
    spec_for(level)          # 越界 / 未实现都在这里拒
    return level


# 读：配置 →（冻结）→ 本会话的档

def configured_level() -> int:
    """读设置里此刻写着的档号（不冻结，每次都现读）。

    读不到，默认；读到了但做不到，拒（模块 docstring 那张表）。
    """
    try:
        if not _SETTINGS_PATH.exists():
            return DEFAULT_LEVEL
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or SETTING_KEY not in raw:
            return DEFAULT_LEVEL
        value = raw[SETTING_KEY]
    except Exception:
        # 文件坏了 / 不是 JSON —— 这属于「读不到」，默认，不许让一轮对话失败
        # （同 `_stream_enabled` 的规矩）。
        return DEFAULT_LEVEL

    return validate_level(value)     # 读到了，说出来就必须做到


_frozen: dict[str, int] = {}
_freeze_lock = threading.Lock()

#: 本会话把思考关掉的那个集合。
#:
#: 为什么需要它 —— 档表六档全开，但用户有时就是想要"这一段别想那么久"
#: （快、便宜、或者模型被思考带偏了）。一条会话级的覆盖，不落盘
#: （设置页管默认、命令管这一次）。
#:
#: 它是"关"的集合，不是"开/关"的字典 —— 默认（不在集合里）= 按档表走
#: （开）。这样"忘了清"的方向是回到档表的约定，不是把用户钉在一个过期的选择上。
#: 与 `_frozen` 共用 `_freeze_lock`：两条都是"这个会话的档"，分两把锁迟早会有人
#: 只拿一把去读"档 + 覆盖"这一对（那就读出一个中间态）。
_thinking_off: set[str] = set()


def current_level(session_id: str = "") -> int:
    """本会话的档 —— 第一次问的时候读一次，之后钉死。

    「冻结」落实的是三条硬约束里的前两条（会话内稳定 + 改档 = 一次冷启动）：
    用户中途改了设置，这一段对话不受影响，下一段（`/clear` 之后）才是新档。

    不要改回"每请求现读"（`configured_level` 就在旁边，那样写很方便）——
    45x 缓存尖峰的成因就是这件事。
    """
    key = session_id or ""
    with _freeze_lock:
        level = _frozen.get(key)
        if level is None:
            level = configured_level()
            _frozen[key] = level
            # 出声（一次会话一行）：改档是冷启动这件事得看得见。
            print("[tier] 投入档 %d（%s）—— 本会话冻结" % (level, TIERS[level].name))
        return level


def current_spec(session_id: str = "") -> TierSpec:
    """本会话的取值表（`current_level` + `spec_for` + 思考覆盖）。

    覆盖生效时返回的是一份新的 `TierSpec`
    （`dataclasses.replace`，`TierSpec` 是 frozen 的），其余每一格逐字不变 ——
    档位只有 `thinking.enabled` 这一格被翻。
    没有覆盖时不 replace，返回值与 `TIERS[n]` 是同一个对象
    （有断言按 `is` 判它，例如 `test_thinking` 那条端到端的 `spec is ...`）。
    """
    spec = spec_for(current_level(session_id))
    if (session_id or "") in _thinking_off and spec.thinking.enabled:
        import dataclasses
        return dataclasses.replace(
            spec, thinking=dataclasses.replace(spec.thinking, enabled=False))
    return spec


def is_thinking_overridden_off(session_id: str = "") -> bool:
    """本会话的思考是不是被 `/effort thinking off` 关掉了（界面/回执要说得准）。"""
    with _freeze_lock:
        return (session_id or "") in _thinking_off


def set_thinking_override(session_id: str, on: bool | None) -> None:
    """`/effort thinking on|off` —— 只改这一段对话。

    · `on=True`，取消覆盖（回档表的约定：六档全开）；
    · `on=False`，记进 `_thinking_off`；
    · `on=None`，不动（只查询）。
    不落盘、不出声（出声是命令那一层的责任：它要把"这是冷启动"说清）。
    """
    key = session_id or ""
    with _freeze_lock:
        if on is None:
            return
        if on:
            _thinking_off.discard(key)
        else:
            _thinking_off.add(key)


def frozen_level(session_id: str = "") -> int | None:
    """本会话已经冻结的档号；还没冻结就返回 `None`。

    它不冻结、不出声、不动任何状态 —— 存在的理由只有一条：
    界面要显示"本会话此刻生效的档"，而那不能用 :func:`current_level`
    （那个函数第一次被问就把档钉死，还打一行日志）。
    `/api/status` 是每 5 秒轮询一次的接口 —— 一个读接口不该有副作用。

「配置档」用 :func:`configured_level`（现读、不冻结），
      「本会话生效档」用这个。两个不一样 = 改过设置、这一段还没生效，
      界面必须把这句话说全（同一组标签不许打两个量纲）。
    """
    with _freeze_lock:
        return _frozen.get(session_id or "")


def set_level(session_id: str, level: int) -> None:
    """把本会话钉到某一档 —— `modes.set_mode` 的孪生兄弟。

    ## 它为什么必须存在

    `/effort` 只调 `reset_tier_cache` 的话，那是"把冻结值丢掉"；
    而新的档若没有别处记下来，下一次 `current_level` 会又读回设置里的旧值
`/effort 4` 变成一条什么都不做的命令。

    这与 `modes.set_mode` 逐条同款：写 `_frozen[sid]`（只动这一个会话）、
    出声（改档是冷启动这件事要看得见）。
    不写设置 —— 持久默认只由设置页改。
    `level` 必须已经过 `validate_level` / `level_of`（这里不重判一次：
    两处判定必漂）。
    """
    key = session_id or ""
    with _freeze_lock:
        _frozen[key] = int(level)
    print("[tier] 投入档 → %d（%s）—— 本会话，**不落盘**"
          % (int(level), TIERS[int(level)].name))


def reset_tier_cache(session_id: str | None = None) -> None:
    """丢掉冻结值 —— 由 `/clear` 调用（与 `reset_git_status_cache` 并排）。

    `session_id=None` = 全清。`/clear` 复用同一个 `session_key`，
    不清的后果是新会话还按旧档跑，而用户以为改档已经生效了。
    """
    with _freeze_lock:
        if session_id is None:
            _frozen.clear()
            _thinking_off.clear()
        else:
            _frozen.pop(session_id or "", None)
            # 思考覆盖也是"这一段对话的档"，`/clear` 一并清
            #   （`/effort` 改的是这一次，直到 `/clear` 恢复默认）。
            _thinking_off.discard(session_id or "")


# 消费侧的判据（步 2 起用这几个，别直接读裸字段）

def offers_subagents(spec: TierSpec) -> bool:
    """这一档给不给子 agent。

    「不给」的表示只有这一处（`subagent_slots == 0`）—— 谁想判断这件事就调它，
    别在别处再写一遍 `slots > 0`，也别从 `subagent_turns` 去推（那是第二套真相源）。
    """
    return spec.subagent_slots > 0


def offers_command(spec: TierSpec) -> bool:
    """这一档给不给 `run_command`。同上：「不给」= `command_timeout_ms == 0`。"""
    return spec.command_timeout_ms > 0


def effective_approval(user_ceiling: int, spec: TierSpec) -> int:
    """审批能放宽到哪一档 —— 只能收窄，不能放宽。

    有效值 = `min(用户自己的选择, 这一档的上限)`。用户把「关闭安全询问」打开、
    而档位只给到 :data:`APPROVAL_REMEMBER`，开关不生效（档压得住用户），
    反过来则用户说了算。调档永远不能替用户把闸门开大。

    六档全是 `APPROVAL_RELAX`（"只有两格随档"这条定稿），
它今天是一根常量旋钮，`min()` 永远返回用户自己那个值。
    但它仍然要留着并被真读（`core/shell.py` 是唯一调用点）：旋钮"今天不改变行为"
    与"没有消费者"是两件事 —— 后者是"一句永不兑现的声明"，而它没有任何东西会红。
    读者：`tests/test_tiers.py::test_the_tier_ceiling_narrows_the_relax_switch`。
    """
    return min(user_ceiling, spec.approval_ceiling)


# 表自身的自检（给测试用的唯二入口 —— 别在测试里照抄字段名）

def declared_knobs(cls: type = TierSpec) -> tuple[str, ...]:
    """`cls` 上实际存在的字段里，属于「旋钮」的那些（按声明序）。

    `cls` 参数是为了负对照：测试要能喂一个"故意写坏"的类进来，
    证明这个扫描器真的会红（否则「名单没漂移」可能只是"扫描器没扫"）。
    """
    return tuple(f.name for f in fields(cls) if f.name not in METADATA_FIELDS)


def knobs_with_defaults(cls: type = TierSpec) -> tuple[str, ...]:
    """有默认值的字段（应当永远是空的 —— 有默认值 = 有一条静默遗忘的路）。"""
    return tuple(
        f.name for f in fields(cls)
        if f.default is not MISSING or f.default_factory is not MISSING
    )
