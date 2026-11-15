"""句柄：形态、分配、入境中和、近失形态扫描。

═══════════════════════════════════════════════════════════════════════
形态为什么是 `【PII:N】` 而不是私用区（PUA）—— 实际结论
═══════════════════════════════════════════════════════════════════════

PUA 码点曾是候选形态，理由是"模型没有东西可改写"。实际把它否掉了：

    卢恩字母   `ᚠᚡᚢᚣ`（U+16A0-16A3）  ->  模型逐字节抄对
    格拉哥里   `ⰀⰁⰂⰃ`（U+2C00-2C03）  ->  模型逐字节抄对
    私用区                       `󠀀`→ 模型「打不出」

注意对照才是这条结论的力气所在：两个可见罕见字符块它都抄得逐字节全对，
只有 PUA 吐不出来，不是"探针的毛病"，是 PUA 本身不能用。
（精确措辞：不能断言"分词器把 PUA 塌成了一个 token"；证据只支持"模型无法产出 PUA"。
 对 ICE 而言两条后果相同：句柄搬不过去。）

`【PII:N】` 随后按同一个探针验过：三个句柄逐字节相等、不自己补号、
括号不丢、半角冒号没被换成全角，可用。

换来的：人可读、可调试、GBK 安全（不用 `chr()` 生成）、容量无上限、
句柄形态与日志显示形态合成一个。
付出的：模型有东西可改写了 —— 那条"宽容匹配"从空转变成了真需求（见下）。

═══════════════════════════════════════════════════════════════════════
两条铁律（与 PUA 版同形，但实现不同）
═══════════════════════════════════════════════════════════════════════

① 入境无差别剥掉全部精确形态 `【PII:N】` —— 不管在不在映射里。

    PUA 版能"无差别剥"是因为真值里永远不会有 PUA。可见形态没这个便宜：
    一段网页可以天生就写着 `【PII:1】`，而 N 是连号、猜都不用猜
只剥"我们发过的"那条路会让出口还原把真值填进攻击者指定的位置。
判据放宽成"形状对不对"（而不是"在不在映射里"），强度与 PUA 版持平。
    代价：文档里恰好写着我们这个保留格式时会被剥掉 —— 概率约等于 0。

    只剥精确形态，不剥近失形态。判据是攻击面：只有精确形态能被还原，
    近失还原不了，只有精确形态危险。多剥那半会误伤正文里正常写着 `PII:1` 的文本，
    而换不到任何安全 —— 而且那会让"句柄形状"有两个定义、两份差事，
    正是最忌的那种结构。近失走 `scan_variants` 只报告。

② 还原只认精确形态；近失写法（`[PII:1]` / 全角冒号 / 补零）只报不还原。

    容忍还原 = 往更多位置注入真值 —— 那正是
    "还原失败可以宽容，注入必须严谨"那条护栏要防的。近失走 `scan_variants` 报出来，
    人工/读数看得见，但不参与还原。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Collection, Dict, Iterable, Iterator, List, Optional, Tuple

from core.ice.types import Residual

#: 句柄的唯一形态。前后是全角方括号 `【】`(U+3010/U+3011) —— 实际 GBK 编得出，
#: 而 `⟦⟧`(U+27E6/27E7) 编不出。
HANDLE_PREFIX = "【PII:"
HANDLE_SUFFIX = "】"

#: 精确形态。还原只认它。
#:
#: 不许前导零（`[1-9][0-9]{0,8}` 而不是 `[0-9]{1,9}`）：我们发的是 `【PII:1】`，
#: 模型要是写成 `【PII:01】`，那是写歪了，必须落进 `malformed_handle` 那条路
#: （只报不改）。放前导零进来的话，它会被精确形态吃成一个"未登记的句柄"、
#: 然后被静默丢掉 —— 写歪了却连一声都不出，正是最忌的形状。
#: 不许前导零（`[1-9][0-9]{0,8}` 而不是 `[0-9]{1,9}`）：我们发的是 `【PII:1】`，
#: 模型要是写成 `【PII:01】`，那是写歪了，必须落进 `malformed_handle` 那条路
#: （只报不改）。放前导零进来的话，它会被精确形态吃成一个"未登记的句柄"、
#: 然后被静默丢掉 —— 写歪了却连一声都不出，正是最忌的形状。
HANDLE_RE = re.compile(r"【PII:([1-9][0-9]{0,8})】")

#: 有损解码的痕迹。用码点算，不写字面量 —— U+FFFD 编不出 GBK。
REPLACEMENT_CHAR = 0xFFFD

#: 单会话句柄上限。可见形态的容量本来没有上限，这个数是内存护栏，
#: 不是能力边界；超了就地"生不出句柄"。
MAX_HANDLE_INDEX = 999_999_999


def handle_text(index: int) -> str:
    """第 *index* 个句柄（从 1 起）。越界抛 :class:`ValueError`。

    这个可以直接写成字面量（GBK 安全），
    但为了不给"句柄字面量"开口子，仍然走函数生成。
    """
    if index < 1 or index > MAX_HANDLE_INDEX:
        raise ValueError("handle index out of range: %d" % index)
    return "%s%d%s" % (HANDLE_PREFIX, index, HANDLE_SUFFIX)


def display_of(index: int) -> str:
    """句柄的日志形态 —— 与句柄本身是同一个东西。"""
    return handle_text(index)


def is_handle(text: str) -> bool:
    """整串恰好是一个句柄。"""
    return bool(text) and HANDLE_RE.fullmatch(text) is not None


def iter_handles(text: str) -> Iterator[re.Match]:
    """扫出文本里精确形态的句柄（按出现顺序）。"""
    return HANDLE_RE.finditer(text or "")


def handle_index(match: re.Match) -> int:
    return int(match.group(1))


@dataclass(frozen=True)
class Handle:
    """一个已发出的句柄。

    `value` 是真值，且不进 repr（它会被异常栈/日志带出去）。
    `text` 是唯一允许进模型那一份的形态；`display` 是唯一允许进日志的形态
    —— 这两个是同一个串。
    """

    index: int
    value: str = field(repr=False, default="")
    rule_id: str = ""
    session_id: str = ""

    @property
    def text(self) -> str:
        return handle_text(self.index)

    @property
    def display(self) -> str:
        return handle_text(self.index)


def strip_handles_reporting(
    text: str, known: Collection[str]
) -> Tuple[str, int, Tuple[int, ...]]:
    """入境中和：剥掉文本里全部精确形态的句柄。

    返回 (干净串, 剥掉总数, 命中已知句柄的下标元组)。

    为什么是"全部"而不是"只剥我们发过的" —— 见模块 docstring 铁律①：
    `N` 是连号、猜都不用猜，只剥已知的那条路等于给攻击者开了一扇门。
    """
    if not text:
        return text, 0, ()
    hits: List[int] = []
    kept: List[str] = []
    pos = 0
    n = 0
    for m in HANDLE_RE.finditer(text):
        kept.append(text[pos:m.start()])
        pos = m.end()
        n += 1
        if m.group(0) in known:
            hits.append(int(m.group(1)))
    kept.append(text[pos:])
    return "".join(kept), n, tuple(hits)


#: 近失形态 —— 只报告，不还原（铁律②）。每一条都配一个能造出来的例子。
_NEAR_MISS_PATTERNS: Tuple[Tuple[str, str], ...] = (
    # 半角方括号
    (r"\[PII:[0-9]{1,9}\]", "ascii_brackets"),
    # 全角冒号
    (r"【PII：[0-9]{1,9}】", "fullwidth_colon"),
    # 小写
    (r"【pii:[0-9]{1,9}】", "lowercase"),
    # 补零
    (r"【PII:0[0-9]{1,8}】", "zero_padded"),
    # 括号只剩一边
    (r"【PII:[0-9]{1,9}(?![0-9])】?", "unbalanced"),
    # 光秃秃的 PII:1（连括号都没有）
    (r"(?<![A-Za-z])PII:[0-9]{1,9}(?![0-9])", "bare"),
)


def _near_miss_regexes() -> List[Tuple[re.Pattern, str]]:
    return [(re.compile(p), kind) for p, kind in _NEAR_MISS_PATTERNS]


def scan_variants(
    text: str, known_indices: Iterable[int]
) -> Tuple[Residual, ...]:
    """扫「句柄写歪了 / 被换了」的痕迹。只报告，绝不参与还原。

    三类：

    `foreign_handle`
        一个不属于本会话的精确形态句柄（下标不在映射里）。
        它意味着：有人编了一个，或者上一轮的句柄漏过来了。
    `malformed_handle`
        近失写法（半角括号 / 全角冒号 / 补零 / 大小写 / 少一边括号）。
        不还原：容忍还原等于往更多位置注入真值。
    `replacement_char`
        U+FFFD —— 有损解码已经啃掉了东西。
    """
    if not text:
        return ()
    out: List[Residual] = []
    known = set(known_indices)

    for i, ch in enumerate(text):
        if ord(ch) == REPLACEMENT_CHAR:
            out.append(Residual("replacement_char", i, i + 1, REPLACEMENT_CHAR))

    # 精确形态扫完之后把它们原地遮掉，再扫近失。
    #   理由是撞出来的：`unbalanced` / `bare` 两条模式是精确形态的超集
    #   —— `【PII:1】` 会被它们在自己身上命中，于是"我们自己的句柄"被报成
    #   `malformed`（那是假信号，会把真问题淹掉）。
    #   遮成 `\x00` 而不是删掉：下标必须与原串对齐（否则报出来的位置是错的）。
    masked = list(text)
    for m in HANDLE_RE.finditer(text):
        if int(m.group(1)) not in known:
            out.append(Residual("foreign_handle", m.start(), m.end(),
                                int(m.group(1))))
        for k in range(m.start(), m.end()):
            masked[k] = "\x00"
    masked_text = "".join(masked)

    for rx, kind in _near_miss_regexes():
        for m in rx.finditer(masked_text):
            out.append(Residual("malformed_handle:" + kind,
                                m.start(), m.end(), None))

    return tuple(sorted(out, key=lambda r: (r.start, r.kind)))
