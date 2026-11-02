"""子 agent —— 框架持有一个跨越 `tool_call` 的执行体。

## 它是什么 / 不是什么

是：把一件"上下文很脏、结论很短"的活派出去（读一堆文件给个结论），
父只收回那段结论。
不是：安全边界 —— 外部实现里也一样（有的只读只靠提示词，
有的通配前缀能绕过）。工具面收窄不等于权限。

## fork 的形状 —— 不剪父最新那一轮

```
[…父的历史…, assistant(那条 run_subagent 调用), tool(占位结果 ×N)…, user(directive)]
```

- 不剪：保住父那条 assistant（它带着全部 `tool_calls`），给它每一个
  `tool_call` 补一条逐字相同的占位结果。逐字相同是必要的，意外的变更会导致
  额外缓存重算。仅最后那段 directive 每个子不同，有且仅有此处允许变更。
- 另一种做法是剪到最后一个 `turn/end` 之前，丢掉父最新那一轮
  （而那一轮里恰好写着"我刚决定拆哪几件事"）。`core/message_history.py::balanced_prefix_end()`
  算的是后一种，我们拿它当闸门（见下），不拿它当剪点。

为什么我们能用上缓存：父那条请求恰好是
`[…history, assistant(tool_calls=[run_subagent…])]`，而子的请求就是它 + 一段
子与父同前缀，命中。

不能用 `user(placeholder_results…)` 那种写法：此处仅作 OpenAI 式兼容处理，
tool 的结果应当是 `role: "tool"` + `tool_call_id`；`user(...)` 式的回复会致使子
首请求不平衡，具体性状会表现为悬空 `tool_call`，上游 400 报错。

## 三个承重的判据（都有测试）

1. 前置闸门：`balanced_prefix_end(快照.payload) == len(快照.payload)` ——
   父的历史必须已是平衡的，否则子拿到的第一条请求就是坏的。
2. 沿用父的 `session_id`：工具门禁按 session 查暴露集；换 id，子的每一步都被拒。
   顺带：子发现的工具回流给父 ✓，用户中断也同时停住子 ✓。
3. 工具表照抄父的（含本工具自己），调用时才拒嵌套 —— 判据：**工具定义
   应当做到与父逐字节相同，从工具表摘除它等同于在 tool 段即截断缓存。**
"""

from __future__ import annotations

import re

import contextlib
import contextvars
import copy
from typing import Any, Callable, Optional

from core import request_snapshot, tiers
from core.message_history import balanced_prefix_end

#: 占位结果的逐字文案 —— 每个子 agent 必须一模一样（差别只许在末段 directive）。
#: 作为子请求前缀的一部分，若有变更则报废整条缓存。
FORK_PLACEHOLDER = "已派遣子代理，结果稍后返回"

#: 子 agent 空产出时的兜底 —— 判据：不兜底的话父会以为没活可干、直接把这一轮收掉。
#: 空产出必须说话，不能回一个空串。
EMPTY_OUTPUT = "（子 agent 跑完了，但没有返回任何内容。）"

#: 子 agent 的轮次预算在这里没有常量 —— 它由档位给
#: （`TierSpec.subagent_turns`；档 3 是 8）。
#: 理由同 `_MAX_TOOL_CONCURRENCY`：某一档的值只能在一处定义。
#:`run_child` 必须收到 `max_turns=`（没有默认值：有默认值就等于留了一条
#: "忘了传、于是静默用旧值"的路）。

#: 前台子 agent 的 wall-clock 预算（批 5 收尾）。
#:
#: 超过它，自动转后台（不取消）—— 父这一轮不再被钉在这一次
#: `tool_call` 上，结果回来时走"后台任务回报"那条路。
#:
#: 判据不是"多快算正常"，是别让一次卡住的子 agent 冻住整轮：
#: 用户随时可以 Ctrl+B 更早转走，这一条只是兜底。
#: 与 `JOB_MAX_LIFETIME_MS`（30 分钟，到点杀）是两件事：
#: 这里只换等待的方式，不改变它还能跑多久。
SUBAGENT_FOREGROUND_BUDGET_MS = 180_000

#: 嵌套深度。v0 只允许一层（`can_spawn()` 在子里面返回 False）。
#:
#: 用 `ContextVar`，不是进程级 `int`。
#: 它表达的那句话是「我此刻在不在子 agent 里面」—— 那是当前执行上下文
#: 的属性，不是进程的属性（`request_snapshot` 也是 `ContextVar`；
#: 同一个问题该用同一个答案）。用一个进程级 int 装它，有三处坏法：
#:
#: ① 跨会话串味 —— 调度器允许不同会话并行（每会话一把锁 + 进程级上限 3）。
#:    会话 A 在跑子 agent 时 `_depth ≥ 1`，
#:    而此时会话 B 的 `_ice_on()`（`core/agent_loop.py`）就返回 `False`
#:B 这一轮的 ICE 信息层静默失效 = 明文交给模型（正撞 ICE 那条
#:    「不许把明文交给模型」）；同一次还会让 B 的审批类工具被误拒。
#: ② 非原子 RMW —— `+=` / `-=` 是 LOAD→ADD/SUB→STORE 三步。两个方向都致命：
#:    残留 +1，父会话此后永远被当成"在子里面"（ICE 对父永久失效 ·
#:    用户再也批不了任何东西 · 再也派不出子 agent）；
#:    漂到 −1，子里反而伪名化（把父那份的句柄当"入境句柄"剥掉 ——
#:    会破坏上下文）。
#: ③ `can_spawn()` 判的是 `== 0`，漂到 0 就放行嵌套。
#:
#: 换成 `ContextVar` 之后三条一起消失：每个 `asyncio` 任务 / 线程 / 显式
#: `Context` 各拿一份，天然自洁，也不需要锁（同一个上下文里没有并发）。
_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "crannia_subagent_depth", default=0)


def depth() -> int:
    return _depth.get()


def can_spawn() -> bool:
    """现在能不能派子 agent —— 在子的循环里会返回 False（递归护栏）。

    判据是"调用时拒"，不是"从工具表里摘"（见模块 docstring 第 3 条）。
    """
    return _depth.get() == 0


class _DepthGuard:
    """把 `_depth` 加一，无论成功失败都还原（轮中途抛异常也必须清）。

    `set()` 返回一个 `Token`，`reset(token)` 是精确还原 ——
    比"进去前存一份、出来时减一"强：中间无论嵌套多少层，出来都回到进去那一刻
    的值（减一那版在嵌套/跨上下文下才对不上）。
    """

    __slots__ = ("_tok",)

    def __enter__(self):
        self._tok = _depth.set(_depth.get() + 1)
        return self

    def __exit__(self, *exc):
        _depth.reset(self._tok)
        return False


# 只读档
#
# 只读子 agent 在执行层拒，`tools=` 逐字不动。
#
# 为什么不是"把写工具从 `tools=` 里摘掉"——那条路根本走不通，两个独立理由：
#   ① 摘 `tools=` 会在 tools 段就断掉与父的缓存前缀（子的 `tools`
#      取自快照，与父逐字节相同才有缓存）；
#   ② 更根本：单独摘它不构成只读 —— 执行闸 `bus.registry.is_tool_active`
#      查的是父的暴露集，而子沿用父的 `session_id`（fork 的硬要求）
#模型照样可以对一个"没发给它"的工具发起调用。
#
# 这是能力约束，不是安全边界。外部实现里也一样：
#   有的只读只靠提示词，有的只读子手里仍有完整执行工具。
#   它拦的是"顺手写了一下"，不是"决意要写"。
#   真正的强制力仍然只有 `SecurityGuard` 的路径分级。

#: 只读档开着的时候有几层（计数不是 bool —— 同 `_depth` 的形状，
#: 嵌套目前虽然被 `can_spawn()` 拒掉，但计数器不依赖那条前提）。
#: 同 `_depth`：用 `ContextVar`，不是进程级 `int`。
#: 它有一条 `_depth` 没有的坏法：**一个 `readonly=true` 的子与一个
#: `readonly=false` 的子，生命周期只要重叠**，后者就会读到 `readonly()` 为真
#:它的写工具全被拒（不需要丢更新，重叠就够了）。
_readonly: contextvars.ContextVar[int] = contextvars.ContextVar(
    "crannia_subagent_readonly", default=0)


def readonly() -> bool:
    """现在跑的这一层是不是只读的。"""
    return _readonly.get() > 0


class _ReadonlyGuard:
    """把只读档打开，无论成功失败都还原（同 `_DepthGuard` 的判据）。"""

    __slots__ = ("_tok",)

    def __enter__(self):
        self._tok = _readonly.set(_readonly.get() + 1)
        return self

    def __exit__(self, *exc):
        _readonly.reset(self._tok)
        return False


#: 权限档判成 `read`、但其实会开一份会话态的工具 —— 只读档要单独拒掉它们。
#:
#: 判据不建大名单：主判据是现成的权限档（`get_tool_permission(name) != "read"`
#:拒），与判并发用的是同一条（"判据不建新名单"）。那张前缀表已经
#: 覆盖了 `write_*` / `add_*` / `create_*` / `delete_*` / `deliver_*` / `todo_*` …
#: 但 `create_document` 因为"它开的是内存里的一份文档会话、不是文件 I/O"
#: 被故意放进了 `_READ_PREFIXES`（`bus/registry.py`）—— 而子**沿用父的
#: `session_id`**，它在只读子里开的那份会话会落进父那一格。
#: `set_slide_design` / `mock_image` 同理（都是原地改内存会话），且 `mock_image`
#: 还兼着 `_WRITE_PREFIXES` 那一半 —— 两边都列着，正是因为两边都不准。
_READ_TIER_BUT_STATEFUL: frozenset[str] = frozenset({
    "create_document", "set_slide_design", "mock_image",
})


def is_write_like(tool_name: str) -> bool:
    """这个工具会不会改东西？（只读档据此拒）

    fail-closed：权限档查不到就回 `admin`（`get_tool_permission` 的默认）
一个谁都没分类过的新工具在只读子里默认被拒，而不是默认放行。
    """
    from bus.registry import get_tool_permission

    if tool_name in _READ_TIER_BUT_STATEFUL:
        return True
    return get_tool_permission(tool_name) != "read"


def readonly_denied_message(tool_name: str) -> str:
    """只读子里碰到写工具时的回执 —— 拒绝，并给出路线（形状同
    `agent_loop._approval_in_child_message`：永不挂起）。

    为什么是"报回父"而不是"问用户"：子没有可以提问的那一轮（审批靠"回答变成
    新的一轮"兑现，而子的循环跑在父的一次 `tool_call` 里面）。
    """
    return (
        f"Error: this sub-agent was started in **read-only** mode, and '{tool_name}' "
        f"changes something — so nothing was executed. Report back to the parent agent "
        f"what you wanted to change and what it should become (name the exact file and "
        f"edit); the parent can make the change and will get the normal write access."
    )


def build_fork_prefix(snapshot, assistant_msg: dict,
                      extra_placeholder: bool = True) -> Optional[list]:
    """拼子 agent 的前缀（不含 directive —— 那一条由 `user_message=` 补）。

    *assistant_msg* 是父那条带着 `run_subagent` 调用的 assistant 消息。
    返回 `None` 表示前置闸门没过（父的历史不平衡）—— 调用方必须把它变成一句
    说人话的拒绝，不许拿一段坏历史去 fork。

    返回的是新列表（`snapshot.payload` 那份一个字都不动 —— 它是父的、还要用）。
    """
    prefix = list(snapshot.payload)
    # ① 闸门：父的历史必须已经平衡（那个剪点算的就是这件事，我们只借它的判据）
    if balanced_prefix_end(prefix) != len(prefix):
        return None

    # ② 克隆父那条 assistant（不原地改 —— 它还在父的历史里）
    clone = copy.deepcopy(assistant_msg)
    prefix.append(clone)

    # ③ 给它每一个 tool_call 补一条逐字相同的占位结果
    #    形状应当是 `role: tool` + `tool_call_id`，而不应是 `user(...)` 式写法
    #    （详见 docstring 内实现）
    if extra_placeholder:
        for tc in (clone.get("tool_calls") or []):
            prefix.append({
                "role": "tool",
                "tool_call_id": tc.get("id") or "unknown",
                "content": FORK_PLACEHOLDER,
            })
    return prefix


def build_directive(task: str) -> str:
    """末段 directive —— 唯一允许每个子不同的那一块。

    四段式 —— 子 agent 只回一段话，所以那一段的质量全靠这里说清楚。
    四段的理由：

    ① 任务 —— 原文照抄，不加工（加工就是替它做判断）。
    ② 别重复父做过的 —— fork 给了它父的全部上下文；不说这句，它会从头再来一遍。
    ③ 只回一段话 + 要自足 —— 这是唯一的回传通道，而"如上所述"式结论
       回到父那里就是废的（父看不到"上"指的是什么）。位置要给 `文件:行号`。
    ④ 找不到就明说 —— 不说这句，它会编一个看起来像结论的东西回来
       （同族判据：部分产出不是成功）。
    """
    return (
        f"【任务】{task}\n\n"
        f"【背景】上面是你上级 agent 的上下文快照 —— 它刚决定把这件事交给你。"
        f"**不要重复它已经做过的**；直接做这件事。\n\n"
        f"【只回一段话】它**只会收到你最后那段文本** —— 你的过程、工具输出、推理它"
        f"都看不到。⇒ 结论要**自足**：谁、在哪、是什么，都写清楚；"
        f"提到位置时给**文件:行号**（或精确路径），别写「如上所述」「那个文件」这种依赖前文的说法。\n\n"
        f"【找不到就明说】没找到、做不到、或只找到一部分，都**直接说**；"
        f"**别编一个看起来像结论的东西**。"
    )


#: 子 agent 报告里，冒充 harness 自己的那些标签。
#:
#: 与 `core/content_sanitizer.RESERVED_TAGS` 不是一张表，别合并：
#: 那张表是我们自己的标签（`<user_document>` / `<system_instructions>` …），
#: 防的是"文档冒充框架的包裹"；这张防的是"报告冒充框架本身在对模型说话"。
#: 已知要防的就是 `<system-reminder>` —— 而它不在 `RESERVED_TAGS` 里，
#: 所以只跑 `sanitize_text` 挡不住它。
_FORGED_TAG_RE = re.compile(
    r"<\s*/?\s*(?:system-reminder|system|assistant|human|tool_result)\b[^>]*>",
    re.IGNORECASE,
)

#: 行首的角色前缀 —— 冒充"这是另一条消息"（做法：加一个反斜杠）。
_ROLE_LINE_RE = re.compile(
    r"^(\s*)(Human|Assistant|System|User|Tool)(\s*:)", re.IGNORECASE | re.MULTILINE,
)


def _sanitize_report(text: str) -> str:
    """子 agent 报告回父之前先"去毒"。

    ## 为什么单独一步（内容隔离已经包了一层，为什么还要这个）

    内容隔离覆盖的是工具结果那条路：`_ISOLATED_DOC_TOOLS` → `sanitize_text`
    → `wrap_as_user_document`。而 `run_subagent` 的回执虽然也在那套里，
    它挡的是「外部内容冒充文档」，挡不住「报告冒充框架在对模型说话」——
    因为 `RESERVED_TAGS` 里没有 `<system-reminder>` 这一类。

    ## 两条判据

    ① 确定性的那一半永远跑，概率性的那一半只负责出声。
       转义伪造标签 + 给角色前缀加反斜杠 —— 这两件不依赖检测，
       所以不存在"检测漏了就放过去"。内容隔离那条「检测可以错、打码不能错」
       在这里的落法就是：打码这一步压根不做判定。

    ② 非破坏性：转义 + 标记，绝不删改结论正文。
       子 agent 的结论是它唯一的产出（`run_child` 只回一段话），
       改它等于改事实。所以用的是 `\\` 前缀与 `&lt;` 转义，
       模型仍然读得到原字面 —— 只是再也不构成一个标签。

    标记行加在报告头部，且说清"未改动" ——
    措辞同 `content_sanitizer.advisory_line`：不许写"已中和"（那是假声明）。
    """
    if not text:
        return text

    marks: list = []

    # ① 伪造的 harness 标签 —— 转义 `<`，不删
    text, n = _FORGED_TAG_RE.subn(
        lambda m: m.group(0).replace("<", "&lt;"), text)
    if n:
        marks.append("forged-harness-tag")

    # ② 行首角色前缀 —— 加一个反斜杠
    # \1 = 缩进 · \\ = 一个字面反斜杠 · \2 / \3 = 第 2、3 组
    # -> 结果形如 `\Assistant:` —— 原字面还在，只是不再是行首
    text, n = _ROLE_LINE_RE.subn(r"\1\\\2\3", text)
    if n:
        marks.append("impersonated-role-prefix")

    # ③ 概率性的那一半：只出声
    from core.content_sanitizer import advisory_line, find_injection_indicators
    patterns = find_injection_indicators(text)
    if patterns or marks:
        line = advisory_line(list(patterns) + marks, source="subagent")
        text = (line + chr(10) + text) if line else text
    return text


def run_child(*, snapshot, assistant_msg: dict, task: str, provider,
              max_turns: int, readonly_mode: bool = False,
              emit: Callable | None = None,
              directive: str | None = None,
              cancel_check: Callable[[], bool] | None = None,
              workspace: str | None = None,
              tool_allowlist: frozenset | None = None) -> dict:
    """跑一个子 agent，只把最终文本交回父。

    返回 `{"success": bool, "output": str, ...}` —— 形状与别的工具一致（会被
    `json.dumps` 之后作为 `role: tool` 回填）。异常一律算失败，
    绝不把部分产出报成成功（未知的终止原因要判成失败）。

    *readonly_mode*，这一层里所有写类工具在派发处被拒（见上面那段）。
    参数名带 `_mode` 后缀是故意的：模块里已经有一个 `readonly()` 谓词，
    同名参数会把它遮住（而遮住的症状是"只读档看起来永远开着"）。

    *tool_allowlist*，只允许调用这些工具，其余在执行侧被拒。
    不改变发给模型的工具表，父子同前缀不受影响。

    *workspace*，这一趟的落点（隔离工作树的绝对路径）。给了就临时钉住它，
    子里的相对落点、`primary_output_dir()` 与 `classify_path` 的「这一段选定的目录」
    同时指向那里；`None` = 跟父同目录（默认）。

    *directive*，末段那段提示词的替换品，`None` = 按 `task` 现造
    （`build_directive`，即今天唯一的行为）。
    群成员那条路也借这台机器：群成员要的是
    "你是谁 + 四段式"，而四段式一个字都不该在第二个地方再写一遍
    —— 所以群那边传进来的是 `build_member_directive(...)`，
    它自己由 `build_directive` 拼出来。
    默认 `None`，现有那条路一个字节不变。
    """
    from core.agent_loop import agent_loop

    prefix = build_fork_prefix(snapshot, assistant_msg)
    if prefix is None:
        return {
            "success": False,
            "error": ("父的历史不是平衡的（有悬空 tool_call 或孤儿 tool 结果）—— "
                      "不能拿一段坏历史去开子 agent。先让这一轮正常结束，再派它。"),
        }

    if not can_spawn():
        return {
            "success": False,
            "error": ("子 agent 里不能再派子 agent（v0 只允许一层）—— "
                      "这件事你自己做完，或者把它带回给用户。"),
        }

    # 只读档与深度同一段作用域（两个守卫都靠 `__exit__` 还原 ——
    # 轮中途抛异常也必须清）。下面那个 `except Exception` 在 with 外面，
    # 所以异常路径同样会先把两层还原掉。
    #
    # 第三个要还原的是请求快照。子走的是同一个
    # `agent_loop`，而那条路每轮都会 `set_current(capture(...))` ——
    # 覆盖父的快照、且从不还原（父只在每轮发请求前重设一次），
    # 同一批里的第二个 `run_subagent` 在 `_dispatch_subagent` 读到的，
    # 是第一个子跑完时留下的那一份（而两者的 `session_id` 恰好相同，
    # 那道串味绊线拦不住），拿别人的转录去 fork，静默。
    from core import session_workspace

    guard = _ReadonlyGuard() if readonly_mode else contextlib.nullcontext()
    ws_ctx = (session_workspace.use_path(workspace) if workspace
              else contextlib.nullcontext())
    text_directive = directive if directive is not None else build_directive(task)
    if workspace:
        text_directive += ("\n\n★ 你的工作目录是 `%s`（独立的工作树）："
                           "读写都用这个目录下的路径。" % workspace)
    with _DepthGuard(), guard, request_snapshot.preserved(), ws_ctx:
        try:
            content, _msgs, _chunks, tokens = agent_loop(
                # directive 走 `user_message=`：`agent_loop` 会把它 append 到 `messages`
                # 末尾 —— 那正好就是那条 fork 形状的最后一块，不用我们手动拼。
                user_message=text_directive,
                provider=provider,
                messages=prefix,          # ← 新列表；`snapshot.payload` 一个字不动
                session_chunks=[],
                memory_context="",        # 有 `_system` 时它不参与（见 agent_loop）
                max_turns=max_turns,      # ← 档位给的（调用方：`_dispatch_subagent`）
                # 三个都取自快照，子与父同前缀（缓存），且工具门禁认得出工具
                session_id=snapshot.session_id,
                _tools=snapshot.tools,
                _system=snapshot.system,
                # 第四个必须同源的 —— 思考模式进提示缓存键
                # （五项 cacheSafeParams 之一），子要是按自己的默认
                # 发，第一条请求的前缀就从最前面断了，fork 那台机器白搭。
                # 取自同一个会话的档（`snapshot.session_id`），不是另读一次设置。
                thinking=tiers.current_spec(snapshot.session_id).thinking,
                _emit=None,               # v0：子的轮次事件不进父的时间线（轮号会串）
                # 任务级取消令牌：取消一个后台子 agent
                # 只停它，不动父那一轮（会话级 `is_cancelled` 仍在）。
                cancel_check=cancel_check,
                # 子 agent 定义声明的工具白名单；None 表示不限制。
                _tool_allowlist=tool_allowlist,
            )
        except Exception as exc:  # noqa: BLE001 —— 判据：异常一律算失败
            return {"success": False,
                    "error": f"子 agent 失败：{type(exc).__name__}: {exc}"}

    text = (content or "").strip()
    if not text:
        text = EMPTY_OUTPUT
    # `WB5`：回父之前先"去毒"（确定性那半永远跑，见 `_sanitize_report`）
    text = _sanitize_report(text)
    return {"success": True, "output": text, "tokens": tokens,
            "note": "子 agent 的过程、工具输出与推理都没有回传，只有上面这段话"}


#: `run_subagent` 的 schema。常驻工具（`_CORE_TOOLS`）：
#: 模型不知道它存在，就没法搜它。
#: 它的每个字都按每条请求计费（改措辞必须重量一次）。
SUBAGENT_TOOL_SCHEMA: dict = {
    "description": (
        "派一个独立子任务给子 agent，只回一段结论。"
        "★ 只有这两种情况才用：①读一堆文件/网页、过程脏但结论短；②几件真能并行的独立小活。"
        "★ 别用它做：查一个文件、跑一条命令 —— 你自己一两步能做完的都别派，"
        "它要付一整个上下文的钱。任务里写清要它回什么（要 文件:行号、要清单还是结论）。"
        "你看不到它的过程，它也看不到你之后的动作；它不能再派子 agent。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要它做什么（写清楚：它只有你的上下文快照 + 这句话）",
            },
            "readonly": {
                "type": "boolean",
                "description": ("true = 只准看，不许改（写类工具在执行层被拒）。"
                                "探索/调研/找代码这类**都要带上 true**；"
                                "确实要它动手改文件时才省它。"),
            },
            "name": {
                "type": "string",
                "description": "这一步的短名（3–5 个词），显示在界面的子代理列表里。",
            },
            "background": {
                "type": "boolean",
                "description": ("true = 转到后台跑，你立刻拿回执、先做别的；"
                                "结果回来时会收到一条后台任务回报。"),
            },
            "isolate": {
                "type": "boolean",
                "description": ("true = 独立 git 工作树；改动记在新分支上，"
                                "回执给分支名（工作区须是 git 仓库）。"),
            },
        },
        "required": ["task"],
    },
}
