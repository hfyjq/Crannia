"""ICE 信息侧的纯数据类型 —— 零逻辑、零依赖、零 I/O。

这一层只承载形状，不做任何判定。判据（怎么算高置信、能不能替换）在
:mod:`core.ice.rules`；句柄与 PUA 的事在 :mod:`core.ice.handles`。

真值一律不进 `repr`（`field(repr=False)`）：这些对象的 `repr` 会出现在
异常栈、调试输出、日志里，而它们的 `value` 字段装的是用户的身份证号。
`Finding.display` / `Handle.display` 是唯一允许外泄的形态，且只给码点不给真值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


class Confidence(str, Enum):
    """一条命中有多可信。

    只有 :attr:`HIGH` 允许替换。真伪的分界是校验位，不是"看起来像"。
    """

    HIGH = "high"      # 校验位 / 号段表通过
    MEDIUM = "medium"  # 结构成立但没有证明（本版没有规则会给出它 —— 预留）
    LOW = "low"        # 不确定，只报不改


@dataclass(frozen=True)
class Rule:
    """一条检测规则。由 TOML 装载（:mod:`core.ice.rules`），装载期校验。

    `supports_encryption` 是闸的声明位（同名字段的语义）：
    只有声明 `True` 且校验位通过的规则才允许替换。缺省 `False`
    —— fallback 必须朝"只报不改"倒。
    """

    id: str
    description: str = ""
    regex: str = ""
    #: 0 = 整个匹配；N = 第 N 个捕获组（组号越界在装载期就抛）
    secret_group: int = 0
    #: 非空时：这些词一个都不出现，这条规则根本不跑（省时间，不改语义）
    keywords: Tuple[str, ...] = ()
    supports_encryption: bool = False
    #: `"luhn"` / `"cn_id_mod11_2"` / `"cn_mobile"` / `"none"`。
    #: 未知值一律 LOW（不认识的校验器不许冒充通过）。
    validator: str = "none"
    #: 本版不求值。非空，这条规则强制不可替换 —— 免得
    #: 「以为过滤过了，其实那表达式从没跑过」。
    filter_expr: str = ""
    source: str = "builtin"


@dataclass(frozen=True)
class Finding:
    """一次命中。`start`/`end` 是字符下标（不是字节），半开区间。"""

    rule_id: str
    start: int
    end: int
    value: str = field(repr=False, default="")
    confidence: Confidence = Confidence.LOW
    replaceable: bool = False

    @property
    def span(self) -> Tuple[int, int]:
        return (self.start, self.end)

    @property
    def display(self) -> str:
        """可进日志的形态：只给规则名、位置、置信度，绝不给真值。"""
        return "%s@%d-%d/%s" % (self.rule_id, self.start, self.end,
                                self.confidence.value)


@dataclass(frozen=True)
class Residual:
    """出口扫描发现的一处残留/异常。

    `kind` 取值：

    `foreign_handle`
        一个不属于本会话的精确形态句柄（【PII:N】 而下标不在映射里）。
        可能是伪造、别的会话漏过来的、或上一轮残留。
    `malformed_handle:*`
        句柄被写歪了：半角括号 / 全角冒号 / 补零 / 大小写 / 少一边括号 / 光秃秃的
        `PII:N`。只报告，绝不还原 —— 容忍还原 = 往更多位置注入真值。
    `replacement_char`
        U+FFFD，说明有损解码已经啃掉了东西。
    """

    kind: str
    start: int
    end: int
    #: 精确形态的句柄存下标；近失形态与 U+FFFD 存各自的码点/`None`。
    codepoint: Optional[int] = None
