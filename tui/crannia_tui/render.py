"""scrollback 渲染：把已经定稿的事件变成一次性输出。

一条规则贯穿全部：**只打印不会再变的东西。**

工具调用在 `start` 时还不知道结果，所以按住不表，等 `end` 到了再整块输出。
理由不是"代码简单"，而是 scrollback 的语义：写出去的行改不回来。
一旦某一行进入终端的历史区，它就归终端管了 —— 那里能翻、能搜、能选中复制，
但也意味着我们不能再动它。所以"什么时候允许输出"和"什么时候内容定型"
必须是同一个时刻。

"正在跑"的状态留在 Live 区（后续阶段），不属于这里。
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text

from . import events as E
from . import outcome as O
from .i18n import t

#: 工具行的两个符号。第一个是"一个动作"，第二个是"这个动作的产出一行"。
#: 用符号而不是"- " 之类的文本前缀，是为了在长转写里能一眼分出层级。
TOOL_MARK = "⏺"
RESULT_MARK = "⎿"


def fmt_chars(n: int) -> str:
    """1000 → '1,000'；12000 → '12,000'。

    千分位不是装饰：工具回执动辄五六位数，不带分隔符时读不出量级。
    """
    return f"{n:,}"


def fmt_ms(ms: int) -> str:
    """毫秒按量级换单位。950 显示 '950ms'，3200 显示 '3.2s'。

    不统一成秒：几百毫秒的工具很多，写成 '0.4s' 反而要多读一位小数。
    """
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


class ScrollbackRenderer:
    """吃事件，往 console 打定稿内容。

    有状态，但状态只有一处：尚未配对的工具调起事件。
    除此之外它不记任何东西 —— 会话历史在 crannia 那边，
    这里记一份的话两边迟早不一致。
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        #: call_id → TOOL_START 事件。等 TOOL_END 来配对后删掉。
        #: 用 dict 而不是 list：工具可以并发调起，顺序不定。
        self._pending: dict[str, E.Event] = {}
        #: 最近一次 agent 说话的原文。用来挡住 task.end 里的重复兜底。
        self._last_say = ""
        #: 本轮是否已经输出过 agent 内容（决定 task.end 要不要兜底）
        self._said_this_turn = False

    # -- 对外 -----------------------------------------------------

    def feed(self, ev: E.Event) -> bool:
        """处理一条事件。返回 True 表示往终端写了东西。

        返回值不是给用户的，是给测试和 Live 区用的：
        "这一条有没有产生输出"决定了要不要清屏重画。
        """
        if ev.type == E.TOOL_START:
            self._pending[ev.s("call_id")] = ev
            return False
        if ev.type == E.TOOL_END:
            return self._tool_end(ev)
        if ev.type == E.SAY:
            return self._say(ev)
        if ev.type == E.ERROR:
            return self._error(ev)
        if ev.type == E.TURN_START:
            self._said_this_turn = False
            # 跨轮也要清：上一轮说的最后一句如果和新一轮的第一句恰好相同
            # （"好的"这种），不清就会把新一轮的话吞掉。
            self._last_say = ""
            return False
        if ev.type == E.TASK_END:
            return self._task_end(ev)
        return False

    def flush_unclosed(self) -> None:
        """把没等到 END 的工具调起打成一行"未完成"。

        什么时候会缺 END：后端在这一轮中途崩了、或者我们在工具跑到一半时
        才连上事件流。两条路都真实存在，所以不能静默丢掉 ——
        用户会以为那个工具压根没跑过。
        """
        for call_id, start in self._pending.items():
            name = start.s("name", "?")
            self.console.print(
                Text.assemble((f"{TOOL_MARK} ", "dim"), (name, "bold"),
                              ("  " + t("（没有收到结束事件）"), "yellow"))
            )
        self._pending.clear()

    # -- 各类型 ---------------------------------------------------

    def _tool_end(self, ev: E.Event) -> bool:
        call_id = ev.s("call_id")
        start = self._pending.pop(call_id, None)
        name = ev.s("name") or (start.s("name") if start else "") or "?"
        # activity 在 START 那边（end 事件里没有这个字段）——
        # 所以必须配对，不能只靠 end 自己画。
        activity = start.s("activity") if start else ""
        ok = ev.b("ok", True)
        outcome = ev.s("outcome") or ("completed" if ok else "error")
        reason = ev.s("reason")
        ms = ev.i("duration_ms")
        chars = ev.i("result_chars")
        err = ev.s("error")

        head = Text()
        head.append(f"{TOOL_MARK} ", style="dim")
        head.append(name, style="bold")
        if activity:
            head.append("  ")
            head.append(activity, style="")

        detail = Text()
        detail.append(f"  {RESULT_MARK}  ", style="dim")
        if outcome == "completed":
            parts = []
            if chars:
                parts.append(t("%s 字符") % fmt_chars(chars))
            if ms:
                parts.append(fmt_ms(ms))
            detail.append(" · ".join(parts) if parts else t("完成"), style="dim")
        elif outcome != "error":
            # 等审批 / 被拦 / 被拒：没有"体积 · 耗时"可报 —— 那几格里
            # 最关键的是"它到底跑没跑"，一句话说清就够。
            detail.append(O.label(outcome, reason), style="dim")
        elif not ok and err.strip():
            # 错误只给一行：完整 traceback 在上游日志里，
            # 终端里铺开会把上下文冲掉。
            detail.append("✗ ", style="red")
            detail.append(err.splitlines()[0][:160], style="red")
        else:
            # 框架拒绝（工具不存在 / 计划模式 / 白名单 …）：没有错误正文，
            # 只有结局本身。
            detail.append(O.label(outcome, reason), style="dim")

        # 卡片之间留一行。不带它时，连续几个工具会糊成一片 ——
        # 而"一轮里连着跑七八个工具"正是最常见的形态。
        self.console.print(Text(""))
        self.console.print(Group(head, detail))
        return True

    def said(self, text: str) -> bool:
        """打一段 agent 说的话，同一段只打一次。

        两个入口共用它：事件流里的 `task.end.response`，和 `--once` 那条
        HTTP 返回值的兜底。它们会同时到达，而且谁先到并不固定 —— 实际跑下来
        HTTP 常常先返回、事件流后到。各自判各自的标记，就必然打两遍；
        共用一份"上一次说过什么"，第二遍才挡得住。

        判据是"这段文本和上一段一样"，不是"说过话没有"：一轮里 agent 可能
        连说几段，只有末尾那一小段会和 `response` 重复。
        """
        text = (text or "").strip()
        if not text or text == self._last_say:
            return False
        self._last_say = text
        self._said_this_turn = True
        self.console.print(Text(""))
        self.console.print(Markdown(text))
        return True

    def _say(self, ev: E.Event) -> bool:
        return self.said(ev.s("text"))

    def _error(self, ev: E.Event) -> bool:
        msg = ev.s("message") or ev.s("error") or t("未知错误")
        stage = ev.s("stage")
        first = msg.splitlines()[0]
        # 截断要留痕：切在半个 JSON 上的 "…param": None, 'co" 会被当成乱码，
        # 而带省略号的短句一眼就能看出"这里还有、被我省了"。
        if len(first) > 140:
            first = first[:140] + "…"
        line = Text()
        line.append("✗ ", style="red")
        if stage:
            line.append(f"[{stage}] ", style="dim")
        line.append(first, style="red")
        self.console.print(line)
        return True

    def _task_end(self, ev: E.Event) -> bool:
        """一轮结束。

        正常路径下 agent 的话已经由 `say` 输出过了，这里什么都不做。
        但有些路径只把话放在 `response` 里、不发 `say` —— 那种情况下不补打，
        用户就只能看到一行红字，看不到后端给的说明。所以补一次，走
        `said()` 的去重，避免和 `say` 或 HTTP 返回值撞成三遍。
        """
        if self._said_this_turn:
            return False
        return self.said(ev.s("response"))
