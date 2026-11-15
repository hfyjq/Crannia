"""检测层：正则扫一遍，用校验位把置信度分开，重叠的按优先级消解。

本层是纯的：不吃会话、不碰磁盘、不认识句柄。它只回答
「这段文本里，第几到第几个字符，看起来是什么，有多可信，能不能替换」。

`replaceable` 只是快照（写在 :class:`~core.ice.types.Finding` 上方便调用方读）。
真正分配句柄之前，:mod:`core.ice.pseudonym` 会再判一次同一个谓词
（:func:`core.ice.rules.is_replaceable`）—— 两处同源，不是两套逻辑。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import List, Sequence, Tuple

from core.ice.rules import IceRuleError, builtin_rules, is_replaceable, rule_index, validate
from core.ice.types import Confidence, Finding, Rule


@lru_cache(maxsize=256)
def _compile(pattern: str) -> "re.Pattern[str]":
    """编译缓存。

    必须走 `lru_cache`：census 读者会抓「模块级且被写过的容器」，
    写成 _COMPILED: dict = {} 再在函数里 _COMPILED[p] = ...，
    `ast.walk` 扫的是整棵树（不只看模块体），那条测试当场红。
    """
    return re.compile(pattern)


#: 置信度的排序权重（越小越优先）。
_CONF_RANK = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}


def _resolve_overlaps(findings: List[Finding], order) -> List[Finding]:
    """重叠消解 —— 保证返回值两两不重叠。

    判据（按序）：**起点靠前的先占位；起点相同，置信度高的先占位；再相同，
    声明在前的规则先占位。**

    为什么非有不可：一条有效的 18 位身份证有约 10.6% 的概率同时过 Luhn
    （`11010519491231` 打头的一千个顺序码里有 106 个两边都过）。
    银行卡正则 `[0-9]{16,19}` 一样命中那 18 位，不消解就会对同一段产出两条 finding
伪名化时两次替换互相错位，还原回来的串多一个或少一个字符。

    为什么置信度要排在规则声明之前：否则一个"形状像身份证、校验位不过"（LOW）
    的 18 位数会把同一段里真正过 Luhn（HIGH）的那个读数压掉 ——
    放着能替换的不用，去用不能替换的，等于白丢一层保护。

    以当前三条规则，重叠只可能发生在完全相同的 span 上
    （两条正则的前后 lookaround 都要求"整段数字"），真正起作用的是后两级排序。
    「左边先占位」那条是给未来规则留的，它是个启发式：两个不同起点的重叠命中
    仍可能出现"左边那个吃掉右边那个"的取舍。
    """
    ordered = sorted(findings, key=lambda f: (
        f.start,
        _CONF_RANK.get(f.confidence, 9),
        order.get(f.rule_id, 0),
    ))
    out: List[Finding] = []
    last_end = -1
    for f in ordered:
        if f.start >= last_end:
            out.append(f)
            last_end = f.end
    return out


def detect_pii(
    text: str, *, rules: Sequence[Rule] | None = None
) -> List[Finding]:
    """扫一遍 *text*，返回按位置升序、两两不重叠的命中列表。

    *rules* 为 `None`，内置三条（见 :func:`core.ice.rules.builtin_rules`）。

    返回值里带真值（`Finding.value`）。它不进 `repr`，但调用方
    不要把整个对象塞进日志 —— 要记就记 `Finding.display`（只有规则名/位置/置信度）。
    """
    if not text:
        return []
    if rules is None:
        rules = builtin_rules()
    if not rules:
        return []

    found: List[Finding] = []
    for rule in rules:
        # 关键词预筛：一个都没出现，这条规则不用跑。不改语义，只省时间。
        if rule.keywords and not any(k in text for k in rule.keywords):
            continue

        rx = _compile(rule.regex)
        for m in rx.finditer(text):
            try:
                value = m.group(rule.secret_group)
                start, end = m.start(rule.secret_group), m.end(rule.secret_group)
            except (IndexError, re.error) as exc:  # pragma: no cover
                # 装载期已经卡过组号越界；这里是第二道（规则可能是手搓的 Rule 对象）。
                raise IceRuleError(
                    "规则 %r 的 secretGroup=%d 在这个正则上取不到值：%s"
                    % (rule.id, rule.secret_group, exc)) from exc
            if not value:
                continue

            conf = validate(rule, value)
            found.append(Finding(
                rule_id=rule.id, start=start, end=end, value=value,
                confidence=conf, replaceable=is_replaceable(rule, conf),
            ))

    return _resolve_overlaps(found, rule_index(rules))


def detect_summary(findings: Sequence[Finding]) -> str:
    """一行可进日志的摘要。只给规则名与条数，绝不给真值。"""
    if not findings:
        return "none"
    counts: Tuple[str, ...] = tuple(
        "%s=%d" % (rid, sum(1 for f in findings if f.rule_id == rid))
        for rid in sorted({f.rule_id for f in findings})
    )
    return " ".join(counts)
