"""全屏布局用的几个小部件。

为什么拆出来而不是全塞进 `app.py`：这块是"长什么样"，而 `app.py` 是
"什么时候改哪一块"。混在一起的话，调间距会顺手改到事件分流，
而后者一改错就是"消息丢了"或"打两遍"，很难从界面上看出来。

尺寸与配色一律写在 `app.tcss`，这里不写 `DEFAULT_CSS`。两个地方都能写宽高时，
外部样式表赢，而改的人往往只改眼前那一份 —— 界面上不报错，只是"改了没反应"。
"""

from __future__ import annotations

import random
import re
import time

from rich.console import Console, Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static, TextArea

from . import approval as A
from . import outcome as O
from .i18n import t
from .render import fmt_chars, fmt_ms

#: 标志。四行高，用块字符拼 —— 在位图终端里也能显示，
#: 且不会像 ASCII art 那样一改终端宽度就散架。
#:
#: 两条横线比竖线右缩一格，这是让它读成 C 而不是 "[" 的关键：
#: 不缩的话三笔在最左侧连成一条直线，左上左下两个角是实的，看上去就是方括号。
#:
#: 竖线用整块 `█`，不用右半块 `▐`：终端字符格子高约是宽的两倍，
#: 半行高的横线与整格宽的竖线正好等粗；右半块的竖线只有横线一半粗，
#: 夹在一上一下两条横线之间，看起来像萎缩了。
#:
#: 竖线占两整行、横线各占半行，笔画长度比是 4:1 —— 竖线太短的话，
#: 读起来像"⊂"而不像 C；四行是终端里最接近的取法。
LOGO = " ▄▄▄▄\n█\n█\n ▀▀▀▀"

#: 抬头分隔线：那一串字符。
#: 它属于设计内容，不是主题色，所以颜色写在这里而不是 app.tcss。
_SEPARATOR_TEXT = ("#- < . . . > / * { _ } - | #> = \\ @ ( ) ) - "
                   "< *** / [ _ ^ _ \\ - < \\ - > ; | · · · =")

#: 分隔线调色板：每个字形轮换一个颜色，整条同色会读成普通装饰线。
#: 设计意图是"创造性与生命力"，所以取的是高饱和的一组，不是主题色。
_SEPARATOR_PALETTE: tuple[str, ...] = (
    "#4caf50",  # 绿
    "#8bc34a",  # 浅绿
    "#cddc39",  # 黄绿
    "#ffeb3b",  # 黄
    "#ffc107",  # 琥珀
    "#ff9800",  # 橙
    "#ff5722",  # 深橙
    "#f44336",  # 红
    "#e91e63",  # 粉
    "#9c27b0",  # 紫
    "#673ab7",  # 深紫
    "#3f51b5",  # 靛
    "#2196f3",  # 蓝
    "#03a9f4",  # 浅蓝
    "#00bcd4",  # 青
    "#009688",  # 蓝绿
)


def _separator(width: int) -> Text:
    """按可用宽度截断的彩色分隔线。

    每个非空白字形换一种颜色，按调色板循环；空白不占色位，
    免得空隙把色带切得参差不齐。装饰内容，截断不补省略号。
    """
    out = Text()
    slot = 0
    for ch in _SEPARATOR_TEXT:
        if ch == " ":
            out.append(ch)
            continue
        out.append(ch, style=_SEPARATOR_PALETTE[slot % len(_SEPARATOR_PALETTE)])
        slot += 1
    return out[:width] if width < len(out) else out


def _center_block(text: str, width: int) -> Text:
    """把定宽多行块整体居中：每行加同一段左空白，保持块内相对位置。

    逐行居中会把这个标志拆散 —— 每行长度不同，居中后左边缘就不齐了。
    样式表的 align-horizontal 对定宽子部件不生效（实测），所以在这里算。
    """
    rows = text.split("\n")
    block = max(len(r) for r in rows)
    pad = max(0, (width - block) // 2)
    return Text("\n".join(" " * pad + r for r in rows))


class _Logo(Static):
    """标志。在 `render()` 里按自己的宽度居中。

    不预先算好再 `update()`：抬头在启动时打印一次，而那一次往往发生在
    尺寸定下来之前；窗口之后被拖大时，预填的空白会停在旧宽度上，
    标志看起来就是歪的（文字行由样式表居中，会自己跟上，标志不会）。
    `render()` 每次重画都读当时的宽度，不需要额外的 resize 钩子。
    """

    def render(self) -> Text:
        return _center_block(LOGO, self.size.width or 80)


class HeaderBar(Widget):
    """启动抬头：居中的标志，下面是几行信息。

    它不是固定区：作为对话区的第一块打印一次，随后被内容顶走。
    这些行只在换模型/档位/目录时变，不值得占一条永久行。

    标志四行 + 文字最多四行（标题 / 模型与档位 / 分隔线 / 目录），
    尺寸与文字居中在 `app.tcss` 里。
    """

    def compose(self) -> ComposeResult:
        yield _Logo(id="logo")
        yield Static("", id="info")

    def on_mount(self) -> None:
        self.show(title="Crannia", version="", model="", effort="", cwd="")

    def show(self, *, title: str, version: str, model: str,
             effort: str = "", cwd: str) -> None:
        """刷新文字行。取不到的字段整行不出，不留空行占位。"""
        width = self.size.width or self.app.size.width or 80
        lines = Text()
        lines.justify = "center"
        head = title or "Crannia"
        lines.append(head, style="bold")
        if version:
            lines.append(f" v{version}", style="dim")
        if model or effort:
            lines.append("\n")
            lines.append(model)
            if model and effort:
                lines.append(" | ", style="dim")
            if effort:
                lines.append(effort, style="dim")
        sep = _separator(width)
        if sep.plain:
            lines.append("\n")
            lines.append(sep)
        if cwd:
            lines.append("\n")
            lines.append(">_ ", style="#4caf50")
            lines.append(cwd, style="dim")
        self.query_one("#info", Static).update(lines)


#: 明确是"网址"的 scheme。这里与 GUI 的 `openpath.js` 用同一张表 —— 两边
#: 分类不一致时，同一条链接在桌面端和终端里会去两个不同的地方。
_URL_SCHEME = re.compile(r"^(https?|mailto|tel|data|ftp):", re.I)


def local_path_of(href: str) -> str | None:
    """这个链接指向本机文件就返回路径，是网址就返回 None。

    `file:///C:/x` 剥成 `C:/x`（模型偶尔这么写，指的就是那个文件）。UNC
    （`file://server/share`）不认 —— 剥完会变成一段没有根的相对路径，
    交给后端只会得到"这个路径不存在"，不如照网址处理。
    """
    h = (href or "").strip()
    if not h:
        return None
    if h.lower().startswith("file:"):
        rest = h[5:].lstrip("/")
        return rest if re.match(r"^[A-Za-z]:", rest) else None
    if h.startswith("//") or _URL_SCHEME.match(h):
        return None
    return h


#: 代码围栏的开头与结尾。
_FENCE = re.compile(r"^\s*```")


def harden_soft_breaks(text: str) -> str:
    """行尾补两个空格，把单换行定成 Markdown 硬换行。

    Markdown 默认把单换行折成空格 —— "一行一项"的命令回执会被挤成一整行
    （`/effort` 那段就是这么糊掉的）。围栏里的不动（那是代码，改了就是改
    内容），表格行不动（换行在那儿是行分隔符，不是折行点）。
    """
    lines = text.splitlines()
    out: list[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        folded = (not in_fence
                  and bool(line.strip()) and bool(nxt.strip())
                  and not line.lstrip().startswith("|")
                  and not nxt.lstrip().startswith("|"))
        out.append(line + "  " if folded else line)
    return "\n".join(out)


class AgentMarkdown(Markdown):
    """回复正文。正文里的本机路径点一下就有去处。

    Textual 的 `Markdown` 给每个链接挂了 `@click=link(href)`，默认收尾是
    `app.open_url` —— 把 href 交给系统。这里分流：网址照旧交给系统；
    本机路径改走后端 `/api/reveal`，那条路只定位、不执行。
    判据是"回复是模型写的文本"：交给系统打开，它写一个 `.bat` 的路径
    就变成用户一点即运行。

    `open_links=False` 不能省：基类那次"交给系统"是**类级事件处理器**，
    覆写 `on_markdown_link_clicked` 只多跑一份、不顶掉它 —— 一次点击会
    两个都发生（文件被默认程序打开，资源管理器也跳出来）。关掉它，
    网址那半由下面显式补上。
    """

    def __init__(self, markdown: str = "", **kwargs) -> None:
        super().__init__(markdown, open_links=False, **kwargs)

    def on_markdown_link_clicked(self, event) -> None:
        path = local_path_of(event.href)
        if path is None:
            self.app.open_url(event.href)               # 网址：交给系统
            return
        self.app.reveal(path)


class Transcript(Vertical):
    """对话区：从上往下追加，超过一屏就滚动。

    每块东西 mount 上去就不再改 —— 全屏给了"任意位置都能重绘"的能力，
    但这里刻意不用：一个块改两次，阅读时就会怀疑自己刚才看错了。
    需要原地变化的只有底部那一行状态，不在这里。

    贴底不用 Textual 的 `anchor()`：内容不满一屏时它会把整块压到底部
    （合成器算 `virtual - container`，负数不夹取），抬头会沉到输入框上方。
    这里用 `_stuck` 自己记：新内容或被挤矮时贴底，用户上翻就松开、
    翻回底部自动贴上（`watch_scroll_y` 是唯一判据）。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: 用户是不是贴着底部。新内容只在这个为真时把视野拽下去。
        self._stuck = True

    def compose(self) -> ComposeResult:
        # 抬头是这一区的第一块，跟着内容一起滚走（见 `HeaderBar` 的 docstring）。
        yield HeaderBar()

    def watch_scroll_y(self, old: float, new: float) -> None:
        """用户滚到底就重新贴上，滚上去就松开。

        **必须先调 `super()`**：父类那次 watcher 才负责同步滚动条位置与
        触发重绘。只更新自己的标记、不往下传，滚轮会变成"值在动、画面不动"
        —— 看起来就是卡死（这个坑踩过一次）。
        """
        super().watch_scroll_y(old, new)
        self._stuck = self.scroll_y >= self.max_scroll_y - 1

    def _stick(self) -> None:
        """贴底。`scroll_end` 会等一次刷新再滚，用的是届时的高度 ——
        刚 mount 的块还没参与布局，现在算出来的底是旧底。"""
        if self._stuck:
            self.scroll_end(animate=False)

    def on_resize(self, _event) -> None:
        """被挤矮/长高（审批面板挂载、窗口缩放）：贴着底就继续贴着，
        否则"正卡着的那一步"会被挤出视野。"""
        self._stick()

    def clear_content(self) -> None:
        """清掉会话内容，保留抬头。

        抬头虽然是本区第一块，但它不是会话内容：只在模型/目录变化时刷新。
        连同内容一起删掉，之后的状态刷新就查不到部件，而且新会话里也该继续显示。
        """
        for child in list(self.children):
            if not isinstance(child, HeaderBar):
                child.remove()
        self._stuck = True

    def add(self, renderable, *, classes: str = "") -> Static:
        """追加一块，返回它。滚不滚到底由 `_stuck` 决定：用户往上翻了就不拽。

        返回部件是给"插话那句话"用的：它先黄着，等 agent 读了要改回正常色。
        """
        block = Static(renderable, classes=classes)
        self.mount(block)
        self._stick()
        return block

    def add_agent_text(self, text: str, *, classes: str = "agent") -> None:
        """贴一段回复。单换行先定成硬换行（见 `harden_soft_breaks`）——
        否则 Markdown 会把它们折成空格，一行一项的表就成了一段话。"""
        self.mount(AgentMarkdown(harden_soft_breaks(text), classes=classes))
        self._stick()

    def add_card(self, card: "ToolCard") -> None:
        """追加一张工具卡。它之后还会被改，所以这里不套 Static。

        相邻的卡片贴紧 —— 它们是一条动作链（"先查、再读、再改"），
        中间留空会把它拆成三段。但第一张要和上面那段话隔开一行，
        否则对话和动作糊成一片。
        """
        if self.children and not isinstance(self.children[-1], ToolCard):
            card.styles.margin = (1, 0, 0, 0)
        self.mount(card)
        self._stick()


#: 活动横幅上那句随机短语。与 GUI 的 `loadingPhrases.js` 同一份名单
#: （那份文件自己注释了：源头是"加载随机句.txt"，今天两边都是硬编码，
#:   正解是后端出只读端点）。在正解落地之前，这里只能跟着抄一份 ——
#:   改短语要两处一起改，否则两个界面的横幅措辞会漂。
LOADING_PHRASES = (
    "解释中", "修饰语句", "正在思考", "阐明想法", "构思中", "思考中", "工作中", "操作中",
    "尝试理解", "沉思中", "进行推理", "生成中", "尝试中", "整理中", "计算中", "正在执行",
    "深度思考", "编写中", "编写文件", "输入中", "理清思路", "处理中",
)

#: 活动横幅的转轮帧。GUI 用的是旋转图标，终端里就是这四个字符轮转。
SPIN_FRAMES = "-\\|/"

#: 横幅延迟出现的门槛（秒）。零点几秒就结束的活，横幅闪一下比不闪更吵。
_BANNER_DELAY = 0.75

#: 命令原文最多显示几行（不计"还有 N 行"那行）。再多会把对话挤走。
_CMD_MAX_LINES = 4

#: 只借 `Text.wrap` 按显示宽度断行（中文按两格）。宽度每次现传。
_WRAP_CONSOLE = Console(width=200, legacy_windows=False)


def random_loading_phrase() -> str:
    #: 短语名单与后端共用同一张表（`locales/en.json`）；表里没有的那几句
    #: 回落中文，不会变成裸键。
    return t(random.choice(LOADING_PHRASES))


def fmt_count(n: int) -> str:
    """301000 -> '301k'，1200 -> '1.2k'。与 GUI 的 `fmtCount` 同一套。"""
    if n < 1000:
        return str(int(n))
    v, u = n / 1000, "k"
    if v >= 999.5:
        v, u = n / 1e6, "m"
    if v >= 999.5:
        v, u = n / 1e9, "g"
    return f"{v:.1f}".replace(".0", "") + u


def fmt_step_ms(ms: float) -> str:
    """一步的用时：< 1 秒给毫秒（`857ms`），≥ 1 秒换成秒（`8.57s`）。

    与 GUI `format.js::fmtStepMs` 同一条判据：50ms 写成 `0.1s` 会把"快"
    这件事抹掉，而 `8574ms` 要读者心算 —— 按量级换单位，两边都好读。
    """
    if ms < 1000:
        return f"{int(round(ms))}ms"
    return fmt_elapsed(ms / 1000)


def fmt_elapsed(seconds: float) -> str:
    """10 秒以内给两位小数（'0.75s'）—— 一位的话第一眼看到的是 0.8s，
    而那个 0.05 的差正是"它到底动没动"的信号。过了 10 秒一位就够。"""
    if seconds < 10:
        return f"{seconds:.2f}s"
    return f"{seconds:.1f}s"


class ToolCard(Static):
    """一次工具调用 —— 步骤流的一格。

    跑着的时候是"进行时描述"（`● 正在写入 x.svg`）+ 命令原文
    （`> <完整命令>`，单行、超宽截断）+ 右上角 `running 1.2s`，
    跑完换成"工具名 + 结局"（`● run_command（已完成）`）——
    进行时描述的是**此刻**，跑完还停在那儿像卡住；而事后翻记录时，
    该被看见的是"哪个工具、什么结局"，不是措辞。

    与对话区其它块不同，这一块**必须能改**：它的核心信息是"跑完了没有"，
    而这件事在调起那一刻还不知道。所以它是卡片而不是一行定稿。

    不做展开/折叠：`tool.end` 只带结果长度、不带正文，折叠出来会是一片
    空白，那是假装能展开。
    """

    def __init__(self, *, activity: str, arg: str = "", name: str = "",
                 backgroundable: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        #: 调起那刻的进行时描述（服务端 `activity` 原样，带"正在"）。
        self._activity = (activity or "").strip() or t("调用工具")
        #: 参数原文（`sub_hint`，今天只有 run_command 有）或短标识
        #: （`arg_hint`，文件名）。参数那一行。
        self._arg = (arg or "").strip()
        self._name = (name or "").strip()
        #: 这一步有没有前台执行体（服务端 `tool.start.backgroundable`）。
        #: 只有它真的时候才提示 Ctrl+B —— 没得转的时候提示等于骗人。
        self._backgroundable = bool(backgroundable)
        self._label = self._activity
        #: 后端给的结局（`completed` / `waiting` / `error` / `denied` /
        #: `rejected`），另外 `run`（跑着）与 `unknown`（没收到结束事件）
        #: 是界面自己的两个状态。公开的：界面之外也可能要按它筛。
        self.state = "run"
        #: 结局的细分（`failed` / `refused` / `cancelled` / ...），决定文案。
        self._reason = ""
        #: 运行中：从调起算起的秒数。
        self._took = 0.0
        self._meta = ""

    def on_mount(self) -> None:
        self._paint()

    def _arg_block(self, width: int) -> Text | None:
        """命令原文：最多 `_CMD_MAX_LINES` 行，多出来的折成一行计数。

        折行按显示宽度（rich cell，中文两格）。单行截断会藏掉命令的尾巴，
        无限折行会把对话挤成走马灯 —— 四行封顶是两头折中。
        """
        if not self._arg:
            return None
        avail = max(20, width - 4)          # 每行前面有 "  > " / 四个空格
        wrapped = [str(t).rstrip() for t in
                   Text(self._arg).wrap(_WRAP_CONSOLE, avail)]
        hidden = max(0, len(wrapped) - _CMD_MAX_LINES)
        shown = wrapped[:_CMD_MAX_LINES]
        out = Text()
        for i, line in enumerate(shown):
            if i:
                out.append("\n    ")
            else:
                out.append("  > ")
            out.append(line, style="dim")
        if hidden:
            out.append("\n    " + t("── 还有 %s 行 ──") % hidden, style="dim")
        return out

    def _build(self, width: int | None = None):
        """拼出这张卡。纯数据进纯数据出，不起 App 也能断言。

        *width* 缺省取部件宽度；宽度变了要重画（见 `on_resize`）。
        方法名不能叫 `_compose`：那是 Textual 的异步钩子。
        """
        if width is None:
            width = self.size.width or 80
        # 圆点单独染色：Text 的 base style 会传染给之后 append 的每一段
        # （这就是"整行跟着圆点变绿"的根源）—— 先建无样式的 Text，再给
        # 圆点那一段上色，正文保持默认白。
        colour = ("green" if self.state == "run"
                  else O.dot(self.state, self._reason))
        label = self._activity if self.state == "run" else self._label
        head = Text()
        head.append("● ", style=colour)
        head.append(label)

        meta = self._meta
        if not meta and self.state == "run":
            # 跑着的时候右上角是 `running 1.23s`：没有这个词，那格数字
            # 读起来更像"这一步用了多久"而不是"它还在跑"。还没到第一拍
            # （0.5s）时只给 `running…` —— `running 0ms` 是噪声。
            meta = (f"running {fmt_step_ms(self._took * 1000)}"
                    if self._took > 0 else "running…")

        # 右上角要贴右边，但卡片宽度随窗口变 —— 用一张可伸展的表让 rich
        # 自己算，而不是在这里按当前宽度补齐空格（那样改窗口大小就错位）。
        row = Table.grid(expand=True)
        row.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        row.add_column(justify="right", no_wrap=True)
        row.add_row(head, Text(meta, style="dim"))

        parts: list = [row]
        if self._backgroundable and self.state == "run":
            parts.append(Text("  " + t("# 使用 ctrl+b 转至后台运行"),
                             style="dim"))
        arg_block = self._arg_block(width)
        if arg_block is not None:
            parts.append(arg_block)
        return Group(*parts) if len(parts) > 1 else row

    # -- 状态 -----------------------------------------------------

    def tick(self, seconds: float) -> None:
        """运行中跟着节拍（0.5s）重画。

        判据是"显示出来的字变了没有"，不是"整秒变没变" —— `running 1.23s`
        的那两位小数每拍都在动，按整秒判它就永远停在第一帧上。
        """
        if self.state != "run":
            return
        before = fmt_step_ms(self._took * 1000)
        if fmt_step_ms(seconds * 1000) == before:
            return
        self._took = seconds
        self._paint()

    def done(self, *, outcome: str, reason: str = "",
             chars: int = 0, ms: int = 0) -> None:
        """跑完了：结局 + 真执行过才有意义的体积与耗时。

        错误正文不显示：结局已经说完（"失败"对"未执行"是两句话），
        而排障要看的那份正文在轨迹/日志里，卡上铺开只会把对话冲掉。
        """
        self.state = outcome or "completed"
        self._reason = reason or ""
        self._label = t("%s（%s）") % (self._name or t("工具"),
                                       O.label(self.state, self._reason))
        bits = []
        # 只有真跑完的才有"体积 · 耗时"可报：等审批的那张卡报的是
        # 审批载荷的长度，比不报更误导。
        if self.state == "completed":
            if chars:
                bits.append(t("%s 字符") % fmt_chars(chars))
            if ms:
                bits.append(fmt_ms(ms))
        self._meta = " · ".join(bits)
        self._paint()

    def orphan(self) -> None:
        """这一轮结束了，但它的结束事件没来。

        后端中途崩掉、或者我们在工具跑到一半才连上事件流，都会这样。
        标成"没收到"而不是当成成功：用户会以为那个工具跑完了。
        """
        if self.state != "run":
            return
        self.state = "unknown"
        self._meta = t("未检测到结束事件")
        self._paint()

    # -- 画 -------------------------------------------------------

    def on_resize(self, _event) -> None:
        """窗口变宽/变窄时重画：命令的行数是按宽度算的，不重画就会停在
        上一次的断行位置上（宽了不合并、窄了溢出）。"""
        self._paint()

    def _paint(self) -> None:
        self.update(self._build())


#: 小键盘上那些"有字符"的键：有些终端只给键名不给字符（除号报成 `divide`），
#: 而 `TextArea` 只按字符插入 —— 按键到了、字没出来。这里按键盘面补一次。
_KEYPAD_TEXT = {
    "slash": "/",
    "divide": "/",
    "kp_divide": "/",
    "keypad_divide": "/",
    "asterisk": "*",
    "multiply": "*",
    "plus": "+",
    "minus": "-",
    "full_stop": ".",
    "period": ".",
}


def _keypad_text(key: str) -> str:
    """按键名取要插入的字。带修饰键的（`shift+divide`）只看最后一段 ——
    能走到这里的只有"没有字符"的键，而那正是小键盘那批。"""
    return _KEYPAD_TEXT.get(key.rsplit("+", 1)[-1], "")


class Composer(TextArea):
    """多行输入框。回车发送；Shift+Enter 或 Ctrl+J 换行。

    命令表打开时，回车改成"填入选中项"，↑↓ 归选择（见 `App` 的接线）。

    Shift+Enter 在 Windows 控制台能区分；个别终端把它和回车混成一个键 ——
    那时用 Ctrl+J，它在任何终端里都是独立的键（LF）。
    """

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class Complete(Message):
        """Tab：请应用把选择往下挪一格。"""

    class Move(Message):
        """↑ / ↓：在命令表里挪选择（不碰光标）。"""

        def __init__(self, delta: int) -> None:
            self.delta = delta
            super().__init__()

    class Accept(Message):
        """回车：把选中的命令填进输入框。"""

    class Dismiss(Message):
        """Esc：收起命令表，输入框里的字不动。"""

    class Nav(Message):
        """底栏两格 / 详细页的方向键请求。`action` 见 `App._on_nav`。"""

        def __init__(self, action: str, arg: int = 0) -> None:
            self.action = action
            self.arg = arg
            super().__init__()

    class Background(Message):
        """Ctrl+B：把当前前台任务转到后台。"""

    class AttachImage(Message):
        """Alt+V：请应用去读剪贴板里的图。"""

    BINDINGS = [
        # priority：这几个键必须抢在"插入字符/换行"之前 —— 尤其回车，
        # TextArea 对它的默认处理就是插一个换行。
        Binding("enter", "submit", show=False, priority=True),
        Binding("shift+enter", "newline", show=False, priority=True),
        Binding("ctrl+j", "newline", show=False, priority=True),
        Binding("tab", "complete", show=False, priority=True),
        # priority：命令表打开时，上下键归选择，不归光标。
        Binding("up", "move(-1)", show=False, priority=True),
        Binding("down", "move(1)", show=False, priority=True),
        # 底栏选中态下，左右在两格之间换；普通输入时仍是移动光标。
        Binding("left", "horizontal(-1)", show=False, priority=True),
        Binding("right", "horizontal(1)", show=False, priority=True),
        Binding("ctrl+b", "background", show=False, priority=True),
        # Ctrl+V 由终端自己处理（它绑了粘贴），应用收不到这个键；
        # Alt+V 不在终端的绑定表里，会原样送到这里。
        Binding("alt+v", "attach_image", show=False, priority=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(show_line_numbers=False, soft_wrap=True, **kwargs)
        #: 命令表开着没有 —— 由 App 维护（候选是 App 算的），这里只读。
        self.suggest_open = False
        #: 底栏导航态：`""` / `"pills"`（选中两格之一）/ `"detail"`（详细页开着）。
        self.nav = ""
        #: 底栏有没有可进入的格子（后台/子代理计数 > 0）。由 App 维护。
        self.tasks_ready = False

    def action_submit(self) -> None:
        if self.nav == "pills":
            self.post_message(self.Nav("open"))
            return
        if self.suggest_open:
            self.post_message(self.Accept())
            return
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(text))

    def action_move(self, delta: int) -> None:
        if self.suggest_open:
            self.post_message(self.Move(delta))
            return
        if self.nav == "detail":
            self.post_message(self.Nav("detail_down" if delta > 0
                                       else "detail_up"))
            return
        if self.nav == "pills":
            if delta < 0:
                self.post_message(self.Nav("leave"))
            return
        if delta > 0 and self.tasks_ready:
            self.post_message(self.Nav("enter"))
            return
        if delta < 0:
            self.action_cursor_up()
        else:
            self.action_cursor_down()

    def action_horizontal(self, delta: int) -> None:
        if self.nav == "pills":
            self.post_message(self.Nav("switch", delta))
            return
        if delta < 0:
            self.action_cursor_left()
        else:
            self.action_cursor_right()

    def action_background(self) -> None:
        self.post_message(self.Background())

    def action_attach_image(self) -> None:
        self.post_message(self.AttachImage())

    def on_key(self, event) -> None:
        if event.key == "escape" and self.nav:
            self.post_message(self.Nav("close"))
            event.stop()
            event.prevent_default()
            return
        if event.key == "escape" and self.suggest_open:
            self.post_message(self.Dismiss())
            event.stop()
            event.prevent_default()
            return
        if event.character:
            # 打字就是要回到输入框：底栏/详细页的选中态顺势收起。
            if self.nav:
                self.post_message(self.Nav("close"))
            return
        text = _keypad_text(event.key)
        if not text and len(event.key) == 1 and event.key.isalnum():
            text = event.key          # 小键盘数字（个别终端也不给字符）
        if text:
            self.insert(text)
            event.stop()
            event.prevent_default()

    def action_newline(self) -> None:
        self.insert("\n")

    def action_complete(self) -> None:
        self.post_message(self.Complete())


class SuggestBar(Static):
    """斜杠命令表：一屏最多 6 条，↑↓ 翻，回车填入。

    命令目录来自后端 `/api/commands` —— 成员与说明都在后端，
    前端自己抄一份就是两套真相源：加一条命令时两边漂，而两边看着都对。
    """

    #: 一屏显示几条。再多就把输入框顶走了，而 ↑↓ 翻比"一屏塞二十条"好读。
    WINDOW = 6

    def show(self, matches: list[dict], index: int, top: int) -> None:
        self.display = True
        rows = matches[top:top + self.WINDOW]
        parts: list = []
        for i, m in enumerate(rows, start=top):
            # 绿色只给 `>` 那一格：base style 会染给之后 append 的每一段。
            line = Text()
            if i == index:
                line.append("> ", style="bold green")
                line.append(m["cmd"], style="bold")
            else:
                line.append("  ")
                line.append(m["cmd"])
            if m.get("desc"):
                line.append("  " + m["desc"], style="dim")
            if not m.get("implemented", True):
                line.append("  " + t("（未实现）"), style="dim")
            parts.append(line)
        hint = t("↑↓ 选择 · enter填入 · Esc 收起")
        if len(matches) > self.WINDOW:
            hint += t(" · 共 %s 条") % len(matches)
        parts.append(Text("  " + hint, style="dim"))
        self.update(Group(*parts))

    def clear(self) -> None:
        """收起。display:none 不占行 —— 候选条只在有用的时候存在。"""
        self.display = False


class UpdateHint(Static):
    """输入框右上角的更新提示。

    只有自动更新真的做了事才出现；检查失败与"已是最新"都不占行 ——
    这里是"可以做的事"，不是常态读数。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.display = False

    def show(self, message: str) -> None:
        self.update(Text(message, style="yellow"))
        self.display = True

    def clear(self) -> None:
        self.display = False


class BtwPanel(ScrollableContainer):
    """`/btw` 的回答窗：蓝框、可上下翻，Esc 由应用层关。

    打开时转写区整块让位 —— 顶部只剩活动横幅（或完成读数），
    关掉之后恢复原来的步骤流。回答不落历史，也不进转写区。
    """

    BINDINGS = [
        Binding("up", "scroll_up", show=False, priority=True),
        Binding("down", "scroll_down", show=False, priority=True),
        Binding("pageup", "page_up", show=False, priority=True),
        Binding("pagedown", "page_down", show=False, priority=True),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="btw-text")

    def start(self) -> None:
        self.query_one("#btw-text", Static).update(
            Text("/btw " + t("正在回答…"), style="cyan"))
        self.display = True
        self.scroll_home(animate=False)

    def finish(self, text: str) -> None:
        self.query_one("#btw-text", Static).update(Text(text or t("（没有回答）")))
        self.scroll_home(animate=False)

    def text(self) -> str:
        return str(self.query_one("#btw-text", Static).render())


#: 故障横幅兜底文案（后端 `text` 通常自带一句，`core/llm_errors.HEADLINES`
#: 是唯一口径）。这三句只在后端没给字时用，与 GUI `format.js::retryLabel`
#: 逐字一致 —— 两边漂了，同一场故障会讲两句不同的话。
_RETRY_LABELS = {
    "timeout": "网络连接超时——请检查网络或代理",
    "connection": "网络连接异常--请检查宽带互联网连接",
}


def retry_label(kind: str, text: str = "") -> str:
    """故障横幅上那句话。后端给了就用后端的，没给才按类型兜底。"""
    said = (text or "").strip()
    if said:
        return said
    return t(_RETRY_LABELS.get((kind or "").lower(),
                               "模型服务连接异常——正在尝试重新连接"))


class StatusLine(Static):
    """活动横幅：转轮 + 随机短语 + 用时 + 本轮输出 token。

    与 GUI 的 `StatusBar` 同形：四样东西排在同一条上，不左右分栏。
    短语每轮换一句；计时在等审批时暂停（那段时间它在等人，不是在干活）；
    token 取 `turn.end` 的 `output_tokens`（本轮输出，不是会话累计）。
    出现延迟 750ms：零点几秒的活不闪横幅。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._busy = False
        self._phrase = ""
        self._since = 0.0          # 本轮开始的 monotonic 时刻
        self._pause_acc = 0.0      # 累计"等审批"的秒数
        self._pause_at = 0.0       # 本次暂停开始的时刻（0 = 没在暂停）
        self._tokens = 0
        self._frame = 0
        #: 横幅颜色：正常绿 / 压缩黄。压缩有自己的起点（用户要的是"压了多久"）。
        self._colour = "green"
        self._compact_since = 0.0
        #: 故障横幅，按来源分格存（"llm" / "stream"）。有它就压过活动横幅
        #: —— 在等上游重连时，转轮和计时都是假信息。显示**最后挂上的那格**：
        #: 一边恢复时只摘自己的，另一边的还挂着就接着显示。
        self._faults: dict[str, tuple[str, str]] = {}

    # -- 生命周期 -------------------------------------------------

    def begin(self) -> None:
        """一轮开始：换一句、清两个账。"""
        self._busy = True
        self._faults.pop("llm", None)
        self._phrase = random_loading_phrase()
        self._colour = "green"
        self._compact_since = 0.0
        self._since = time.monotonic()
        self._pause_acc = 0.0
        self._pause_at = 0.0
        self._tokens = 0
        self._paint()

    def pause(self) -> None:
        """等审批：计时冻在进入那一刻。"""
        if self._busy and not self._pause_at:
            self._pause_at = time.monotonic()

    def resume(self) -> None:
        if self._pause_at:
            self._pause_acc += time.monotonic() - self._pause_at
            self._pause_at = 0.0
        self._paint()

    def set_tokens(self, n: int) -> None:
        self._tokens = max(0, int(n))
        self._paint()

    def fault(self, text: str, meta: str = "", tag: str = "") -> None:
        """挂一条故障横幅：全红一行，不带转轮、不带计时。

        与 GUI 那条红条同语义：它说的是"现在的连接有问题"，不是
        "这一轮在做什么"。*tag* 是来源（"llm" / "stream"），恢复时按它认领。
        """
        self._faults[tag] = ((text or "").strip() or t("连接异常"), meta)
        self._paint()

    def clear_fault(self, tag: str = "") -> None:
        """摘掉自己挂的那条。别的来源还挂着就接着显示。"""
        if tag in self._faults:
            del self._faults[tag]
            self._paint()

    def show_done(self, text: str) -> None:
        """把横幅换成完成读数（btw 窗口的顶行用它）。

        空闲状态下转轮不再刷新，这一行会一直留着，直到窗口关掉。
        """
        self._busy = False
        self.update(Text("  " + text, style="dim"))

    def idle(self) -> None:
        self._busy = False
        self._faults.pop("llm", None)    # 事件流断开与否与这一轮无关，留着
        self._paint()

    def compact_start(self) -> None:
        """压缩开始：黄条、固定短语、从这一刻计时（不是从这一轮开始）。"""
        self._busy = True
        self._phrase = t("正在压缩")
        self._colour = "yellow"
        self._compact_since = time.monotonic()
        self._paint()

    def compact_end(self) -> None:
        """压缩结束：回到正常运行态（这一轮若还在跑，横幅继续走）。"""
        if not self._compact_since:
            return
        self._compact_since = 0.0
        self._colour = "green"
        self._phrase = random_loading_phrase()
        self._paint()

    def spin(self) -> None:
        """转轮走一帧。每 120ms 由 app 调一次；空闲时不动。"""
        if not self._busy:
            return
        self._frame += 1
        self._paint()



    # -- 画 -------------------------------------------------------

    def _elapsed(self) -> float:
        now = time.monotonic()
        stop = self._pause_at or now
        return max(0.0, stop - self._since - self._pause_acc)

    def _build(self):
        """横幅的样子。不起 App 也能断言。"""
        if self._faults:
            # 全红一行：没有转轮（那会显示"还在动"，而它恰恰是动不了）。
            # 多种故障同时挂时显示最后挂上的那格 —— 它说的是"现在"。
            tag = next(reversed(self._faults))
            text, meta = self._faults[tag]
            line = Text("  ", style="red")
            line.append(f"！ {text}")
            if meta:
                line.append("  " + meta)
            return line
        if not self._busy:
            return Text("")
        compacting = bool(self._compact_since)
        elapsed = (time.monotonic() - self._compact_since if compacting
                   else self._elapsed())
        if elapsed < _BANNER_DELAY and not compacting:
            return Text("")
        frame = SPIN_FRAMES[self._frame % len(SPIN_FRAMES)]
        # 与 GUI 同形：短语、用时、token 在同一条上（不是左右分栏）。
        # 左缩两格与输入框正文对齐；整条同色且加粗（加粗是为了在满屏
        # 正文里一眼扫到它 —— 这一行回答的是"它还在动吗"，得比正文重）。
        line = Text("  ", style=f"bold {self._colour}")
        line.append(f"{frame} {self._phrase}…")
        meta = fmt_elapsed(elapsed)
        if self._tokens and not compacting:
            meta += f" / {fmt_count(self._tokens)} token"
        line.append("  " + meta)
        return line

    def _paint(self) -> None:
        self.update(self._build())


def services_label(status: dict) -> tuple[str, bool]:
    """底栏那格后台服务读数 —— 返回 `(文案, 要不要染黄)`。

    后端报三格：开关、正在跑的通道数、定时任务条数（读不到是 `None`）。
    关着时要把"还有 N 条定时任务没在跑"说出来——
    否则用户会以为那些任务被删了。
    """
    cron = status.get("cron_jobs")
    cron_n = cron if isinstance(cron, int) and not isinstance(cron, bool) else None
    if status.get("background_services") is False:
        text = t("后台服务：关")
        if cron_n:
            text += t("（定时 %s 条未启用）") % cron_n
        return text, True
    parts = []
    ch = status.get("channels_running")
    if isinstance(ch, int) and not isinstance(ch, bool) and ch:
        parts.append(t("通道 %s") % ch)
    if cron_n:
        parts.append(t("定时 %s") % cron_n)
    return " · ".join(parts), False


class StatusBar(Widget):
    """最底下一行：左边是当前档位，右边是命令入口。

    档位来自后端 `/api/status` 的 `mode_name` / `effort_name`，不是写死的文案：
    写死的话，用户在别处改了模式，这里会继续显示旧值，比不显示更糟。
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Horizontal

        with Horizontal():
            yield Static("", id="state")
            yield Static("", id="tasks")
            yield Static("", id="hint")

    def show_tasks(self, *, bg: int, sub: int, focus: str = "") -> None:
        """底栏中间那两格。计数为 0 的整格不画（照 GUI：它们是状态不是入口）。

        *focus* 是当前被选中的那一格（`bg` / `sub` / 空）：
        选中绿、未选中黄。
        """
        cell = self.query_one("#tasks", Static)
        if not bg and not sub:
            cell.update(Text(""))
            return
        line = Text()
        if bg:
            line.append(t("%s个后台") % bg,
                        style="green" if focus == "bg" else "yellow")
        if sub:
            if bg:
                line.append("    ")
            line.append(t("%s个子代理") % sub,
                        style="green" if focus == "sub" else "yellow")
        cell.update(line)

    def show(self, *, mode: str, effort: str, busy: bool = False,
             ctx: str = "", ctx_armed: bool = False,
             services: str = "", services_warn: bool = False) -> None:
        parts = []
        if mode:
            parts.append(f"mode: {mode}")
        if effort:
            parts.append(f"effort: {effort}")
        state = Text(" · ".join(parts))
        # 后台服务那格只在有话可说时出现（关着 / 有通道 / 有定时任务）；
        #   全都没有就不占位置 —— 它是一格状态，不是入口。
        if services:
            if state.plain:
                state.append(" · ")
            state.append(services, style="yellow" if services_warn else "dim")
        self.query_one("#state", Static).update(state)
        hint = Text()
        if busy:
            # 中断是跑着的时候才存在的操作 —— 空闲时提示它，是在教人按一个
            # 没有用的键。
            hint.append("Esc", style="bold")
            hint.append(t(" 中断") + "   ")
        # 右下角是上下文读数（原来是 /help 入口，那条命令已取消）。
        # `armed`（已进预警区、还没到阈值）追加一句，黄色 —— 与 GUI 同一格
        # 语义（那边 `.ctx-readout.armed` 只改颜色）。这是提醒，不是错误，
        # 所以不染整条，只染这一截。
        hint.append(ctx or t("上下文：—"), style="dim")
        if ctx_armed:
            hint.append(t(" · 接近触发"), style="yellow")
        self.query_one("#hint", Static).update(hint)


class TaskDetail(Static):
    """后台任务 / 子代理的详细页：输入框上方，最多 5 行，↑↓ 翻。

    只读：这里的条目不给选中态 —— 用户要的是"看一眼在跑什么"，
    不是在这里做操作（停止/转后台在各自的地方）。
    """

    #: 一屏最多几条（用户定的上限）。
    WINDOW = 5

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.display = False

    def show(self, *, title: str, rows: list[str], top: int = 0) -> None:
        self.display = True
        out = Text()
        out.append(title, style="bold")
        shown = rows[top:top + self.WINDOW]
        if not shown:
            out.append("\n  " + t("无在跑的%s") % title, style="dim")
        for row in shown:
            out.append("\n  " + row)
        if len(rows) > self.WINDOW:
            out.append("\n  " + t("—— 还有 %s 条 ——") % (
                len(rows) - top - len(shown)), style="dim")
        out.append("\n  " + t("↑↓ 翻页 · ↓ 返回底栏 · Esc 收起"), style="dim")
        self.update(out)

    def clear(self) -> None:
        self.display = False


class ApprovalPanel(Vertical):
    """审批面板：占输入框那个槽，选项用 ▌ 标当前项。

    授权语义全在后端：这里只把选中的 key 原样送回去。
    """

    #: 容器默认不可聚焦，而这里要接数字键与上下键。
    can_focus = True

    class Answer(Message):
        def __init__(self, *, choice: str = "", text: str = "") -> None:
            self.choice = choice
            self.text = text
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._prompt: A.Prompt | None = None
        #: 被高亮的那一项。初始落在默认项上 —— "永不默认选中升级项"
        #: 由后端保证（`default_key` 不指升级项）。
        self._picked = 0
        #: 多选里被勾上的项。空格勾/取消，回车按选项顺序拼 "1,3"。
        self._ticked: set[int] = set()

    def compose(self) -> ComposeResult:
        yield Static("", id="approval-body")
        yield Input(id="approval-input", placeholder=t("还有补充…"))

    def on_mount(self) -> None:
        self.display = False
        self.query_one("#approval-input", Input).display = False

    # -- 挂上 / 收起 ----------------------------------------------

    def show_prompt(self, prompt: A.Prompt) -> None:
        self._prompt = prompt
        self._picked = next((i for i, o in enumerate(prompt.options)
                             if o.key == prompt.default_key), 0)
        self._ticked.clear()
        self.display = True
        inp = self.query_one("#approval-input", Input)
        if prompt.wants_text:
            inp.value = ""
            inp.display = True
            inp.focus()
        else:
            inp.display = False
            self.focus()
        self._paint()

    def clear(self) -> None:
        """收起面板并清状态。幂等 —— App 的两条收尾路都会调它。"""
        self._prompt = None
        self._ticked.clear()
        self.display = False
        self.query_one("#approval-input", Input).display = False

    # -- 键 -------------------------------------------------------

    def on_key(self, event) -> None:
        p = self._prompt
        if p is None or p.wants_text:
            return
        n = len(p.options)
        key = event.key
        if key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < n:
                self._picked = idx
                self._paint()
            event.stop()
            event.prevent_default()
            return
        if key == "space" and p.multi:
            self._ticked ^= {self._picked}
            self._paint()
            event.stop()
            event.prevent_default()
            return
        if key in ("up", "down"):
            self._picked = (self._picked + (-1 if key == "up" else 1)) % n
            self._paint()
            event.stop()
            event.prevent_default()
            return
        if key == "enter":
            if p.multi and self._ticked:
                # 按选项顺序拼（不是勾选顺序）—— 后端按表结算。
                choice = ",".join(o.key for i, o in enumerate(p.options)
                                  if i in self._ticked)
            else:
                choice = p.options[self._picked].key
            self.post_message(self.Answer(choice=choice))
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """文本态：空回车不提交（后端没有默认值，发空串等于替用户放弃）。"""
        text = event.value.strip()
        if text:
            self.post_message(self.Answer(text=text))

    # -- 画 -------------------------------------------------------

    def _paint(self) -> None:
        p = self._prompt
        body = self.query_one("#approval-body", Static)
        if p is None:
            body.update(Text(""))
            return
        # 文本态的问题文本可能在 `question` 而不是 `body`（后端两种都发过），
        # `from_payload` 把不认识的键收进 `extra`。
        text = p.body or str(p.extra.get("question") or "")
        out = Text()
        if text:
            out.append(text)
        if not p.wants_text:
            if text:
                out.append("\n")
            for i, opt in enumerate(p.options):
                if i or text:
                    out.append("\n")
                out.append("▌ " if i == self._picked else "  ",
                           style="bold" if i == self._picked else "")
                out.append(f"{i + 1}. ", style="dim")
                out.append(opt.label, style="bold" if i == self._picked else "")
                if i in self._ticked:
                    out.append("  ✓", style="green")
                if opt.description:
                    out.append("  " + opt.description, style="dim")
            out.append("\n\n")
            out.append("  " + t("↑↓ 或数字选择，回车确认")
                       + (t("；空格勾选") if p.multi else ""), style="dim")
        body.update(out)
