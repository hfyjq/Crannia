"""计划模式 —— 只读 + 计划文件 + 一次性人工批准。它不执行。

与已删除的 Plan-and-Execute 引擎不是一回事：那个执行，
所以有"执行质量"可言；这一个只产出计划文件 + 一次人工批准。别再混。

## 四条第一手判据

| # | 判据 | 我们的落点 |
|---|---|---|
| 1 | 不许做任何编辑，且这条优先于收到的其它任何指令 | 优先级写在指令里，但光写指令不够 —— 执行侧要有闸（下面 `is_readonly_tool`） |
| 2 | 这是你唯一被允许编辑的文件 | 白名单式：只放行本会话自己那一份计划文件 |
| 3 | 绝不用文字或提问面板去问计划批准 | 批准只走专用工具 |
| 4 | FULL / SPARSE 两档提醒 | 见下 |

## 三条不许回退的

### ① 模式声明走「拒绝那一刻」，绝不进 system prompt

把「命令在只读文件沙箱下运行」这句话每请求都带上，
模型瘫痪（首次人工会话 12 轮里有 5 轮以零工具调用结束）。
另两条独立理由：缓存（改 system prompt = 改写前缀）与成本（每轮重发是纯税）。

### ② 计划文件的名字逃出会话键（`slug`），`/clear` 清的是 slug 不是文件

`/clear` 复用会话键，若文件名是 `<键>.md`，新会话会读到上一段的旧计划。
铸一个随机 slug（会话级内存态）：`/clear` 把它清掉，下次进计划模式铸新名
旧文件留在盘上，由 `core/retention.py` 的保留期回收。
（这一条与「模式」不同：`/clear` 不清模式、要清 slug ——
"我要重新想一遍"不等于"我退出计划模式"，但"上一段的计划"确实不该跟过来。）

### ③ 无可用通道，立即拒绝，不是挂起、不是超时

"不挂起"是硬要求：挂起靠"回答变成新的一轮"，而没有通道就没有那一轮 ——
挂起等于永远挂着。还有更硬的一档（通道活跃时把两个 plan 工具直接摘掉），
本框架不做：六个通道全都能收文字回话，那一档挡不住任何东西。
"""

from __future__ import annotations

import re
import secrets
import threading
from pathlib import Path
from typing import Dict, Optional

from core.i18n import t as tr

#: 计划文件放哪（框架自己的制品目录）。
#: 它不在工作区里，（a）不进工作区索引、不污染 `search_files`；
#: （b）语义对（和 `data/memory/sessions/*.md` 同类：都是框架的产物）。
PLAN_DIR = Path("data/plans")

#: 计划文件一律走 `read_file` / `write_file`（`SecurityGuard` 那条路），
#: 不许用 `run_command` 去碰 —— `data/` 在 `command_policy.PROTECTED_PATHS` 里，
#: 碰它每次都因"提及 `data/`"升档要审批。

#: 文件名形状：`plan-<8位十六进制>.md`。
#: 不是会话键 —— 见模块 docstring ②。
_SLUG_RE = re.compile(r"^plan-[0-9a-f]{8}$")


def mint_slug() -> str:
    """铸一个新 slug。唯一铸点（别在别处再拼一个形状）。"""
    return "plan-%s" % secrets.token_hex(4)


_slugs: Dict[str, str] = {}
_lock = threading.Lock()

#: 进计划模式之前是什么模式 —— 退出时照它还原（`prePlanMode` 语义，
#: 不是"直接继续"）。
#: 它与会话键同寿，与 `_slugs` 一起登记、一起 `/clear`（同一个理由：复用键）。
_pre_modes: Dict[str, str] = {}

#: 进入计划模式之后过了几个用户轮。
#:
#: 它不在这里就没人有 —— `agent_loop` 里那个 `turn` 不是它：
#: 那是用户轮内部的循环序号，每个用户轮归零（`turn == 0` 只说明"这是本轮的
#: 第一次请求"）。这里要的是"离进入那次过了几轮"。
#: 与会话键同寿，与 `_slugs` / `_pre_modes` 一起登记、一起 `/clear`
#: （同一个理由：会话键被复用）。
#: 它是"过了几轮"而不是"第几轮" —— 见 `build_plan_reminder` 的「三个必须」。
_turn_index: Dict[str, int] = {}


#: 这一会话刚被压缩过、下一次请求要把 FULL 重发一遍。
#:
#: 它治的是什么：压缩会把这一段轮次挤出上下文（落在压缩边界之前），
#: 而计划模式是会话态（活在进程里），压缩之后模式照常生效（写工具照拦），
#: 而模型手里一条相关指令都没有 —— 它表现得像"这就是一段普通对话"。
#: 一个还在生效的约束，没有任何地方在说它 —— 这是最狠的形状。
#:
#: 「SPARSE 自足」救不了它：自足的 SPARSE 说得清"这一段只读 + 唯一可写的
#: 是哪个文件"，说不清 FULL 里那四条（不许编辑 / 唯一可写文件 /
#: 增量写 / 不许用文字问批准）—— 两条要补的东西判据不同
#: （一条补的是"漂远"，这条补的是"原件没了"）。
#:
#: 生产者在 `PostProcessStage`（压缩发生在那里，跨请求）·
#: 消费者在 `build_plan_reminder`（与 SPARSE 同一个挂载点）· 读完就清
#: （一次性 —— 留着它等于"每轮都发 FULL"，那是纯税 + 模型瘫痪）。
_refull_pending: set = set()


def note_compressed(session_id: str = "") -> None:
    """这一会话刚被压缩过，下一次请求要把 FULL 重发一遍。

    由 `PostProcessStage` 在"这一轮真的压了"那一刻调（判据是它的
    `compressed is not None and compressed is not ctx.messages`）。
    """
    with _lock:
        _refull_pending.add(session_id or "")


def take_refull_pending(session_id: str = "") -> bool:
    """取走那个标记（读到就清）。一次性，不许留着。

    读走即清是这一条的承重处：不清，从此每轮都发 FULL ——
    而"每轮都发"正是那份实际记录里的瘫痪形状（见 `build_plan_reminder` 的注）。
    """
    key = session_id or ""
    with _lock:
        if key in _refull_pending:
            _refull_pending.discard(key)
            return True
        return False


def slug_for(session_id: str = "") -> str:
    """本会话的 plan slug —— 第一次问的时候铸一个，之后钉住。

    `session_id` 为空串时也会铸（`request_snapshot` 拿不到会话号那条路）——
    那格是"没有会话"的兜底，不要把它当成一条真实会话（同
    `core/session_key.py::session_key_for` 那条退化契约）。
    """
    key = session_id or ""
    with _lock:
        slug = _slugs.get(key)
        if slug is None:
            slug = mint_slug()
            _slugs[key] = slug
        return slug


def forget(session_id: str) -> None:
    """丢掉本会话的 slug 与"进入前的模式"（`/clear` 走这条 —— 清 slug，不删文件）。

    四格一起清：`_slugs` / `_pre_modes` / `_turn_index` / `_refull_pending`
    是同一个会话键的四半，只清一部分会让下一段对话拿着上一段的 slug、
    上一段的目标模式、上一段的回合计数（接着从第 7 轮数起，该发的 SPARSE 晚发），
    或替上一段把那次 FULL 补发掉
    —— 「purge 是不变量，不是一串地方」。
    """
    key = session_id or ""
    with _lock:
        _slugs.pop(key, None)
        _pre_modes.pop(key, None)
        _turn_index.pop(key, None)
        _refull_pending.discard(key)


def reset_turn_index(session_id: str = "") -> None:
    """把回合计数归零 —— 进计划模式与出计划模式各一次。

    进的那一次：计数从"进入之后"开始（进入那一轮不推进，
    因为 FULL 已经由 `enter_plan_mode` 的工具回执发过了）。
    出的那一次：不清的话，下次再进来会接着上一段的数继续数。
    """
    with _lock:
        _turn_index.pop(session_id or "", None)


def note_plan_turn(session_id: str = "") -> int:
    """又过一个用户轮，返回新的计数。

    每用户轮只许调一次（判据：`build_plan_reminder` 的 `new_turn`）。
    """
    key = session_id or ""
    with _lock:
        n = _turn_index.get(key, 0) + 1
        _turn_index[key] = n
        return n


def remember_previous_mode(session_id: str, mode: str) -> None:
    """记下"进计划模式之前是什么"（`enter_plan_mode` 的写点）。"""
    with _lock:
        _pre_modes[session_id or ""] = str(mode or "")


def previous_mode(session_id: str) -> str:
    """进计划模式之前那一档；没记过就回默认档（不是 `plan`，那是死循环）。"""
    from core.modes import DEFAULT_MODE
    with _lock:
        return _pre_modes.get(session_id or "") or DEFAULT_MODE


def plan_path(session_id: str = "") -> Path:
    """本会话计划文件的路径（`data/plans/plan-xxxxxxxx.md`）。"""
    return PLAN_DIR / ("%s.md" % slug_for(session_id))


def is_plan_slug(name: str) -> bool:
    """这个名字是不是一个合法的 plan slug（归档/清理用得上）。"""
    return bool(_SLUG_RE.match((name or "").strip()))


def _norm(p) -> str:
    try:
        return Path(p).resolve().as_posix().lower()
    except Exception:
        return str(p).replace("\\", "/").lower()


def is_current_plan_file(file_path, session_id: str = "") -> bool:
    """这份文件是不是本会话自己那份计划文件。

    判据：不是"plans 目录"，是 `plans/<本会话的 slug>.md`。
    目录级的白名单会让 agent（或一次提示注入）往那儿写任意文件。

    判定它的是 `SecurityGuard._is_plan_artifact` —— 唯一解码处
    （`classify_path` 的 2.3 与 `validate_path` 的 Level 2 都走它）。
    别在别处再判一次：两处判据会漂，而两个答案看着都对。
    """
    try:
        sid = session_id or _current_sid()
        want = plan_path(sid)
        return _norm(file_path) == _norm(want)
    except Exception:
        return False


def _current_sid() -> str:
    """从请求快照取会话号（工具处理器拿会话号那条路，同 `core/todos.py`）。

    取不到，空串 —— 那格的 slug 是独立的，不会误放行别人的计划文件。
    """
    try:
        from core.request_snapshot import current_session_id
        return current_session_id()
    except Exception:
        return ""


# 只读闸（判据 1 的"执行侧那一半"）

def is_readonly_tool(tool_name: str) -> bool:
    """这个工具按权限档算不算只读（`read` 档）。

    不建第三张只读名单 —— 复用现成的权限档：
    `get_tool_permission(name) == "read"`。
    查不到，`admin`（fail-closed），拒，方向是对的。

    它不等于"计划模式放不放行"（`is_allowed_in_plan_mode` 才是）——
    闸那边多一张捞例外的表（`_PLAN_MODE_READ_ONLY_EXCEPTIONS`：只读、但档不是
    `read` 的那些）。别拿本函数当闸的判据：那样"会弹审批的跨区搜索被堵、
    不问就干的 `read_file` 开着"这个倒挂会回来。理由在那张表的注释里。
    """
    try:
        from bus.registry import get_tool_permission
        return get_tool_permission(tool_name) == "read"
    except Exception:
        return False        # 拿不到判定，拒（fail-closed）


#: 计划模式下本身就该能跑的两个工具（判据 3：批准只走专用工具）。
#: 没有它们，模型进得去出不来 —— 计划模式会变成进得去、出不来的陷阱。
_PLAN_MODE_TOOLS = frozenset({"enter_plan_mode", "exit_plan_mode"})

#: 能写计划文件的那几个。
#: 这张名单不是"只读名单"（判据 8 禁的是后者，那是权限档该答的问题）——
#: 它答的是另一个问题：哪一次调用可能落在计划文件上。
#: 只有带文件路径参数的写工具才有这个可能，只列它们。
_PLAN_FILE_WRITERS = frozenset({"write_file", "edit_file"})

#: 只读、但档位不是 `read` 的工具 —— 计划模式下放行。
#:
#: ## 它为什么不是"第三张只读名单"
#:
#: 禁的是把 `read` 档重抄一遍（那是权限档该答的问题，抄一遍就有两个真相源）。
#: 这一张答的是另一个问题：哪些工具一个字节都不改，只是档位为别的事定得更高。
#:方向相反：那张是"补抄"，这张是"捞例外"。
#:
#: ## 不加它会怎样（这才是它存在的理由）
#:
#: 闸判的是工具名，不是能力。不加这一条，
#: 「读到工作区外」在计划模式下会按走哪条路分叉：
#:
#: | 路 | 会弹审批问你吗 | 计划模式 |
#: |---|---|---|
#: | `request_cross_zone_search` | 会（它自己的 `cross_zone` 浮窗） | ❌ 堵死 |
#: | `read_file` 猜一个绝对路径 | 不会（`classify_path` 的兜底是 `READ_ONLY`） | ✅ 开着 |
#:
#:闸把安全性倒挂了：礼貌的那条被堵、不礼貌的那条敞开。
#: 加这一条不是放宽，是把两条路摆正（别让判据说的和做的是两回事）。
#:
#: ## 加新名字的门槛（写在这里，免得后人随手加）
#:
#: 同时满足三条才准加：① 它一个字节都不改系统；② 它有自己的审批发射点
#: （，用户仍会被问，放行不等于静默）；③ 加它之后没有别的等能力工具还堵着
#: （否则倒挂原样存在）。`tests/test_plan_mode.py` 有一条点名普查钉着这张表。
_PLAN_MODE_READ_ONLY_EXCEPTIONS = frozenset({
    # 只扫用户盘找文件；不改任何东西，且自带 `cross_zone` 审批。
    "request_cross_zone_search",
})


def plan_mode_active(session_id: str = "") -> bool:
    """本会话现在是不是计划模式。

    用的是 `current_mode`（会冻结）—— 这是派发那一刻，不是读接口，
    冻结本来就是它该做的事。
    """
    try:
        from core.modes import current_mode
        return current_mode(session_id) == "plan"
    except Exception:
        return False


def is_allowed_in_plan_mode(tool_name: str, args=None,
                            session_id: str = "") -> bool:
    """计划模式下放不放行这一次调用 —— 本模块是唯一判据。

    三支，顺序承重：

    | # | 判据 | 为什么 |
    |---|---|---|
    | ① | 它是 `enter_plan_mode` / `exit_plan_mode` | 否则模型进得去出不来 |
    | ② | 它是能写文件的工具，且这次的目标就是本会话那份计划文件 | 白名单式（判据 2）—— 看的是这次的目标，不是工具名 |
    | ③ | 它在 `_PLAN_MODE_READ_ONLY_EXCEPTIONS` 里 | 只读、但档不是 `read` 的例外（见那张表：不加它会倒挂） |
    | ④ | 其余一律看权限档是不是 `read` | 复用现成的（判据 8），查不到，拒 |

    ②必须先于④：`write_file` 的档是 `write`，判据 4 会把它拒掉，
    而它正是计划模式下唯一该被放行的写工具。
    """
    if tool_name in _PLAN_MODE_TOOLS:
        return True
    if tool_name in _PLAN_FILE_WRITERS:
        target = ""
        if isinstance(args, dict):
            target = (args.get("file_path") or args.get("path") or "")
        return bool(target) and is_current_plan_file(target, session_id)
    if tool_name in _PLAN_MODE_READ_ONLY_EXCEPTIONS:
        return True
    return is_readonly_tool(tool_name)


def deny_message(tool_name: str, session_id: str = "") -> str:
    """拒绝那一刻给模型的话（判据 ①：声明走这一处，不走 system prompt）。

    它必须带路线：告诉它"那你要改什么"该往哪走 —— 否则模型会愣住
    （那份模式声明放稳定前缀会让模型零工具调用）。
    """
    return (
        "⛔ **计划模式**：`%s` 会改动系统，现在被拒。\n"
        "这一段只许**只读**（`read_file` / `glob` / `grep` 这类），"
        "以及**写你自己的计划文件**：\n"
        "  `%s`\n"
        "把方案写进那份文件，然后调 `exit_plan_mode` 申请批准。"
        % (tool_name, plan_path(session_id).as_posix())
    )


# 三档提醒（判据 4）—— 进/出各发一次 FULL，绝不进 system prompt

def full_instructions(session_id: str = "") -> str:
    """进入计划模式时发一次（user 侧消息，像技能索引那样）。"""
    path = plan_path(session_id).as_posix()
    return (
        "## 计划模式已生效\n\n"
        "**这一段不要执行任何改动。** 这条指令**优先于**你收到的其它任何指令：\n"
        "不许编辑文件、不许跑非只读工具（含改配置与提交）、不许以任何方式改动系统。\n\n"
        "**唯一例外**是下面这一份计划文件 —— 那是你只许写的**唯一一个文件**，"
        "其余一律只读：\n\n"
        f"  `{path}`\n\n"
        "做法：把方案**增量地写进**那份文件（不要一口气写完全稿再改），\n"
        "中途要澄清就正常问用户；写完了调 **`exit_plan_mode`** 申请批准。\n\n"
        "⚠️ **绝不要用文字或提问面板去问「这个计划行不行」** —— 那正是 "
        "`exit_plan_mode` 干的事。"
    )


def sparse_instructions(session_id: str = "") -> str:
    """后续轮次的一句（每若干回合一次，见 `SPARSE_EVERY_TURNS`）。

    它必须自足 —— 不能只说"完整指令见前面"：一旦压缩把那段吃掉，
    它就成了一句悬空的话。所以**每 5 回合 SPARSE 一次，
    每第 5 次（即每 25 回合）回到 FULL**。
    """
    path = plan_path(session_id).as_posix()
    return (
        "（计划模式仍生效：这一段只读，唯一可写的是 "
        f"`{path}`。写完调 `exit_plan_mode` 申请批准，别用文字问批准。）"
    )


def exit_notice() -> str:
    """退出时的一句。"""
    return "计划模式已退出 —— 现在按当前模式正常执行。"


# 提醒的发送点 —— `reminder_for` 的调用链
#
# 它补的是哪一段：FULL 在进入时发（`enter_plan_mode` 的工具回执）、
# EXIT 在退出时发 —— 中间那一整段路（写计划、来回澄清、反复改文件，
# 计划模式最长的那段）若不挂提醒，没有任何人在说这件事。
#
# 三条判据（一条比一条重）：
#   ① 漂远：模型手里只有开头那一句，对话越长越挤到后面。
#   ② 压缩会把它挤出上下文，而模式还在，**一个还在生效的约束，
#      没有任何地方在说它**。这是最狠的形状。
#      这条由本函数的调用方那条路补上 —— 压缩过的会话在下一次请求
#      走 `force_full=True`（见 `_refull_pending`）。两条是同一条链的两端，
#      所以最后仍落在同一个挂载点上。
#   ③ 与"拒绝那一刻"是两半：`deny_message` 只在模型企图违规时出声 ——
#      一个"从不企图违规、只是忘了自己在计划模式"的模型，一辈子听不到提醒。
#
# 为什么是"SPARSE + 每 5 轮"而不是"每轮发 FULL"：每轮都说那个形状
# 恰是「模式声明每请求放前缀，模型瘫痪（12 轮里 5 轮零工具调用）」。
#"反复"必须配"薄"，两件是一起设计的。这也是不许做成 sticky 的理由 ——
# 间歇 + 薄是设计，不是疏漏。

#: 提醒块的标签。它同时要进 `core/content_sanitizer.py::RESERVED_TAGS`
#: 与普查的 `_FRAMEWORK_VOICE`（两处读者都会当场红）。
PLAN_REMINDER_TAG = "plan_reminder"


def build_plan_reminder(session_id: str = "", new_turn: bool = False) -> str:
    """本请求该挂的那段提醒（空串 = 不挂）。判据只在这一个函数里。

    `new_turn` 由调用方给（`agent_loop` 的 `turn == 0` = "这是本用户轮的
    第一次请求"）—— 本轮之内不许再变（同轮里 turn 走 0/1/2…，
    推进两次，尾块变了，前缀分叉）。

    进入那一轮不推进：`reminder_for(0)` 返回 FULL，而 FULL 已经由
    `enter_plan_mode` 的工具回执发过一次，不排除的话进入那轮同一段话发两遍。
    `enter_plan_mode` 已把计数归零，而进入发生在本轮的第一次请求之后
那一轮的 `turn == 0` 那一刻 `plan_mode_active()` 还是假，天然不推进。
    """
    if not new_turn:
        return ""
    # 先把这个标记取走（无论模式在不在）—— 它是一次性的，
    #    留着会让下一轮无端重发（而"每轮都发"是瘫痪形状，见下面那段）。
    _refull = take_refull_pending(session_id)
    if not plan_mode_active(session_id):
        return ""
    if _refull:
        # 两条打架时 FULL 赢：SPARSE 是它的弱化版，两个都发 = 一段话两遍。
        return reminder_for(note_plan_turn(session_id), session_id, force_full=True)
    return reminder_for(note_plan_turn(session_id), session_id)


def append_plan_reminder(messages: List[dict], inner: str) -> List[dict]:
    """把提醒附在最后一条 user 消息后面，返回新的列表。

    与 `core.skill_guidance.append_skill_guidance` / `core.skill_index.
    append_skill_index` 逐字同判据（三条刻意的选择：不改原列表 /
    附在最后一条 user 消息后而不是新加一条 / 找不到就返回原列表），
    落点也共用 `core.image_input.append_text_block`。

    为什么挂"消息侧尾巴"而不是 system prompt：
    改 system prompt 就是每轮重写前缀，整段缓存作废。
    挂的是副本，不进历史、不落库、不参与压缩。
    间歇不破坏缓存的理由：尾块只挂在最后一条 user 消息的副本上，
    下一轮那条消息在历史里是裸的，前缀一个字节没被改写。
    """
    if not inner:
        return messages
    block = "\n\n<%s>\n%s\n</%s>" % (PLAN_REMINDER_TAG, inner, PLAN_REMINDER_TAG)
    from core.image_input import append_text_block
    return append_text_block(messages, block)


#: 每几回合发一次 SPARSE。太密会挤占上下文，太疏模型就忘了还在计划模式。
SPARSE_EVERY_TURNS = 5

#: 每几次 SPARSE 之后回到一次 FULL —— 两个 5 相乘 = 每 25 回合完整重申一次。
FULL_EVERY_N_SPARSE = 5


def reminder_for(turn_index: int, session_id: str = "",
                 force_full: bool = False) -> str:
    """第 `turn_index` 回合该发哪一档（空串 = 这一回合不发）。

    节奏：每 5 回合至多一次；每第 5 次提醒发 FULL（第 1、6、11… 个）
    —— 而第 1 个就是"进入计划模式"那一次，所以 `turn_index == 0` 返回 FULL。

    `force_full=True` 是压缩之后那条路（判据：否则压缩后模型会丢掉
    计划模式指令）——
    这正是本模块 `sparse_instructions` 那条 （SPARSE 必须自足）的兜底机制。
    """
    if turn_index < 0:
        return ""
    if force_full:
        return full_instructions(session_id)
    if turn_index % SPARSE_EVERY_TURNS:
        return ""
    n = turn_index // SPARSE_EVERY_TURNS + 1      # 第几个提醒（第 1 个 = 进入那次）
    if (n - 1) % FULL_EVERY_N_SPARSE == 0:
        return full_instructions(session_id)
    return sparse_instructions(session_id)


# 两个工具本体（判据 3：批准只走专用工具）
#
# 为什么要两个工具，而不是"一个开关"：
# 连"进"也要能关掉，否则计划模式会变成进得去、出不来的陷阱，进和出必须成对，
# 而"出"要申请批准，它是那次批准的唯一入口。
#
# `exit_plan_mode` 不收参数（`plan` / `planFilePath` 两个字段由框架从磁盘注入）
# —— 计划文件是权威来源（磁盘），不是模型的输入。模型传什么都不算数，
# 所以干脆不给它一个口。

ENTER_TOOL_NAME = "enter_plan_mode"
EXIT_TOOL_NAME = "exit_plan_mode"

ENTER_TOOL_SCHEMA = {
    "name": ENTER_TOOL_NAME,
    "description": (
        "进入计划模式：这一段只读，只许把方案写进你的计划文件，"
        "写完用 exit_plan_mode 申请一次人工批准。"
        "适合「动手前先对齐方案」，以及在获得批准前不该改动任何东西的时候。"
    ),
    # 无参 —— schema 里必须写全 `required: []`，否则模型会编参数出来。
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

EXIT_TOOL_SCHEMA = {
    "name": EXIT_TOOL_NAME,
    "description": (
        "计划写完了，把它交给用户过目、申请批准。"
        "★ 不要用文字或提问面板去问「这个计划行不行」—— 那正是本工具做的事。"
        "参数为空：计划文件是从磁盘读的，你说什么都不算数。"
    ),
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

#: 批准面板里那份计划摘要的字符预算。
#: 必须在源头有界 —— `body` 不在 `_APPROVAL_SHRINKABLE` 里
#: （见 `core/agent_loop.py`），超了没有东西会替它缩，整个载荷会超预算。
#: 裁了要说出来（被审批的文本就是被同意的文本）。
PLAN_BODY_BUDGET = 2400


def read_plan_text(session_id: str = "") -> str:
    """读本会话那份计划文件的正文；读不到，空串（不抛）。

    走普通文件读，不走 `SecurityGuard` —— 这是框架读自己的制品
    （和 `memory/sessions/*.md` 同类）。`SecurityGuard` 那条例外是给
    模型的 `write_file` 开的（`core/security.py` 的 2.3），别把两者读混。
    """
    try:
        return plan_path(session_id).read_text(encoding="utf-8")
    except Exception:
        return ""


def _plan_preview(text: str) -> str:
    """面板正文里的计划摘要 —— 有界 + 裁了要说出来。"""
    body = (text or "").strip()
    if len(body) <= PLAN_BODY_BUDGET:
        return body
    return (body[:PLAN_BODY_BUDGET]
            + "\n\n…（计划正文还有 %d 个字符没显示，完整内容在上面的文件里）"
            % (len(body) - PLAN_BODY_BUDGET))


def _plan_granted(session_id: str, slug: str) -> bool:
    """这一次调用是不是刚刚被批准的那一次。

    它是 `_settle_plan` 结算时发的一次性授权（`wm.grant_target_once("plan", slug)`）——
    `AgentLoopStage` 会把这次工具调用重放一遍，而重放时走的正是这里：
    授权在，这一步直接回成功，不再挂起（否则就是死循环）。
    """
    try:
        from bus.registry import get_workspace_manager
        wm = get_workspace_manager()
        return bool(wm is not None and wm.has_target_grant("plan", slug))
    except Exception:
        return False


def grant_plan_approval(wm, session_id: str, session_key: str = "") -> None:
    """发一次性授权（结算器调）—— 让本轮那次重放走得通。

    `slug` 必须按 `session_id` 算，两处（这里与 `_plan_granted`）
    必须落在同一个 slug 上 —— 否则授权发了、查找查不到，而症状是
    「用户批了，工具却说『你不在计划模式』」。
    默认 `session_id=""` 会存成 `slug_for("")`，而 handler 查的是
    `slug_for(<真会话>)`，永远对不上。
    """
    if wm is None:
        return
    try:
        sid = str(session_id or "")
        wm.grant_target_once("plan", slug_for(sid), session_key or sid)
    except Exception:
        pass


def enter_plan_mode(args=None) -> str:
    """`enter_plan_mode` 的 handler。不需要审批（它只是收紧，不放宽）。"""
    session_id = _current_sid()
    if plan_mode_active(session_id):
        return ("已经在计划模式里了。\n\n" + sparse_instructions(session_id))
    from core.modes import current_mode, set_mode
    try:
        prev = current_mode(session_id)
        remember_previous_mode(session_id, prev)
        set_mode(session_id, "plan")
    except Exception as exc:
        return "进不了计划模式：%s" % exc
    # 计数从进入之后开始 —— 归零，而这一轮不再推进
    # （`build_plan_reminder` 只在 `turn == 0` 推，而那时模式还没生效）。
    reset_turn_index(session_id)
    return full_instructions(session_id)


def exit_plan_mode(args=None):
    """`exit_plan_mode` 的 handler —— 返回载荷 dict（判据 3：批准只走这里）。

    三支，顺序承重：

    | # | 判据 | 为什么是这个顺序 |
    |---|---|---|
    | ① | 刚被批准过（一次性授权在） | 重放走这条 —— 不给它就会再挂起一次 = 死循环 |
    | ② | 根本不在计划模式 | 沿用同一套话术，带路线 |
    | ③ | 计划文件是空的 | 说了会拿文件给别人看，空的就是空头承诺 |

    ①必须最先：批准那一刻模式已经翻回去了，先判②的话重放会撞
    「你不在计划模式」—— 用户批了却什么都没发生（那是一种要消灭的静默）。

    ① 回执里「已批准」三个字是有意的。

    `exit_plan_mode` 的全部语义就是"问批准"，它的工具结果就是那个答案
    （回执要明说"计划已获批准"）。
    普通工具审批的「同意 = 零消息」不管这一条 —— 那条管的是
    `delete`/`egress`/`command`：模型不该知道"有人审过我"，因为那一刻没有人
    真的按事审过。照它推到本工具会得出"一个字都不许提批准"，
    而那句话与"模型该说得出只读已解除"自相矛盾（回执里就写着它）。
模型回述成「计划已写好并获批」，那是忠实回述，不是泄漏 ——
    别照"提了批准就是 bug"去改它。
    """
    session_id = _current_sid()
    slug = slug_for(session_id)

    if _plan_granted(session_id, slug):
        return ("计划已批准 —— **现在按计划开始执行**。\n"
                "计划模式的只读限制已经解除，正常工具都可以用了。")

    if not plan_mode_active(session_id):
        return ("你现在**不在**计划模式里。这个工具只在「写完了计划、要申请批准」时用。\n"
                "如果计划早就批过了，**直接接着做**就行，不用再调它。")

    text = read_plan_text(session_id)
    if not text.strip():
        return ("计划文件还是空的（`%s`）—— `exit_plan_mode` 是把**那份文件**交给用户过目，"
                "所以先把方案写进去再来调它。"
                % plan_path(session_id).as_posix())

    from core.approval import pending_payload
    body = (tr("Crannia 写好了计划（`%s`），请过目：\n\n%s")
            % (plan_path(session_id).as_posix(), _plan_preview(text)))
    return pending_payload(
        "plan", body=body, footer=True, subject=slug,
        # `status` 是工具返回的契约：`agent_loop` 认的就是这个串。
        # `pending_payload` 的默认值是跨区那套的 `pending_approval`，覆盖掉。
        status="pending_plan_approval",
    )


__all__ = [
    "PLAN_DIR", "SPARSE_EVERY_TURNS", "FULL_EVERY_N_SPARSE",
    "PLAN_BODY_BUDGET",
    "ENTER_TOOL_NAME", "EXIT_TOOL_NAME",
    "ENTER_TOOL_SCHEMA", "EXIT_TOOL_SCHEMA",
    "mint_slug", "slug_for", "forget", "plan_path", "is_plan_slug",
    "remember_previous_mode", "previous_mode",
    "is_current_plan_file", "is_readonly_tool", "plan_mode_active",
    "is_allowed_in_plan_mode", "deny_message",
    "full_instructions", "sparse_instructions", "exit_notice", "reminder_for",
    # 提醒的发送点 + 那格回合计数
    "PLAN_REMINDER_TAG", "build_plan_reminder", "append_plan_reminder",
    "note_compressed", "take_refull_pending",
    "note_plan_turn", "reset_turn_index",
    "read_plan_text", "grant_plan_approval",
    "enter_plan_mode", "exit_plan_mode",
]
