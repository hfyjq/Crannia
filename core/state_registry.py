"""跨会话共享态的唯一登记表。

两个用途、一张表：

  ① 会话级（purge 时清）—— :func:`purge_session` 是唯一入口。
     `/clear` 的模块级内存清理全部走它，不许再各自 `import` 各自的 `forget_*`。
  ② 进程级（并发时保护）—— 这里登记「谁在进程里跨会话共享、
     今天谁保护它」。没有这一列，拆共享锁就是摸黑拆。

为什么必须是一张表而不是一串清点：purge 若被写成
*a list of places* rather than an invariant，就必然出现
「加了新的会话级状态、忘了加清点」。这张表把"清点"变成"遍历 + 对账"。

三条判据，每一条都往两边查：
  1. census：`core/` + `bus/` 里「模块级的名字，且它在 import 之后会被写」
     与几个长寿单例（`PipelineScheduler` / `MemoryManager` / `SessionPool` /
     `EventBus` / `SessionStore`）`__init__` 里的实例容器 —— 本表必须恰好
     覆盖它们：漏登记红，登记了不存在的东西也红；
  2. behavior：每条带 `purge` 的条目就地植入 → :func:`purge_session` → 探针确认干净；
  3. fixtures：有 `purge` 的条目必须配测试夹具，多一条少一条都红。

census 的判据是「一句话」：模块级的名字，且它在 import 之后会被写。
三条任一命中：

| # | 判据 | 为什么 |
|---|---|---|
| ① | 被写过：`X[k]=` / `del X[k]` / `X +=` / `X.<容器方法>()` | 已经写过了 |
| ② | 被 `global X` 声明过 | 声明 global 就是在说"我会从函数里改它" |
| ③ | 初值是 `None`（`X = None`） | 空槽等着被写（只读的那几次运行里它永远没被写过） |

为什么不按"值长什么样"认：那会漏掉 `_depth = 0` 这种 `int` 标量，
也会漏掉 `global X; X = ...` 这种普通赋值 —— 而它们是同一件事的两种写法。
枚举"值长什么样"就是下一次漏掉的起点。
（换判据当天扫描结果与旧判据逐格相同，新那半条没有反例，
所以读者里配了自造靶子（合成源码），否则它是"绿了但没测到"。）

明说的边界：census 只扫 `core/` + `bus/`、只认那几个类。
`providers/` / `channels/` / `web/` 里的长命单例、以及第六个长寿单例会看不见
—— 加新形态时先扩本模块的 docstring 与读者，别默默绕过。
常量（`_TABLES` / `TIERS` / schema……）只读不写，不要求登记；
而一个"装载期写入、运行期只读"的容器（如 `registered_stages`）要登记，
guard 里写明它为什么不需要锁。
`contextvars.ContextVar` 按定义不在扫描面（每任务一份、自洁、不跨上下文）
—— 与「它是状态」不矛盾，只是它不是这张表管的那个状态
（同 `core/request_snapshot.py::_current` 那条注释）。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple


def _lazy(module: str, func: str, takes_sid: bool = True) -> Callable[[str], None]:
    """惰性 import 的 purge 适配器。

    不在本模块顶层 import 那些模块：本表被 `core/memory/manager.py`
    引用，顶层 import 会把 `bus.registry` / `core.file_tools` 这些重模块拉进
    每一条 import 链，还可能绕出循环 import。清点是低频动作
    （一次 `/clear` 一次），惰性 import 的代价可以忽略。
    """
    def _run(session_id: str) -> None:
        fn = getattr(importlib.import_module(module), func)
        if takes_sid:
            fn(session_id)
        else:
            fn()
    return _run


@dataclass(frozen=True)
class SessionState:
    """一条按会话的状态。

    `where` 是机读的（census 靠它配对）：文件::符号 或
    文件::类.属性（实例态）。`purge=None` = 它不经过 purge_session，
    由 `cleared_by` 说的那条机制清 —— 那也必须是一条具名机制。
    """
    name: str
    where: str
    cleared_by: str
    purge: Optional[Callable[[str], None]] = None
    note: str = ""


@dataclass(frozen=True)
class ProcessState:
    """一条进程级共享态。`guard` 回答：今天谁保护它、拆锁时要问什么。"""
    name: str
    where: str
    guard: str
    note: str = ""


# 会话级 —— 由 `purge_session` 亲手清（behavior 读者按条验）

SESSION_SCOPED: Tuple[SessionState, ...] = (
    SessionState(
        "discovered_tools", "bus/registry.py::_discovered", "purge_session",
        _lazy("bus.registry", "forget_discovered"),
        "不清：下一段对话继承上一段挑出来的工具表，且那些 schema 进压缩闸门"
        "⇒ 压缩被推早而**不留一行日志**"),
    # 这一段碰过哪些子目录（之后指令文件按目录发现）。
    #   不清：上一段读过的子目录，它的 `LEAF.md` / `AGENTS.md` 会一直挂进
    #   下一段的系统提示词 —— 那是每一轮都在付的钱，而且用户看不见它为什么在。
    SessionState(
        "leaf_touched", "core/leaf_md.py::_touched", "purge_session",
        _lazy("core.leaf_md", "forget"),
        "不清：上一段碰过的子目录的指令会挂进下一段的系统提示词（每轮付费）"),
    # 这里不能加一格「本会话调用过哪些 skill 的工具」：
    #   `list_workspace_files` 属于 `skill:file_parser` 且常驻、每个会话第一步就调
    #它会把 PDF 那一片领域纪律挂到每一个会话上（哪怕这次跑的是 Excel）。
    #   更何况它冗余：要调一个非常驻工具，它必须先在 `_discovered` 里。
    #引导片段的判据是一句话（"被 `search_tools` 发现过"），
    #   状态全部住在上面那格 `discovered_tools` —— 那一格本来就有主。
    SessionState(
        "todos", "core/todos.py::_todos", "purge_session",
        _lazy("core.todos", "forget"),
        "不清：下一段对话一开口就看见上一段任务的清单"),
    SessionState(
        # 常驻 shell：不清的话，上一段对话起的 bash 进程会一直活着
        #   （连同里面的变量 / venv），而新一段对话第一条命令会沿用它的 cwd。
        "persistent_shell", "core/persistent_shell.py::_sessions", "purge_session",
        _lazy("core.persistent_shell", "close_session"),
        "不清：上一段的 shell 进程一直挂着，且新一段第一条命令沿用它的工作目录"),
    SessionState(
        "file_reads", "core/file_tools.py::_reads", "purge_session",
        _lazy("core.file_tools", "forget_reads"),
        "不清：看起来已读过 ⇒ CAS 放行一次它从没做过的写前读"),
    SessionState(
        # 本轮改动了哪些文件（UI 的「查看改动文件」下拉框）。
        #   轮末取走并清空（`take`），所以正常情况它一直是空的；
        #   `/clear` 仍然要清 —— 否则"改了 3 个文件"那半条会跨过 `/clear`
        #   落到新会话的第一轮上（把之前的算成"这次的"）。
        "touched_files", "core/touched_files.py::_touched", "purge_session",
        _lazy("core.touched_files", "forget"),
        "不清：新会话第一轮会把**上一段对话**改过的文件算成'本轮改的'"),
    SessionState(
        # 跑着的时候用户补的那句话：未取走的收件箱。
        #   轮末由调度器取空（`steer.drain`），所以正常它一直是空的；
        #   `/clear` 仍然要清 —— 否则上一段没赶上注入的那句会落进新一段
        #   的第一轮（用户完全不知道它为什么会出现在那里）。
        "steer_inbox", "core/steer.py::_inbox", "purge_session",
        _lazy("core.steer", "clear"),
        "不清：上一段没赶上的那句会落进新一段的第一轮"),
    SessionState(
        # IM 通道的执行队列：这条会话排队等着跑的那几条。
        #   它是"活过一次请求"的会话态（worker 线程 + 队列按会话键挂）。
        #   `/clear` 之后还留着，上一段排的那几条会落进新一段的第一轮，
        #   而用户完全不知道它们为什么会出现在那里（他刚清空过）。
        "channel_turns", "core/channel_turns.py::_workers", "purge_session",
        _lazy("core.channel_turns", "purge"),
        "不清：上一段排队的那几条会落进新一段的第一轮"),
    SessionState(
        # 摘要器健康账本：会话 → 连续降级次数（到线就跳过模型摘要与重试）。
        #   不清：上一段的失败次数会算到新一段头上，新一段一开口就被跳过前两级。
        "summarizer_breaker", "core/memory/compressor.py::_breaker",
        "purge_session",
        _lazy("core.memory.compressor", "forget_breaker"),
        "不清：上一段的连续降级会算到新一段头上，新一段一开口就被跳过前两级"),
    SessionState(
        "context_report_last", "core/context_report.py::_last", "purge_session",
        _lazy("core.context_report", "forget"),
        "不清：面板第一次打开，'变化量'是上一段对话长出来的"),
    SessionState(
        "context_report_memory", "core/context_report.py::_last_memory", "purge_session",
        _lazy("core.context_report", "forget"),
        "同 context_report_last —— 它管的是记忆注入那一格"),
    SessionState(
        # 本会话计划文件的 slug。
        #   要清（与"模式"那条正好相反）—— `/clear` 复用会话键，slug 不清
        #   就等于把上一段的计划文件接着用（文件名若逃出了会话键，
        #   才可能做到"清引用、留文件"）。
        "plan_slug", "core/plan.py::_slugs", "purge_session",
        _lazy("core.plan", "forget"),
        "不清：新会话进计划模式会**接着写上一段那份计划文件**"
        "（旧计划没删，但新会话指向了它）"),
    # 进计划模式之前那一档（记下进入之前的那一档）。
    #   它与 slug 同一个函数清（`core.plan.forget` 一次清两格）——
    #   登记成一行是因为它是另一个事实（"从哪来"而不是"写到哪去"），
    #   而清它的判据只有一条：复用会话键的东西都得清。
    #   不清的后果不重（下一段对话会在退出计划模式时回到"上一段进来之前那一档"），
    #   但那是一句假话，登记它就是让它别成一句没人知道的假话。
    # 进计划模式之后过了几个用户轮 —— 定期提醒（`reminder_for`）的计数器。
    #   与上面两格同一个会话键、同一个清理函数（`core.plan.forget` 一次清四格）。
    #   不清的后果不像前两格那么无害：下一段对话会接着上一段的数
    #   从第 7 轮数起，该发的 SPARSE 早发或晚发，而症状是"提醒的节奏对不上"，
    #   看不出跟 `/clear` 有关系。
    SessionState(
        "plan_turn_index", "core/plan.py::_turn_index", "purge_session",
        _lazy("core.plan", "forget"),
        "不清：新会话接着上一段的回合数继续数 ⇒ 提醒节奏错位"),
    # 刚被压缩过、下一次请求要重发 FULL 那个标记。
    #   它与上面三格同一个会话键、同一个清理函数（`core.plan.forget`）。
    #   不清的后果轻（它是一次性的：下一次新用户轮读到就清），
    #   但它是上一段的事实，跟过去就是替上一段把那次 FULL 补发掉
    #   —— 「复用会话键的东西都得清」，一个也不能靠"后果轻"赖掉。
    SessionState(
        "plan_refull_pending", "core/plan.py::_refull_pending", "purge_session",
        _lazy("core.plan", "forget"),
        "不清：上一段'刚被压缩过'跟到下一段，替它把那次 FULL 补发掉"),
    SessionState(
        "plan_pre_mode", "core/plan.py::_pre_modes", "purge_session",
        _lazy("core.plan", "forget"),
        "不清：新会话退出计划模式时会还原成**上一段对话进来之前**那一档"),
    # 审批超时：每会话一格 `{"event": …, "timer": …}`。
    #   它刻意放在模块级（不是 `PipelineScheduler` 的字段）—— 因为
    #   `purge_session` 的清理走 `_lazy(module, func)`，只认模块级函数，
    #   而 scheduler 是实例。放实例上，`/clear` 清不掉，
    #   上一段对话那条倒计时接着跑、到点去动新一段对话的审批。
    SessionState(
        "approval_timers", "core/approval_timeout.py::_timers",
        "purge_session", _lazy("core.approval_timeout", "forget"),
        "不清：上一段的倒计时穿过 /clear 接着跑，到点去动**新一段对话**的审批"),
    # 审批超时："这个会话已经为坏配置喊过一次"。
    #   它是按会话键的（不同的会话各该被通知一次），进这张表。
    #   不清的后果是实质的：用户改了个看不懂的值 -> 我们喊一声；
    #   `/clear` 之后同一份坏配置不再出声，那就是「静默忽略一个用户改过的键」，
    #   而 `core/tiers.py` 记过同一条：那比不许改更坏。
    #   （这条登记不是走形式 —— 是 census 当场抓出来的漏登记。）
    SessionState(
        "approval_timeout_noted", "core/approval_timeout.py::_bad_noted",
        "purge_session", _lazy("core.approval_timeout", "forget_noted"),
        "不清：下一段对话里同一份坏配置不再出声（静默忽略用户改过的键）"),
    SessionState(
        "auto_denials", "core/auto_review.py::_denials", "purge_session",
        _lazy("core.auto_review", "clear_denials"),
        "不清：新一段对话**继承上一段的疲劳度** ⇒ 上一段里连着被拦 3 次之后，"
        "新对话的第一条命令就直接**退回问人**（分类器等于没开）"),
    SessionState(
        "model_activity", "core/agent_loop.py::_last_model_activity", "purge_session",
        _lazy("core.agent_loop", "forget_model_activity"),
        "不清：`/clear` 之后第一轮会把'上一段对话的最后一轮'当成空闲起点 ⇒ "
        "刚清完就判成'很闲'。不过新会话本来就没有旧结果可清、"
        "清也无害 —— 登记的理由是**它就是会话态**，不是因为它危险"),
    SessionState(
        "cancel_flag", "core/cancellation.py::_cancelled", "purge_session",
        _lazy("core.cancellation", "clear"),
        "轮末调度器已自清（finally）；这里覆盖'/clear 那一轮设下、没被读到'的那一点"),
    # 「直接掐」：两格新状态。
    # 两条的清理动作不一样，别照抄：进程表先杀再清
    #    （前台命令没人等了，留着它继续改文件才是危险的那一侧），
    #    而"下次开口先说一句"那格只清（它是一句话，没有可杀的东西）。
    SessionState(
        "cancel_procs", "core/cancellation.py::_procs", "purge_session",
        _lazy("core.cancellation", "purge"),
        "★★ **杀 + 清**（与 `jobs_table` 的**只清不杀**相反，那是刻意的）："
        "作业是用户明确要求转后台的，而这里挂的是**前台命令** —— "
        "会话都清了，已经没有人在等它"),
    SessionState(
        "response_language", "core/response_language.py::_session", "purge_session",
        _lazy("core.response_language", "clear_session"),
        "★ `/language` 的本会话覆盖（**不落盘**）。不清：下一段对话沿用上一段"
        "选的语言 —— 而它与设置页那个持久默认**不是一个东西**"
        "（`/clear` 之后该回到设置/系统）"),
    SessionState(
        "cancel_interrupt_note", "core/cancellation.py::_interrupted", "purge_session",
        _lazy("core.cancellation", "clear_interrupt_note"),
        "只清（它是一句话，没有可杀的东西）"),
    SessionState(
        "jobs_table", "core/jobs.py::_jobs", "purge_session",
        _lazy("core.jobs", "clear", takes_sid=False),
        "⚠️ 只清表、不杀进程（进程由寿限收）。不清：list_jobs 把上一段对话的"
        "完整命令行曝给模型"),
    SessionState(
        "foreground_procs", "core/jobs.py::_foreground", "purge_session",
        _lazy("core.jobs", "forget_foreground"),
        "前台命令的 Ctrl+B 事件（按会话键）。不清：上一段对话的 proc 登记会留到"
        "下一次转后台时被错误地置事件 —— 而那条命令早就结束了"),
    SessionState(
        "background_pending", "core/tasks_notify.py::_pending", "purge_session",
        _lazy("core.tasks_notify", "forget"),
        "每条会话的待注入后台回报。不清：上一段对话的回报会注进新对话"),
    SessionState(
        "background_dispatching", "core/tasks_notify.py::_dispatching", "purge_session",
        _lazy("core.tasks_notify", "forget"),
        "正在起通知轮的会话。不清：新对话被旧的'正在起轮'标记挡住"),
    SessionState(
        "memory_extract", "core/memory_extract.py::_state", "purge_session",
        _lazy("core.memory_extract", "forget"),
        "后台记忆抽取的三格（游标/节流计数/last_turn_at）+ 在飞标志。"
        "不清：新一段对话继承上一段的节流计数（早抽或晚抽几轮）与旧游标 —— "
        "游标靠指纹失配兜底，但 `last_turn_at` 是旧时刻（冷闸会误判『冷』）"),
    # 这两条是「授权」，不是「数据」 ——
    #   `/clear` 对它们做的是回收权限，不是丢东西。
    SessionState(
        "ice_reveal_exempt", "core/ice/reveal.py::_EXEMPT", "purge_session",
        _lazy("core.ice.reveal", "recover"),
        "★★ **本轮放行过的那几个真值**（放行只活一轮）。"
        "不清的后果：用户在上一次对话里点过一次『同意』⇒ **新一段对话里"
        "那几个真值继续不上保护** —— 一次点击变成跨会话的常设，"
        "而用户以为它只管那一次"),
    SessionState(
        "ice_downgraded", "core/ice/reveal.py::_DOWNGRADED", "purge_session",
        _lazy("core.ice.reveal", "recover"),
        "★★ **本会话关掉了信息层**。★ 这一条是**授权不是数据**："
        "`/clear` 做的是**把它收回**（保护回来），不是删什么。"
        "⚠️ 与上面那条**共用一个 `recover`**（一次调用清两格）—— 有意的："
        "两条都来自同一个弹窗，拆成两个函数就一定会有人只调一半"),
    SessionState(
        "ice_pii_map", "core/ice/store.py::SessionStore._maps", "purge_session",
        _lazy("core.ice.store", "forget_session"),
        "★★ **不清的后果是会话键复用的原形**：会话键是**复用**的 ——"
        "新一段对话会继承上一段的映射，于是**上一段解出来的句柄，这一段的出口照样还原成真值**"
        "（真值从另一个会话渗进这一段）。★ 这也是「登记项里有一类是**授权**"
        "不是数据」的**反面样本**：这一条**就是数据**，`/clear` 对它的动作就是**丢**——"
        "映射一丢，那些句柄永远解不开，而那正是我们要的（句柄只活在「上模型的那一份」）"),
    SessionState(
        "partial_read", "core/partial_read.py::_pending", "purge_session",
        _lazy("core.partial_read", "forget"),
        "★ 「这一次会话里读过、但没读全」。不清的后果是"
        "**上一段那一句会挂到这一段的第一份产出上** —— 而那句话说的那次读"
        "**根本不在这一段里**，模型既看不到它、也无从补救（标签在说谎）。"),
    SessionState(
        # 这个会话最后一次发出去的日期。
        #   不清的后果很轻 —— 新一段对话的第一轮会平白多补一条日期附件
        #   （内容还是对的，只是那句「日期已翻到新的一天」对第一轮是多余的）。
        #   登记它的理由是它就是会话态（按 session 键、活过一次请求），
        #   而不是因为它危险（同 `model_activity` 那一格的写法）。
        "session_last_date", "core/session_date.py::_last_dates", "purge_session",
        _lazy("core.session_date", "forget"),
        "不清：新一段对话的第一轮会把'上一段最后一次发出去的日期'当成自己的起点"
        "（同一天就没事，跨了天就多发一条附件）"),
    SessionState(
        # 模型连接重试的横幅状态。
        #   不清的后果：`/clear` 之后新一段对话的界面还挂着"正在重试 3/10"
        #   （而那个重试属于上一段，早结束了）。
        "llm_retry_state", "core/llm_retry.py::_state", "purge_session",
        _lazy("core.llm_retry", "reset"),
        "★ 红色横幅的会话状态（连接恢复/轮末清）"),
    SessionState(
        # 逐条轨迹的环形缓冲（参数 / 思考 / 结果）。
        #   它是进程内的排障数据：不落盘、不进模型上下文。
        #   不清的后果：`/clear` 之后打开"轨迹"看到的是上一段干过的事，
        #   而用户以为这一轮干过那些（标签在说谎）。
        "trace_records", "core/trace.py::_records", "purge_session",
        _lazy("core.trace", "reset"),
        "★ 默认关（trace_full）；不落盘 —— 落进 data/events 会把日志撑爆"),
    SessionState(
        # 上下文压力的两个会话格子：本次请求的占用比例 · 模型请求过压缩没有。
        #   它们决定 `request_compaction` 发不发（`registry._session_extra_tools`）。
        #   不清的后果：`/clear` 之后新一段对话继承上一段的读数，
        #   明明是刚开聊的小会话，工具表里却挂着一个"请求压缩"的工具。
        "context_pressure", "core/context_pressure.py::_STATE", "purge_session",
        _lazy("core.context_pressure", "forget_session"),
        "★ 压力读数/压缩请求按会话存；不清会把上一段的窗口带到新一段"),
    SessionState(
        # `deliver_file` 登记、通道在回复之后取走发出去的那些文件。
        #   正常情况下每轮都被通道取空（`take_for`），所以它平时是空的；
        #   `/clear` 仍然要清 —— 留下的是"通道没发成"的残留，新一段对话
        #   不该替上一段把文件补发出去。
        "delivery_outbox", "core/delivery_outbox.py::_items", "purge_session",
        _lazy("core.delivery_outbox", "forget", takes_sid=False),
        "不清：上一段没发出去的文件会在新一段的第一轮被通道当成'本轮要发的'"),
)
# `core/request_snapshot.py::_current` 有意不在表里：
# 它是 `ContextVar`（任务级、自洁 —— 每轮新任务新上下文，
# 上一轮的快照带不过来），跨轮 purge 不再需要它，census 也扫不到它
# （`contextvars.ContextVar(...)` 不是容器字面量 —— 登记它就变成"过期登记"）。

# 会话级 —— 由别的具名机制清（实例态，purge_session 够不着；
#              实例 census 反查它们"登记了吗"）

SESSION_SCOPED_BY_OTHER: Tuple[SessionState, ...] = (
    # 两条有意留在阶段层的（别搬）
    # 它们和调度器那 9 格不同：那 9 格的清除机制在调度器自己（轮末收回）；
    # 这两条的清除在 `pre_process` 的 /clear 分支里直调 ——
    # 不搬进 purge_session 的理由是有两条读者钉着位置：
    # `test_tiers.py::test_clear_hook_really_clears`（用假 pool 驱动真阶段，
    # 搬走它够不到）与 `test_git_status_snapshot.py::test_clear_resets_it`
    # （AST 判"必须落在 /clear 分支里"）。不为整洁推翻有判据的读者。
    SessionState(
        "tier_frozen", "core/tiers.py::_frozen",
        "pre_process /clear 分支直调 reset_tier_cache(new_sid)（登记表：这条有意不走 purge_session）",
        note="不清：新会话还按旧档跑（档进缓存键 ⇒ 改档 = 一次冷启动）；"
             "★ 只清**复用的那个 key**，不是全清 —— 六个通道共用一个 scheduler"),
    SessionState(
        "tier_thinking_override", "core/tiers.py::_thinking_off",
        "pre_process /clear 分支直调 reset_tier_cache(new_sid)（与 _frozen 一起清）",
        note="`/effort thinking off` 记在这 —— 单会话、"
             "不落盘。不清：新会话还按上一段那个选择跑（用户的预期是"
             "「`/clear` 之后回默认」）。★ 它是**「关」的集合**，默认=按档表（开）"),
    SessionState(
        "git_status_snapshot", "core/prompt_assembler.py::_git_status_cache",
        "pre_process /clear 分支直调 reset_git_status_cache()（同上有意）",
        note="进程内单槽、每会话算一次；不清则新会话第一轮提示里写着旧分支/旧提交"),
    # 一条没有清除点的（模式轴）
    SessionState(
        "interaction_mode", "core/modes.py::_frozen",
        "★ **有意不清**：模式是**用户的选择**（`/clear` 不动设置），"
        "而「我要重新想一遍」与「我退出计划模式」是**两件事**。"
        "登记在这儿只是让 census 看得见它",
        note="⚠️ 它**没有清除点**：改模式只有 `/mode` 与设置页两个入口（`core.modes.set_mode`）。"
             "★ 所以它也**不进** `SESSION_SCOPED` 的 `purge_session` 清单 —— "
             "别顺手把它加进去，那会让 `/clear` 把用户的模式偷走"),
    # 接管 —— 与 `interaction_mode` 同族，有意不清
    SessionState(
        "computer_takeover", "core/computer/session.py::_state",
        "★ **有意不清** —— 一次启用对整个会话有效，"
        "除非用户关闭软件或再次点击接管按钮。`/clear` 不在那两条里；"
        "停用的两条路是 `session.disable()`（按钮 / 物理输入 / 切走模式）"
        "与进程退出。登记在这儿只是让 census 看得见它",
        note="⚠️ 它是**进程级单例**（Windows 一个登录会话只有一个输入桌面），"
             "只是**记录着**哪个会话持有它。⇒ 不按会话清；"
             "`core.modes.set_mode` 切走完全权限时会调 `disable_for_session`"),
    # 浏览器接管 —— 与 `computer_takeover` 同一次接管
    SessionState(
        "browser_takeover", "core/browser/session.py::_state",
        "★ **有意不清** —— 它与 `computer_takeover` **同一个持有者**："
        "`core.computer.session.disable()` 的监听者会关掉浏览器连接；"
        "`/clear` 不在停用路径里（同接管）",
        note="⚠️ 它是**进程级单例**（一台机器只有一个活动浏览器连接），"
             "只是记录着哪个会话持有它；不按会话清"),
    # 一条根本不是会话态的（图片输入）
    #
    # census 会逮到它（判据是「import 之后被写过」，而它确实被写）——
    # 那是对的。登记在这儿是要把
    # "它为什么不进 purge 清单" 这件事写下来，而不是默默绕过。
    SessionState(
        "image_blob_cache", "core/image_input.py::_blob_cache",
        "★ **不需要清，也不该清** —— 它**不是会话态**：键是**图片文件的路径**"
        "（内容寻址：摘要就是文件名），值是那张图编码后的 data URL。"
        "它是「按**内容**缓存的解码结果」，与谁在哪个会话里问过它无关。"
        "⇒ 登记在这儿只是让 census 看得见一个**进程级缓存**",
        note="⚠️ **它不是「读到了就是对的」**：失效判据是 `(mtime_ns, size)`，"
             "取自 `os.stat` —— 与写前必读同一条（**来源必须跟着值走**）。"
             "清了它的代价只是**多一次解码**（几十毫秒）——"
             "所以「忘了清」在这里的后果是零，这也是它**不进** `purge_session` 的理由。"
             "★ 反过来说：**将来若有人把它改成按 session 键**，"
             "这一条就必须搬进 `SESSION_SCOPED` —— 那时它就是会话态了"),
    # 界面语言的缓存—— 同族：不是会话态
    # census 逮到它是对的（import 之后会被写）。登记在这儿是要把
    # 「它为什么不进 purge 清单」写下来：语言是用户的设置，
    # `/clear` 不许碰它（判据 3，同  /  那两条轴）。
    SessionState(
        "i18n_cache", "core/i18n.py::_cache",
        "★ **有意不清** —— 界面语言是用户设置（`data/settings.json` 的 "
        "`interface_language`），不是会话态；`/clear` 不许改语言。"
        "登记在这儿只是让 census 看得见一个**进程级缓存**",
        note="三格：`packs`（语言包，按 mtime 失效）· `cur`（设置文件读数，"
             "按 mtime 失效）· `bad_noted`（坏值只喊一次）。"
             "★ 清了它的代价 = 下一次多读一次盘，**与谁在哪个会话无关**"),
    # 权限解禁的设置读数—— 同族：不是会话态
    # census 逮到它们是对的（读一次设置就会被写）。`/clear` 不许碰：
    # 那是用户的设置（`data/settings.json` 的 `full_access_unlock`），
    # 与哪一段对话无关。登记在这儿只是让 census 看得见；
    # `bypassing()` 那一格是 `ContextVar`（每轮重新 `pin`），不在这张表里。
    SessionState(
        "unlock_cache", "core/unlock.py::_cache",
        "★ **有意不清** —— 设置读数的失效指纹（`(mtime_ns, size)`），"
        "与哪条会话无关；清了只是下次多读一次盘",
        note="与 `i18n_cache` / `image_input._blob_cache` 同族"),
    SessionState(
        "unlock_cache_value", "core/unlock.py::_cache_value",
        "★ **有意不清** —— 同上：`_cache` 那个指纹对应的布尔值",
        note="两格一起失效；分开登记只是因为 census 按符号扫"),
    SessionState(
        "unlock_bad_noted", "core/unlock.py::_bad_noted",
        "★ **有意不清** —— 「坏值只喊一次」是**进程级**的，不是按会话",
        note="与 `approval_timeout._bad_noted` 不同：那一条按会话键（每条会话各该被提醒一次）"),
    # 出网代理的读数与账本 —— 同族：不是会话态
    # census 逮到它们是对的（读一次系统代理 / 写一次环境变量就会被写）。
    # `/clear` 与它们无关：代理是这台机器的设置，不随对话变化。
    SessionState(
        "_wininet_cache", "core/net_proxy.py::_wininet_cache",
        "★ **有意不清** —— Windows 系统代理的读数（两格：读的时刻 + 地址），"
        "进程级；清了只是下次多读一次注册表",
        note="2 秒过期：代理软件开关 `ProxyEnable` 之后要能很快跟上"),
    SessionState(
        "_reach_cache", "core/net_proxy.py::_reach_cache",
        "★ **有意不清** —— 某个代理地址连不连得上的探测结果，按 URL 键，进程级",
        note="3 秒过期；清了只是多连一次 TCP（超时 0.35 秒）"),
    SessionState(
        "_OURS", "core/net_proxy.py::_OURS",
        "★ **有意不清** —— 我们自己写进环境变量的那几格及其值。"
        "用途是把「我们投影进去的」从「机器上本来就有的」里分出来",
        note="进程级账本；键按大写（Windows 的环境变量大小写不敏感）"),
    SessionState(
        "auto_review_rules_bad_noted", "core/auto_review.py::_rules_bad_noted",
        "★ **有意不清** —— 同上：`auto_review_rules` 那份坏配置是**进程级**的，"
        "不是按会话",
        note="规则来自 `data/settings.json`；改好文件即恢复，`/clear` 与它无关"),
    # 异步钩子的在飞线程—— 同族：不是会话态
    # census 逮到它是对的（跑一条 `async` 钩子就会被写）。登记在这儿是要把
    # 「它为什么不进 purge 清单」写下来：它是线程登记，`/clear`
    # 不许去 join / 清一个正在跑的用户脚本。
    SessionState(
        "hooks_async_threads", "core/hooks.py::_async_threads",
        "★ **有意不清** —— 只是**在飞的后台钩子线程**的登记（测试 join 用），"
        "不是会话态；`/clear` 不许碰它",
        note="线程跑完自己 `discard`；守护线程，进程退出不阻塞。"
             "清了它的代价 = 测试少一个 join 的把手，**对话行为一个字节不变**"),
    # 调度器 9 格：载体归零 + 轮末收回
    # pre_process 的 /clear 分支把 ctx 上的载体清成 False/{}/空，
    # 轮末 `_collect_session_flags` 见到载体为假就把这一格从表里 del 掉。
    # `/clear` 走的是 stop 早退路径 —— 收回发生在轮末，与普通轮同一条路。
    SessionState(
        "pending_cross_zone", "core/pipeline/scheduler.py::PipelineScheduler._pending_cross_zone",
        "pre_process /clear 归零 ctx 载体 → 轮末收回时 del",
        note="不清：下一句话被当成对上一段对话的选项回答"),
    SessionState(
        "pending_delete", "core/pipeline/scheduler.py::PipelineScheduler._pending_delete",
        "同上（四个 pending_* 共用一套派发/结算代码）", note="同 pending_cross_zone"),
    SessionState(
        "pending_delete_bulk",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_delete_bulk",
        "同上（同一套派发/结算代码）",
        note="★ 批量删除的硬闸 —— **自己一格**，"
             "不与 `pending_delete` 共用（共用一套派发/结算会把两件事混在一起）"),
    SessionState(
        "pending_egress", "core/pipeline/scheduler.py::PipelineScheduler._pending_egress",
        "同上", note="同 pending_cross_zone（出网审批）"),
    SessionState(
        "pending_write_outside",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_write_outside",
        "同上（同一套派发/结算代码）",
        note="★ 工作区之外写文件 —— **自己一格**，"
             "不与 `pending_command` 共用（共用一套派发/结算会把两件事混在一起）"),
    SessionState(
        "pending_command", "core/pipeline/scheduler.py::PipelineScheduler._pending_command",
        "同上", note="同 pending_cross_zone（命令执行审批）"),
    SessionState(
        "pending_destructive",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_destructive",
        "同上", note="同 pending_cross_zone（`destructive` 轴的兜底闸）"),
    SessionState(
        "pending_plan",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_plan",
        "pre_process /clear 归零 ctx.pending_plan → 轮末收回时 del",
        note="同上（被审批物是**计划本身**，结算翻的是模式不是目标）"),
    SessionState(
        "pending_ask",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_ask",
        "pre_process /clear 归零 ctx.pending_ask → 轮末收回时 del",
        note="★ **提问面板** —— 自己一格。不清：下一句话会被当成"
             "对上一个问题的回答（同 pending_cross_zone）"),
    SessionState(
        "pending_computer_app",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_computer_app",
        "pre_process /clear 归零 ctx.pending_computer_app → 轮末收回时 del",
        note="★ 接管的**逐应用授权** —— 自己一格。"
             "不清：上一段那个应用的授权浮窗会挂到新会话的第一句话上（同 pending_cross_zone）"),
    # 逐条轨迹的旁两格
    #: 主格 `trace_records` 在 `SESSION_SCOPED`（有 purge）。这两格由同一个
    #:   `trace.reset()` 一起清 —— 拆成三条 purge 只是把"清一次"写三遍。
    SessionState(
        "trace_dropped",
        "core/trace.py::_dropped",
        "/clear → trace.reset(会话键)（与 trace_records 同一处）",
        note="★ 每个会话丢过多少条（'没看见 ≠ 没发生'）"),
    SessionState(
        "trace_seq",
        "core/trace.py::_seq",
        "/clear → trace.reset(会话键)（与 trace_records 同一处）",
        note="★ 轨迹序号（界面按 `since` 增量拉）"),
    SessionState(
        "pending_ice",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_ice",
        "pre_process /clear 归零 ctx.pending_ice → 轮末收回时 del",
        note="同上（被审批物是**一个句柄**，结算发的是"
             "「本轮放行那个值」的授权，不是目标授权）"),
    SessionState(
        "pending_hook_ask",
        "core/pipeline/scheduler.py::PipelineScheduler._pending_hook_ask",
        "pre_process /clear 归零 ctx.pending_hook_ask → 轮末收回时 del",
        note="★ **外部命令钩子要求人工确认** —— 自己一格。"
             "不清：上一段那个「这一步要不要做」的浮窗会挂到新会话的第一句话上"
             "（同 pending_cross_zone）"),
    SessionState(
        "session_skip_delete_approval",
        "core/pipeline/scheduler.py::PipelineScheduler._session_skip_delete_approval",
        "pre_process /clear 置 ctx.session_skip_delete=False → 轮末 discard",
        note="不清：'本次会话内不再问删除'穿过 /clear 活到下一段"),
    SessionState(
        "session_cross_zone_auto",
        "core/pipeline/scheduler.py::PipelineScheduler._session_cross_zone_auto",
        "pre_process /clear 置 ctx.session_cross_zone_auto=False → 轮末 discard",
        note="不清：'不再问这个域名'活到下一段"),
    SessionState(
        "approval_ask_counts",
        "core/pipeline/scheduler.py::PipelineScheduler._approval_ask_counts",
        "pre_process /clear 清成 {} → 轮末 del",
        note="升级项（问第 2/3 次才出现）的记账；不清则新对话第一次问就跳过升级"),
    SessionState(
        "session_target_grants",
        "core/pipeline/scheduler.py::PipelineScheduler._session_target_grants",
        "pre_process /clear 清成 {} → 轮末 del",
        note="选项 5（按目标放行）是**会话级**的 ⇒ 必须清"),
    SessionState(
        "session_cwd", "core/pipeline/scheduler.py::PipelineScheduler._session_cwd",
        "pre_process /clear 里 _reset_session_cwd() 清 wm 投影 → 轮末收回时 del",
        note="不清：新会话第一条命令在上一段最后待过的目录里跑"),
    # MemoryManager 6 格：new_session 就地置零/置初值
    SessionState(
        "_token_total", "core/memory/manager.py::MemoryManager._token_total",
        "MemoryManager.new_session 就地置零（与 purge_session 同一处）",
        note="不清：/context 的累计数是上一段的"),
    SessionState(
        "_note_gate", "core/memory/manager.py::MemoryManager._note_gate",
        "MemoryManager.new_session 就地置 {{tokens:0, tools:0}}",
        note="★ 会话笔记那道量闸的累计器。"
             "不清的代价：上一段那笔「已攒够 10,000」的账跨进新会话 ⇒ "
             "**第一份笔记在一次寒暄之后就立了**（新会话本来就该先攒够量），"
             "而笔记一立起来就被当成「已有的」，后面按 5,000 的门槛更新 ⇒ "
             "前半段的笔记质量被这次空写钉住"),
    SessionState(
        "_saved_count", "core/memory/manager.py::MemoryManager._saved_count",
        "MemoryManager.new_session 就地置零", note="同 _token_total"),
    SessionState(
        "_session_note_cache", "core/memory/manager.py::MemoryManager._session_note_cache",
        "MemoryManager.new_session 就地置空串",
        note="不清：注入段还是上一段的笔记（记忆注入是快照）"),
    SessionState(
        "_pending_memory_inject", "core/memory/manager.py::MemoryManager._pending_memory_inject",
        "MemoryManager.new_session 置初值 True（= 待注入）",
        note="它是'注入一次'闸门的开关；新会话本来就该再注入一次"),
    # `_profile_only` 已删：它是"新会话只带画像、不带 facts"的两档开关，
    #    而画像/facts 两段现在整段删除（只剩会话笔记），它没有可区分的对象了。
    #    census 会把"过期登记"报出来。
    # SessionPool 2 格
    SessionState(
        "_loaded", "core/session_pool.py::SessionPool._loaded",
        "SessionPool.new_session 用新 (sid, msgs) 覆盖",
        note="它是'这个 key 认不认识'的缓存 —— 判据不能只看它（磁盘上还有绑定表）"),
    # 这里不能再加一格 `_model_overrides`（`SessionPool` 的会话模型覆盖）：
    #    那是一条死路（`/model` 往里写、全仓没有读者），保留它就是让
    #    「这一段用什么模型」有两个看起来都对的地方（`core/session_model.py`
    #    那一个才是真的）。正确地删掉它，也让 `/clear` 那条"用户的选择不夺走"
    #    的语义落到了真住处：按存储 id 落库，`/clear` 换线程，天然回默认
    #    （`/clear` 就是开新对话）。
)


# 进程级 —— 跨会话共享；拆共享锁前把这一列读一遍

PROCESS_SHARED: Tuple[ProcessState, ...] = (
    ProcessState(
        "finish_hooks", "core/jobs.py::_finish_hooks",
        "启动期登记、其后只读",
        "无害（钩子是模块函数，不按会话键、不串会话）"),
    ProcessState(
        "start_hooks", "core/jobs.py::_start_hooks",
        "启动期登记、其后只读",
        "无害（同上；`task.started` 挂在这个启动钩子上）"),
    ProcessState(
        "language_bad_setting_noted", "core/response_language.py::_bad_noted",
        "读到一个认不出的设置值之后置一次（`global`）", "无害（一个模块级'喊过了'旗）"),
    ProcessState(
        "incidents", "core/incidents.py::_ITEMS",
        "装载期 / 运行期追加（`note` 里 append）；`clear()` 清空",
        "无害（只增不改的清单，每条自带时间戳；读侧 `all()` 取副本）"),
    ProcessState(
        "workspace_plugins_noted", "core/skill_sources.py::_WS_PLUGIN_NOTE_SHOWN",
        "工作区插件开关关着时置一次（`global`）",
        "无害（同上一条同形：只是让一行提示不刷屏）"),
    ProcessState(
        "task_events_installed", "core/task_events.py::_installed",
        "`install()` 写一次（`global`）", "无害（一个模块级幂等旗）"),
    ProcessState(
        "background_scheduler", "core/tasks_notify.py::_scheduler",
        "install() 写一次；reset() 清", "启动期注入，读侧只读"),
    ProcessState(
        "background_deliver", "core/tasks_notify.py::_deliver",
        "install()/reset() 写", "可选投递器（测试注入）；真路径走 cron 的 resolve_channel"),
    ProcessState(
        "background_thread", "core/tasks_notify.py::_thread",
        "install()/reset()", "一条守护线程（通知调度器）"),
    ProcessState(
        "background_stop", "core/tasks_notify.py::_stop", "reset()",
        "停线程标志；进程退出时自然消失"),
    ProcessState(
        "background_wake", "core/tasks_notify.py::_wake", "install()/reset()",
        "唤醒事件；单条，不按会话键"),
    ProcessState(
        # 最近一次"为什么退出接管"（界面要说一句人话）。
        "_last_stop", "core/computer/session.py::_last_stop",
        "★ **一把模块内锁（`_lock`）** —— `disable()` 在锁内 update；`status()` 在锁内读",
        "⚠️ **`/clear` 不清它** —— 它是**进程级的史**（同 `core.security._recent_commands`），"
        "与哪条会话无关；界面只在 `at` 变了时才提示一次"),
    ProcessState(
        # 接管被关掉时的监听者（Web UI 的横幅 / 托盘）。
        "_listeners", "core/computer/session.py::_listeners",
        "★ **一把模块内锁（`_lock`）** —— `subscribe()` 在锁内 append；"
        "回调在**锁外**执行（监听者要 emit 事件 / 碰 IO）",
        "⚠️ **`/clear` 不清它** —— 监听者是**进程级**的接线，与哪条会话无关"),
    ProcessState(
        # 浏览器接管"已经订阅过关停监听"的幂等旗。
        "_listener_done", "core/browser/session.py::_listener_done",
        "★ 只在 `_ensure_listener()` 里置一次；无锁（重复订阅本身幂等）",
        "⚠️ **`/clear` 不清它** —— 进程级接线；清了会重复订阅"),
    ProcessState(
        # 重复命令的取证装置：同一命令在 30 秒内被记第二次
        #打一行（只出声、不拦 —— 归属未定：模型重发 vs 框架重派，
        #   两者的修法不同，所以先出声、再定修法）。
        "_recent_commands", "core/security.py::_recent_commands",
        "★ **加锁**（`_dup_lock`）—— 六个通道各在自己的线程里跑 `run_command`；"
        "而这张表是**跨会话**的（重复的是**命令原文**，与哪个会话无关）",
        "⚠️ **`/clear` 不清它** —— 它是**进程级的史**，不是会话级的状态"
        "（与 `ice` 的 `_maps` 正相反）。表有 200 条上限、超了丢最早插入的那个"),
    ProcessState(
        "ice_evicted_ledger", "core/ice/store.py::SessionStore._evicted",
        "**有意不加锁**：只在 `_evict_if_needed` 里 append，而那个函数只在"
        "`SessionStore.session()` 建新会话时走一次 —— 六个通道各在自己的线程里"
        "建自己的会话，今天由 `_EXECUTE_LOCK` 全局串行兜着（拆锁时要回答这条）",
        "★ 淘汰**不许静默**（`purge_session` 那条判据：'清失败'与'没东西清'必须"
        "看起来不一样）⇒ 它是一本**台账**，读它的是 `last_evicted()`。"
        "⚠️ **/clear 不清它** —— 它是进程级的史，不是会话级的状态（与 `_maps` 正相反）"),
    ProcessState(
        "_registry", "bus/registry.py::_registry",
        "装载期 + 首调懒装载（on_demand import）写入；今天靠 _EXECUTE_LOCK 全局串行",
        "拆锁时要回答：两个会话**同时**首调同一个 skill，register 的读改写会怎样"),
    ProcessState(
        "cron_store", "core/cron/store.py::_store",
        "★ **一把模块内锁（`_store_lock`）**—— `get_cron_store()` 与 "
        "`reset_cron_store()` 都走它；要它的有两边：定时任务的守护线程"
        "与六个通道各自的轮次",
        "⚠️ **`/clear` 不清它** —— 任务**天生是跨会话的**，"
        "所以它不是会话级状态。`reset_cron_store()` 是它唯一的复位口"
        "（只给测试用；**改了默认库路径的测试必须连它一起复位**）"),
    ProcessState(
        "cron_runner", "core/cron/runner.py::_runner",
        "★ **一把模块内锁（`_runner_lock`）** —— `get_cron_runner()` / "
        "`configure()` / `reset_cron_runner()` 都走它。要它的有两边："
        "bootstrap（`configure` 把 scheduler 交进来）与测试（`reset`）",
        "⚠️ **它自己那条守护线程是第二份共享态**：`_stop` / `_inflight` / `_wake` "
        "住在线程对象里（`start`/`stop` 成对）。★ **`/clear` 不清它** —— "
        "任务是跨会话的，清了就等于把调度器停了。"
        "⚠️ 与 `cron_store` **必须一起复位**：`reset_cron_runner()` 只停线程、"
        "不动库（`reset_cron_store()` 才动库）"),
    ProcessState(
        "registered_stages", "core/pipeline/stage.py::registered_stages",
        "装载期写入（@register_stage 装饰器），运行期只读",
        "不需要锁；要防的是'运行期还有人注册'"),
    ProcessState(
        "_SUBSTATE_HANDLERS", "core/pipeline/stages/pre_process.py::_SUBSTATE_HANDLERS",
        "装载期写入（注册装饰器，:1650-1655），运行期只读", "同 registered_stages"),
    ProcessState(
        "_sibling_cache", "core/skill_doctor.py::_sibling_cache",
        "无锁；今天靠 _EXECUTE_LOCK 全局串行",
        "拆锁时要回答：两会话同时体检 ⇒ 要么加锁、要么把体检挪到装载体外单线跑"),
    ProcessState(
        "_last_seen", "core/skill_doctor.py::_last_seen",
        "无锁；同上", "指纹缓存（键 = 路径），并发写最坏是重复体检一次"),
    ProcessState(
        "_QUARANTINED", "bus/registry.py::_QUARANTINED",
        "`bus/quarantine` 那把 `_LOCK` 护着（读侧无锁即可：它是一个**整体替换**的 set，"
        "读到的要么是旧的要么是新的，不会是半截）",
        "被屏蔽的**插件归属**集合。★ 它是**系统级**的，"
        "`/clear` 不清 —— 一个坏包不会因为用户换一段对话就变好"),
    ProcessState(
        "_cache", "bus/quarantine.py::_cache",
        "`bus/quarantine._LOCK` 护着",
        "账本的内存镜像（`data/runtime/quarantine.json`）；`load()` 填、"
        "`quarantine()`/`release()` 改。⚠️ 它**不是**真相源 —— 真相是那个文件，"
        "这里只是省掉每次读盘"),
    ProcessState(
        "_MCP_DISABLED", "bus/registry.py::_MCP_DISABLED",
        "`bus/mcp_disabled._LOCK` 护着（与 `_QUARANTINED` 同款：**整体替换**的 set，"
        "读到的要么是旧的要么是新的）",
        "**用户主动停用**的 MCP 归属集合。★ 与 `_QUARANTINED`"
        "**分开**（一个是框架判的'坏了'、一个是用户点的'不想跑'）——合并的话，"
        "「解封」会顺手把用户停用的也放出来。⚠️ 它也是**系统级**的，`/clear` 不清"),
    ProcessState(
        "_SERVICES", "core/services.py::_SERVICES",
        "`core/services._lock` 护着；技能装载时写入，重启或退出时清空",
        "技能后台服务的句柄表。进程级，`/clear` 不清：服务与会话无关"),
    ProcessState(
        "_REPORT", "core/skill_load_report.py::_REPORT",
        "`core/skill_load_report._lock` 护着；装载与诊断时写入",
        "装载结果表（失败阶段与原因）。进程级，重启时重写"),
    ProcessState(
        "_SOURCES", "core/inbound.py::_SOURCES",
        "`core/inbound._lock` 护着；注册与撤销都是整条增删",
        "外部事件来源表。进程级，`/clear` 不清：来源是插件装载时注册的"),
    ProcessState(
        "_WINDOW", "core/inbound.py::_WINDOW",
        "`core/inbound._lock` 护着；每次投递追加时间戳，过期的会被剪掉",
        "来源限流窗口。进程级；`/clear` 不清，避免清空后被瞬间刷满"),
    ProcessState(
        "_SEEN", "core/inbound.py::_SEEN",
        "`core/inbound._lock` 护着；每次投递写入去重键，过期的会被剪掉",
        "事件去重表。进程级；`/clear` 不清，避免重复消息在清理后被再投一次"),
    ProcessState(
        "_scheduler", "core/inbound.py::_scheduler",
        "`configure()` 写一次（幂等，重启时只换引用）",
        "外部事件用的调度器引用。进程级"),
    ProcessState(
        "_ACTIONS", "core/outbound.py::_ACTIONS",
        "`core/outbound._lock` 护着；注册与撤销都是整条增删",
        "出站动作表。进程级，`/clear` 不清：动作是插件装载时注册的，与会话无关"),
    ProcessState(
        "_WINDOW", "core/outbound.py::_WINDOW",
        "`core/outbound._lock` 护着；每次发送追加一条时间戳，超过一分钟的会被剪掉",
        "出站限流窗口（按收件人）。进程级；`/clear` 不清，避免清空后被瞬间刷满"),
    ProcessState(
        "_SKILL_DISABLED", "bus/registry.py::_SKILL_DISABLED",
        "`bus/skill_state` 写入（**整体替换**的 set，读到的要么是旧的要么是新的）",
        "**用户主动停用**的技能归属集合。与 `_QUARANTINED` / `_MCP_DISABLED` 分开："
        "三者的撤销方式与拒绝话术都不同。系统级，`/clear` 不清"),
    ProcessState(
        "_cache", "bus/mcp_disabled.py::_cache",
        "`bus/mcp_disabled._LOCK` 护着（同 `bus/quarantine._cache`）",
        "账本的内存镜像（`data/runtime/mcp_disabled.json`）。⚠️ 同上：**不是**真相源，"
        "真相是那个文件"),
    ProcessState(
        "_cache", "core/groups.py::_cache",
        "`core/groups._LOCK` 护着（同 `bus/mcp_disabled._cache`）",
        "群聊名册的内存镜像（`data/runtime/groups.json`）。⚠️ 同上：**不是**真相源，"
        "真相是那个文件。★ 这些是**用户建的群**——"
        "所以它**不挂 `/clear`**（群不是会话态，清一段对话不该把群解散了）"),
    ProcessState(
        "_cache", "core/group_digest.py::_cache",
        "`core/group_digest._LOCK` 护着（同 `core/groups._cache`）",
        "群聊「上一轮摘要」的内存镜像（`data/runtime/group_digests.json`）。"
        "⚠️ 它不是真相源；★ **键 = 存储 id** —— "
        "`/clear` 会铸新的存储 id，旧摘要天然失效，所以**不挂** `purge_session`"
        "（不删数据，只是没有指针）"),
    ProcessState(
        "_cache", "core/group_minutes.py::_cache",
        "`core/group_minutes._LOCK` 护着（同 `core/groups._cache`）",
        "群聊「会议纪要」的内存镜像（`data/runtime/group_minutes.json`）。"
        "⚠️ 它不是真相源；★ **键 = 存储 id** —— "
        "`/clear` 会铸新的存储 id，旧纪要天然失效，所以**不挂** `purge_session`"
        "（不删数据，只是没有指针）"),
    ProcessState(
        "_table_cache", "core/model_capabilities.py::_table_cache",
        "无锁（只读表 + 惰性缓存；并发首次读最多重复解析一次，结果相同）",
        "模型能力表的内存镜像（`providers/model_capabilities.json`）。"
        "⚠️ 它不是真相源；★ 键是 `(provider, model)`、"
        "**不是会话态** ⇒ **不挂** `purge_session`（`/clear` 不该清掉一张全局表）"),
    ProcessState(
        "_MIME_BY_EXT", "core/artifacts.py::_MIME_BY_EXT",
        "无锁（import 期一次建好，之后只读）",
        "产物底座：扩展名 → 回给浏览器的 mime。"
        "⚠️ 它由 `_EXT_BY_MIME` 反推、**不是真相源** ⇒ **不挂** `purge_session`"),
    ProcessState(
        "_rejected_ids", "core/artifacts.py::_rejected_ids",
        "无锁（只做诊断计数；上限 `_REJECTED_MAX`，输入是不可信的 id）",
        "端点拒过的**非法 id**（诊断用，最多 64 条）。"
        "★ 它是进程级观测、**不是会话态** ⇒ **不挂** `purge_session`；"
        "★ 有上限是判据本身（untrusted 输入不许把内存撑大）"),
    ProcessState(
        "_meta_cache", "core/model_capabilities.py::_meta_cache",
        "无锁（与 `_table_cache` 同一次读填好）",
        "能力表的 `source` / `fetched` 元数据（同上，全局只读，不挂 `/clear`）"),
    ProcessState(
        "_cache", "core/model_configs.py::_cache",
        "`core/model_configs._LOCK` 护着（同 `core/groups._cache`）",
        "模型「配置条目」账本的内存镜像（`data/runtime/model_configs.json`）。"
        "⚠️ 同上：**不是**真相源，真相是那个文件。★ 这些是"
        "**用户自己建的模型配置**——所以它**不挂 `/clear`**（配好的模型不是会话态，"
        "清一段对话不该把用户填的中转站地址也清掉）"),
    ProcessState(
        "_creds", "core/model_configs.py::_creds",
        "`core/model_configs._LOCK` 护着（与 `_cache` **同一把锁、同一次原子写**）",
        "★ 凭据账本的内存镜像（`data/runtime/provider_credentials.json`）——"
        "**单独一个文件**（配置可以随便看/导出，凭据不行）。"
        "⚠️ 它也是**不是**真相源。★ 与 `_cache` **必须同进同出**：两个文件各写一次，"
        "而哪一次失败都只出声、不回滚（本次运行内是真的）。"
        "⚠️ 那个文件同时在 `core/security.py::DEFAULT_FORBIDDEN_PATHS` 里 ——"
        "**改动它的落点时两处都要改**"),
    ProcessState(
        "_INSTANCES", "core/model_configs.py::_INSTANCES",
        "★ **无锁**：读写都发生在 `instance_for` 里，而那是**每轮**从"
        "`scheduler._resolve_turn_model` 调的 ⇒ 并发 3 时两个会话**可能同时**"
        "miss 同一条 cid（各建一个实例，后者覆盖前者）。**两条都可用**，"
        "代价只是多构造一次；而加锁会把一次 `import` 放在临界区里（那才是"
        "real contention）。判据：**并发的写是「同一个值的两种算例」，"
        "不是两次不同的事实**",
        "★ **建好的 provider 实例**"
        "（`{cid: (updated_at, 实例)}`，里面有 httpx 客户端）。"
        "⚠️ 与上面两格**不是一类东西**（那两格是账本的内存镜像，这一格是**派生出来的机器**）。"
        "★ 失效判据挂在 `updated_at` 上（不是「记得清」）：用户改了那条配置 ⇒ 下一轮重建。"
        "⚠️ `/clear` **不清它** —— 它是**进程级的缓存**，与哪一段对话无关"
        "（清了只是多几次 import）"),
    ProcessState(
        "_ST", "core/session_model.py::_ST",
        "★ **无锁**：只在 `_short_term()` 里写一次（首次拿到单例后就一直命中），"
        "而比赛的两方**写进去的是同一个对象**（`get_memory_manager()` 那个）"
        "⇒ 没有可以分叉的事实",
        "★ **惰性拿到的 `ShortTermMemory` 单例引用**"
        "（那个模块顶层 import 会把整条记忆链拉进每一条 import 路径）。"
        "⚠️ `/clear` **不清它** —— 它认的是**库**，不是会话。"
        "★ 但 `session_model.reset_for_tests()` 会清它：测试之间**会换 sandbox**"
        "（chdir 到新临时目录），不清就指着上一个用例那个库"
        "（症状：读得到、有表、里面是别人的行）"),
    ProcessState(
        "_ST", "core/session_workspace.py::_ST",
        "★ **无锁**：与 `core/session_model.py::_ST` **逐字同一形状**"
        "（只在 `_short_term()` 里写一次，而比赛的两方写进去的是**同一个对象**）",
        "★★ **惰性拿到的 `ShortTermMemory` 单例引用**"
        "—— 同一个理由（顶层 import 会把整条记忆链拉进每一条 import 路径）。"
        "⚠️ `/clear` **不清它**（它认的是**库**，不是会话）。"
        "★ `session_workspace.reset_for_tests()` 会清它（同 `session_model` 那条："
        "测试之间会换 sandbox）。"
        "⚠️ **它旁边的 `_bound` 不在这里** —— 那是 `ContextVar`，按定义不在"
        "扫描面上（每任务一份、自洁），见 `core/state_registry.py` 顶部那段"),
    ProcessState(
        "_last", "core/channel_health.py::_last",
        "★ 无锁读不了 —— 读写都在 `_lock` 里（`note()` 每轮一条、`snapshot()` 只在端点里）",
        "★★ **进程级的读数，不是会话态**：`platform → 最后一次进管线的时间`。"
        "`/clear` 不该碰它（清掉会让状态端点误报「这条通道从没来过」）；"
        "清它的只有 `clear()`（测试用）。"
        "⚠️ **它不是心跳** —— 健康但没人说话的通道也会「安静」，判据见模块 docstring"),
    ProcessState(
        "_declared", "core/tool_output_kind.py::_declared",
        "装载期写入（_note_output_kind），运行期只读", "同 registered_stages"),
    ProcessState(
        "_stats", "core/auto_review.py::_stats",
        "无锁；只在 `_auto_review_gate` 那条派发路上加一笔（六个通道各在自己的线程里跑）",
        "★ 它是**台账，不是判据**（`note_stat` 的 docstring 写着）⇒ 并发最坏是少记一笔，而**没有东西读它做决定**。清它的是 `clear_stats()`（测试用），**不是** `/clear` —— 它是进程级的史"),
    ProcessState(
        "_declared", "core/auto_review.py::_declared",
        "装载期写入（manifest 的 `review_input` + 框架侧那张表），运行期只读",
        "★ 与上面 `tool_output_kind._declared` **同形同命**（同一套声明机制"
        "的第三个字段）—— **不是会话态**，`/clear` 不该碰它，"
        "所以也不进 `purge_session` 那份清单"),
    ProcessState(
        "_declared", "core/tool_permission.py::_declared",
        "装载期写入（_note_permission），运行期只读",
        "★ 与 `tool_output_kind` 那格**同名不同模块** —— "
        "census 按 `文件::符号` 认，所以这是两格。同 registered_stages"),
    ProcessState(
        "_declared_destructive",
        "core/tool_permission.py::_declared_destructive",
        "装载期写入（_note_permission），运行期只读",
        "★ `destructive` 轴的声明表。与上一格"
        "同生同死 —— 同一处 loader 注册、同一对 snapshot/restore 管。同 registered_stages"),
    ProcessState(
        "_declared_concurrency",
        "core/tool_permission.py::_declared_concurrency",
        "装载期写入（`_note_concurrency_safe`），运行期只读",
        "★★ **第三根轴 `concurrency_safe` 的声明表**。"
        "三格同生同死（同一处 loader 注册、同一对 `snapshot`/`restore` 管）。"
        "★ 声明表按 `文件::符号` 认，同名不同模块各自成格；"
        "新增一格时 census 会当场要求登记"),
    ProcessState(
        "_update_state", "core/updater.py::_update_state",
        "更新流程内写（自替换前会重启进程）",
        "'进程内只允许一个更新'这条不变量今天没有执法者 —— 拆锁前想一遍"),
    # `global X; X = ...` 这种普通赋值不会被"按值认"的判据看见。
    #   见下一行的注释。
    ProcessState(
        "_UPDATING", "core/updater.py::_UPDATING",
        "**无锁**：`UpdateManifest.install()` 里置真、失败/结束时置假；"
        "健康检查守护进程读它",
        "★ 它才是那条不变量的**执法者候选**（`_update_state` 那格的说明里写着"
        "「今天没有执法者」）—— 两格是同一件事的**数据面与标志面**。"
        "⚠️ 拆锁时一起看：今天靠的是'自替换前会 os._exit'这个物理事实"),
    ProcessState(
        "_env_warned", "bus/core.py::_env_warned",
        "warn-once 集合；并发下最多多打一行", "无害"),
    ProcessState(
        "_startup_stats", "bus/core.py::_startup_stats",
        "启动期写入、其后只读", "无害"),
    ProcessState(
        "_bus", "core/events.py::_bus",
        "单例本身；创建有 _bus_lock", "取到之后是只读引用"),
    ProcessState(
        "_subs", "core/events.py::EventBus._subs",
        "自带 _lock（:386）", "SSE 订阅表，已按流隔离"),
    ProcessState(
        "_project_root", "core/prompt_assembler.py::_project_root",
        "惰性算一次，其后只读", "无害"),
    # 这两格不是会话级（没有 session_id 键，也不该随 `/clear`
    #   清），所以归这边；census 会要求登记它们。
    ProcessState(
        "_loaded", "core/leaf_md.py::_loaded",
        "每次 `leaf_md.load()` 重写（就是装配系统提示词那条路）",
        "★ **进程级快照**：给日志 / 设置页读「这次读了哪几个指令文件、多少字符」。"
        "天然并发写（多条会话同时装配）⇒ 最坏读到**上一次**的清单；"
        "它**不参与任何判定** ⇒ 无害（与 `_startup_stats` 同族）"),
    ProcessState(
        "_warned", "core/leaf_md.py::_warned",
        "**无锁 warn-once 闩**（超 40000 字符的软告警只出声一次）",
        "★ 与 `_env_warned` 同族：并发下最多**多打一行**，没有别的后果"),
    ProcessState(
        "_bash_cache", "core/shell.py::_bash_cache",
        "首次探测后钉死，其后只读", "无害"),
    ProcessState(
        "_ps_cache", "core/shell.py::_ps_cache",
        "首次探测后钉死，其后只读", "无害（与 `_bash_cache` 同一套写序判据）"),
    ProcessState(
        "_ps_probed", "core/shell.py::_ps_probed",
        "**无锁闩**；最后一句写（同 `_bash_probed`）",
        "无害 —— 写序判据同下面那条，只是对象换成了 PowerShell 探测"),
    ProcessState(
        "_bash_probed", "core/shell.py::_bash_probed",
        "**无锁闩**；★ 它是**最后一句写**（见 `bash_path` 的 docstring）",
        "★ **写序就是它的护栏**：若闩先落、缓存后填，另一条会话在那个窗口里"
        "会读到「闩真 + 缓存空」⇒ 拿到空 bash 路径（一次性的'找不到 bash'）。"
        "⇒ 登记它的理由不只是'有人盯着'，是**它俩的次序有判据**"),
    ProcessState(
        "_stages", "core/pipeline/scheduler.py::PipelineScheduler._stages",
        "装载期定形（initialize() 一次）", "运行期只读"),
    # IM 选址的五个格（渠道级，不是会话级）
    # 它们随进程生灭、与 `/clear` 无关；登记在这里的主要理由是有人盯着：
    # 加第六个格会被 census 当场点名。
    # 第五格 `_loaded` 与上面四个写在同一个 `_lock` 下
    #   （`global` 赋值同样是"会被写"的一种，census 会一起管住）。
    ProcessState(
        "_loaded", "core/im_routing.py::_loaded",
        "自带 `_lock`（与上面四格同一把）；一次性的**惰性装载闩**",
        "★ 它是「routing.json 只读一次」的实现，不是会话态 ⇒ `/clear` 不清。"
        "⚠️ 它**必须在 `_lock` 里**：否则两个渠道同时首调会各读一次盘"),
    ProcessState(
        "_bindings", "core/im_routing.py::_bindings",
        "自带 `_lock`；只在路由的读写点进锁（快）",
        "渠道 → 默认会话（**持久**，data/runtime/routing.json）。`/c#` 刚问完与 "
        "`/change` 写它；**Web 输入不许动它**"),
    ProcessState(
        "_pending", "core/im_routing.py::_pending",
        "自带 `_lock`",
        "每渠道**挂着的一次询问**（编号表 + **扣住的事件**）。Web 输入即作废"),
    ProcessState(
        "_numbering", "core/im_routing.py::_numbering",
        "自带 `_lock`",
        "每渠道**最后展示过的**编号表（展示过就不再重排）"),
    ProcessState(
        "_temp", "core/im_routing.py::_temp",
        "自带 `_lock`",
        "每渠道的**临时目标**（不写盘，只活到进程结束）"),
    ProcessState(
        "_session_locks", "core/pipeline/scheduler.py::_session_locks",
        "自带 `_session_locks_guard`",
        "每会话一把锁的表；**不许在 /clear 时丢锁**（purge 在持锁轮内跑）"),
    ProcessState(
        "_last_activity", "core/pipeline/scheduler.py::PipelineScheduler._last_activity",
        "每轮在自身任务里写（字典单键写入原子）；IM 选址读者只遍历",
        "活跃表（单调钟）。**只在内存** —— 重启后 0 个活跃 ⇒ 走默认键"),
    ProcessState(
        "_bg_status", "core/memory/manager.py::MemoryManager._bg_status",
        "**无锁**：后台笔记线程 append、轮末取走",
        "拆锁时要回答：两个会话的后台线程同时 append ⇒ 要么加锁、要么改成每会话一格"),
    ProcessState(
        "_table_cache", "core/window_estimate.py::_table_cache",
        "自带 `_table_lock`；惰性读一次静态表，其后只读",
        "★ 窗口表（`providers/window_table.json`）的**派生缓存** —— 不是真相源，"
        "坏了重读一次即可（同 skill_doctor 的 `_last_seen`：最坏是重复读一回文件）"),
    ProcessState(
        "_observed", "core/window_estimate.py::_observed",
        "自带 `_observed_lock`（每个 provider 的响应流里写、装载期读）",
        "★ 窗口解析的「实际」层：`{model: 已成功发出过的最大 prompt_tokens}`（**只存下界**）。"
        "并发写最坏是丢一次「更大」的更新（下次响应会再报）⇒ 无锁也能活，但加了锁更省心"),
    ProcessState(
        "_state", "core/memory_organize.py::_state",
        "**无锁**（单键写原子）：`last_scan_at` 是标量、`in_progress` 是布尔",
        "★ 整理的进程级两格 —— 它**不是**会话级：整理整个进程里"
        "至多跑一个（跨会话共享同一把锁文件）。`last_scan_at` 并发丢一次写的最坏"
        "后果是**多扫一遍会话目录**（10 分钟节流提前失效），可接受"),
    ProcessState(
        "_warned", "core/settings_baseline.py::_warned",
        "warn-once 标志；并发下最多多打一行",
        "★ 出厂基准读不到/坏了时**只出声一次**的闩。"
        "与 `bus/core.py::_env_warned` **同形同命**（warn-once 集合 ⇒ 无害）。"
        "⚠️ `/clear` 不清它 —— 那是**进程级的史**，不是会话级的状态"),
    # 这两格不是会话级（没有 session_id 键，也不该随 `/clear` 清），
    #   所以归这边；census 会要求登记它们。
    ProcessState(
        "_shell32", "core/recycle.py::_shell32",
        "**无锁**：只在 `_lib()` 里惰性装载一次（`_shell32 = lib`），其后只读引用。"
        "⚠️ 首调那一瞬两条会话同时进来**最坏是各装一次**（DLL 句柄，系统里同一份）"
        "⇒ 无害；真要收紧就补一把 `_lib_lock`（同 `window_estimate._table_cache` 那种形状）",
        "★ 判据 ② 命中（`global _shell32`）：**声明 global 就是在说'我会从函数里改它'**。"
        "`/clear` **不清** —— 它是进程级的 DLL 句柄，清了只会白装一遍"),
    ProcessState(
        "_load_error", "core/recycle.py::_load_error",
        "**无锁 warn-once 字符串**：`_lib()` 失败时写一次（同一句 `global`）",
        "★ 与 `bus/core.py::_env_warned` **同形同命**（并发下最多重算一次字符串 ⇒ 无害）。"
        "它是给回执用的一句话（'为什么这台机器上用不了回收站'），"
        "⚠️ 不许当成判据 —— 判据是 `available()`。`/clear` 不清它"),
    # 这两格不是会话级：没有 session_id 键，而且 `/clear` 不该碰它们 ——
    #   碰了就把挂在那个循环上的 MCP 连接一起掐了。所以归这边。
    ProcessState(
        "_loop", "core/async_loop.py::_loop",
        "自带 `_lock`。★ **起循环那一段必须原子**：`get_loop()` 会被启动期与"
        "任意工具调用**同时**调到，不加锁就会起来**两个**循环 —— "
        "而 MCP 的 session 只活在其中一个上面（那个 bug 的形状是「有时能用」）",
        "★ **进程级**：`/clear` 不清它（清了等于把 MCP 连接一起掐了）。"
        "它是「活得比一次调用长的异步资源」那个家 —— 见模块 docstring"),
    ProcessState(
        "_thread", "core/async_loop.py::_thread",
        "与 `_loop` **同一把锁**，而且**必须一起换**：只换一半的状态是"
        "『循环在、线程不在』，而 `get_loop()` 会因此判成'需要重启' "
        "⇒ 又起来一个（`get_loop` 的存活判据把两者一起看，是有意的）",
        "★ 常驻线程的句柄（daemon，随进程退）。写它的只有 `get_loop()` 与 "
        "`shutdown()`，两者都持锁。⚠️ `shutdown()` **生产路径零调用点**是有意的"
        "（它给测试收尾用）"),
    # `bus/` 也在 census 的扫描面（模块 docstring 写着扫 `core/` + `bus/`）。
    ProcessState(
        "_map_cache", "bus/loaders/skill_md.py::_map_cache",
        "**无锁**：键是 `(root, reserved)`，值是 `({命令: skill}, [被拒的])`。"
        "并发写最坏是**同一个键算两遍**（纯函数、结果相同）⇒ 无害",
        "★ **进程级的命令缓存**：扫一遍 `skills/` 要读**每个** `SKILL.md`"
        "（真实 skill 的索引约 36 KB），而 `_is_framework_command` **每条消息都走** ⇒"
        "不缓存是不可接受的。"
        "⚠️ **`/clear` 不清它** —— 清了不是错，只是白扫一遍"
        "（与 `skill_doctor._sibling_cache` 同族）。"
        "★★ **失效点只有一处**：`skill_loader.reload_skills` —— 判据是那条纪律"
        "「**生效要人按一下**」；不失效的话「放进去就能用」就是**一句假话**"),
    # 外部命令钩子（`core/hooks.py`）
    ProcessState(
        "_cache_key", "core/hooks.py::_cache_key",
        "`_load()` 在配置文件的 (路径, mtime, 大小) 变化时整体替换",
        "★ 进程级：一份配置签名，与哪条会话无关。⚠️ `/clear` 不清它"
        "（清了只是下次重新读一遍配置）"),
    ProcessState(
        "_cache_hooks", "core/hooks.py::_cache_hooks",
        "与 `_cache_key` **同一次**整体替换（键变才重读）",
        "★ 解析好的钩子列表（进程级）。读侧只读；解析结果不按会话区分"),
    ProcessState(
        "_started", "core/hooks.py::_started",
        "`session_start()` 第一次见到某个存储 id 时加进去（每会话一次）",
        "★ 进程级：只用来保证 `SessionStart` 一个存储 id 只跑一次。"
        "`/clear` 换新存储 id ⇒ 天然再来一次，所以不需要清理"),
    ProcessState(
        "_source_hint", "core/hooks.py::_source_hint",
        "`note_session_source()` 写、`take_session_source()` 取走（一次性）",
        "★ 进程级：`/clear` 铸新 id 时留下来源 `clear`，下一次会话开始取走"),
    ProcessState(
        "_warned", "core/hooks.py::_warned",
        "`_warn_once()` 往里加键（同一句只喊一次）",
        "★ 进程级的噪声抑制账本；不按会话键、`/clear` 不清"),
    # shell 快照（`core/shell.py`）
    ProcessState(
        "_snapshot_cache", "core/shell.py::_snapshot_cache",
        "`ensure_snapshot()` 在**第一次执行命令**时写一次（每进程一次）",
        "★ 进程级：一份快照文件的路径，与哪条会话无关。"
        "⚠️ `/clear` 不清它 —— 清了只是下次重新生成一份"),
    ProcessState(
        "_snapshot_done", "core/shell.py::_snapshot_done",
        "`ensure_snapshot()` 首次调用时置真（`global`）",
        "★ 一个模块级幂等旗：保证「只试一次」，失败也不反复尝试"),
    ProcessState(
        "_snapshot_file", "core/shell.py::_snapshot_file",
        "`ensure_snapshot()` 成功时写一次（生成那一刻的绝对路径）",
        "★ 收尾删除用它、不用再算相对路径 —— 进程工作目录变过之后，"
        "重算会删到另一个目录里的同名文件"),
    # 代码符号查询（`core/code_intel.py`）
    ProcessState(
        "_clients", "core/code_intel.py::_clients",
        "`_clients_lock` 护着建表；客户端自己的请求/响应各有一把锁。"
        "键是 **(服务器名, 工作目录)**，不是会话 id",
        "★ **进程级**：一个语言服务器进程可以被多段对话共用（它只认工作目录）。"
        "⚠️ `/clear` **不清它** —— 清了等于把服务器进程一起丢掉、下一轮重新握手"
        "（功能不变，只是白付一次启动）。真正的复位口是 `shutdown_all()`"),
    ProcessState(
        "_override", "core/code_intel.py::_override",
        "只在 `configure()` / `reset()` 里整体替换（测试与嵌入方用）",
        "★ 进程级注入位；**不是**会话态。生产路径从不给它赋值"),
    ProcessState(
        "_present_at", "core/code_intel.py::_present_at",
        "`config_present()` 每 2 秒重算一次时写（`time.monotonic()`）",
        "★ 一个节流时间戳：它决定工具进不进 `tools=`，而那条路**每请求都走**"),
    ProcessState(
        "_present_cache", "core/code_intel.py::_present_cache",
        "与 `_present_at` **同一次**写；`configure()` / `reset()` 清掉",
        "★ 一个模块级布尔缓存（'这台机器有没有可用的语言服务器'）。"
        "⚠️ 它按 2 秒 TTL 重算，所以加/删配置**不需要重启**"),
    ProcessState(
        "_catalog_cache", "core/code_intel.py::_catalog_cache",
        "`_detected()` 按 (工作目录) 缓存 60 秒；`reset()` 清空",
        "★ 目录探测结果：本机装好了哪些服务器。⚠️ 只探测、不安装；"
        "`/clear` 不清（与哪段对话无关）"),
    ProcessState(
        "_plugin_cache", "core/code_intel.py::_plugin_cache",
        "`_plugin_specs()` 按 `skills/*/lsp.json` 的文件签名整体替换",
        "★ 技能声明那份配置的签名（进程级）；变了才重读"),
    ProcessState(
        "_plugin_specs_cache", "core/code_intel.py::_plugin_specs_cache",
        "与 `_plugin_cache` **同一次**整体替换",
        "★ 解析好的技能服务器表；只读"),
    ProcessState(
        "_legacy_warned", "core/code_intel.py::_legacy_warned",
        "第一次读到旧格式配置时置真（`global`）",
        "★ 噪声抑制：旧格式只提示一次"),
    ProcessState(
        "_warned", "core/code_intel.py::_warned",
        "工作区 `lsp.json` 被信任门挡下时往里加路径（同一条只喊一次）",
        "★ 与 `core/hooks.py::_warned` **同形同命**：进程级噪声抑制账本，"
        "不按会话键、`/clear` 不清"),
    ProcessState(
        "_default", "core/dedup.py::_default",
        "`default()` 第一次调用时建那个去重账本实例（之后一直是同一个）",
        "★ **必须是同一份**：`pre_process` 占位、`post_process` 落账、"
        "调度器轮末放位 —— 分成两份就是「占位写 A、落账写 B」，两边都看不出错，"
        "而平台重投照样被挡。进程级，`/clear` 不清（`core/dedup.clear()` 的注释）"),
)


def purge_session(session_id: str) -> Dict[str, str]:
    """清掉 *session_id* 的全部模块级会话态。返回 {name: outcome}。

    这是 `/clear` 内存清理的唯一入口（`MemoryManager.new_session` 调用）。

    失败不许静默：某一条清不掉时它写进结果、并打一行 ——
    "清失败"与"本来就没东西清"必须看起来不一样（bare `except: pass`
    会让这两件事长得一模一样）。
    """
    outcome: Dict[str, str] = {}
    for entry in SESSION_SCOPED:
        if entry.purge is None:
            continue
        try:
            entry.purge(session_id)
            outcome[entry.name] = "done"
        except Exception as exc:  # noqa: BLE001
            outcome[entry.name] = "error:%s" % type(exc).__name__
            print("[state] purge %s failed for %r: %s: %s"
                  % (entry.name, session_id, type(exc).__name__, exc))
    return outcome


def all_entries() -> Tuple[SessionState, ...]:
    """会话级两条表的合并视图（读者与工具用；顺序即上表的书写顺序）。"""
    return SESSION_SCOPED + SESSION_SCOPED_BY_OTHER
