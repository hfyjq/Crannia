"""规则装载 + 校验器 + `supports_encryption` 闸。

三条规矩：

1. 装载 fail loud。坏正则 / 缺 `id` / `secretGroup` 越界，抛
   :class:`IceRuleError`，绝不静默跳过。静默跳过的代价是：
   用户以为有一条规则在护着，其实那条根本没装上，而没有任何东西会红。
2. 认不出的校验器判 LOW。不认识的 `validator` 不许冒充"通过"。
3. 闸只有一个实现（:func:`is_replaceable`），且两处调用同一个谓词
   —— `detect` 用它填 `Finding.replaceable`，`pseudonym` 在真的分配句柄前再判一次。
   两处各写一套 = "一个判定两处漂移"，而漂移的方向通常是更松。
"""

from __future__ import annotations

import re
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from core.ice.types import Confidence, Rule


class IceRuleError(ValueError):
    """规则坏了。加载期就抛 —— 见模块 docstring 第 1 条。"""


# 校验器（纯函数，只吃 ASCII 数字）

#: ISO 7064 mod 11-2（GB 11643 身份证校验位）的加权因子与校验码表。
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"

#: 中国大陆移动号段前三位。
#: 这是一份尽力而为的快照，不保证与现行放号一致。
#: 漏掉的后果是漏检（漏检不管），方向是安全的；
#: 所以宁可窄一点也不要宽 —— 宽了才会把非手机号的 11 位数当成手机号。
CN_MOBILE_PREFIXES = frozenset((
    # 13x（移动 / 联通）
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    # 14x（数据 / 上网卡）
    "145", "146", "147", "148", "149",
    # 15x
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    # 16x（虚拟运营商 / 物联网）
    "162", "165", "166", "167",
    # 17x
    "170", "171", "172", "173", "174", "175", "176", "177", "178",
    # 18x
    "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    # 19x（含广电 192）
    "190", "191", "192", "193", "195", "196", "197", "198", "199",
))

#: 校验器名 → 实现。表里没有的名字，LOW（第 2 条规矩）。
VALIDATORS = ("none", "luhn", "cn_id_mod11_2", "cn_mobile")


def luhn_ok(value: str) -> bool:
    """Luhn（mod 10）校验。银行卡用。

    只吃 ASCII 数字 —— `str.isdigit()` 对阿拉伯-印度数字也返回真，
    那种串进来会静默算错（`int()` 反而能吃），所以先卡字符集。

    判据与边界用例取自 Presidio 的 `generic/credit_card_recognizer.py`
    （借判据，不借依赖 —— 引它要拖一整条 spacy 链）。
    """
    digits = [c for c in value if c in "0123456789"]
    if len(digits) != len(value) or len(digits) < 12:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def cn_id_ok(value: str) -> bool:
    """身份证 18 位的 ISO 7064 mod 11-2 校验位。

    只做校验位，不做行政区/生日合法性 —— 那是另一层，本版不做。
    """
    if len(value) != 18:
        return False
    body, last = value[:17], value[17:].upper()
    if any(c not in "0123456789" for c in body):
        return False
    if last not in "0123456789X":
        return False
    total = 0
    for ch, w in zip(body, _ID_WEIGHTS):
        total += (ord(ch) - 48) * w
    return _ID_CHECK[total % 11] == last


def cn_mobile_ok(value: str) -> bool:
    """11 位 + 前三位在号段表里。这不是校验位，是"看起来像"。"""
    if len(value) != 11:
        return False
    if any(c not in "0123456789" for c in value):
        return False
    return value[:3] in CN_MOBILE_PREFIXES


_VALIDATOR_FNS = {
    "luhn": luhn_ok,
    "cn_id_mod11_2": cn_id_ok,
    "cn_mobile": cn_mobile_ok,
}


def validate(rule: Rule, value: str) -> Confidence:
    """这条规则对这个值给出多高的置信度。

    `"none"` 与任何没见过的 `validator` 一律 :attr:`Confidence.LOW`
    —— 认不出就不许冒充通过（模块 docstring 第 2 条）。
    """
    fn = _VALIDATOR_FNS.get(rule.validator)
    if fn is None:
        return Confidence.LOW
    return Confidence.HIGH if fn(value) else Confidence.LOW


def is_replaceable(rule: Rule, confidence: Confidence) -> bool:
    """闸的唯一实现（同名字段的同义物）。

    三件事同时成立才允许替换：

    1. 规则声明了 `supports_encryption`（缺省 false = 只报不改）；
    2. 规则没有 `filter` —— 本版不求值那个表达式，所以带它的规则一律不换，
       免得出现"以为过滤过了，其实那表达式从没跑过"；
    3. 置信度是 HIGH —— 也就是校验位真的通过了。
    """
    if not rule.supports_encryption:
        return False
    if rule.filter_expr:
        return False
    return confidence is Confidence.HIGH


# 装载

def _bad(msg: str) -> IceRuleError:
    return IceRuleError(msg)


def rules_from_toml(text: str, *, source: str = "builtin") -> Tuple[Rule, ...]:
    """从 TOML 文本装载规则。任何一处坏，整体抛（不做部分装载）。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _bad("TOML 解析失败：%s" % exc) from exc
    raw = data.get("rules")
    if not isinstance(raw, list) or not raw:
        raise _bad("TOML 里没有 [[rules]] 条目")

    out = []
    seen: Dict[str, int] = {}
    for idx, item in enumerate(raw):
        out.append(_one_rule(item, idx, source, seen))
    return tuple(out)


def _one_rule(item: Any, idx: int, source: str, seen: Dict[str, int]) -> Rule:
    where = "rules[%d]" % idx
    if not isinstance(item, Mapping):
        raise _bad("%s 不是一张表" % where)
    rid = item.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise _bad("%s 缺 id" % where)
    rid = rid.strip()
    if rid in seen:
        raise _bad("id 重复：%r（第 %d 条与第 %d 条）" % (rid, seen[rid], idx))
    seen[rid] = idx

    pattern = item.get("regex")
    if not isinstance(pattern, str) or not pattern:
        raise _bad("%s（%s）缺 regex" % (where, rid))
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise _bad("%s（%s）正则编不出来：%s" % (where, rid, exc)) from exc

    group = item.get("secretGroup", 0)
    if isinstance(group, bool) or not isinstance(group, int):
        raise _bad("%s（%s）secretGroup 必须是整数，实得 %r" % (where, rid, group))
    if group < 0 or group > compiled.groups:
        raise _bad("%s（%s）secretGroup=%d 越界（该正则有 %d 个捕获组）"
                   % (where, rid, group, compiled.groups))

    keywords = item.get("keywords", [])
    if isinstance(keywords, str):
        keywords = [keywords]
    if not isinstance(keywords, list) or any(not isinstance(k, str) for k in keywords):
        raise _bad("%s（%s）keywords 必须是一串字符串" % (where, rid))

    validator = item.get("validator", "none")
    if not isinstance(validator, str):
        raise _bad("%s（%s）validator 必须是字符串" % (where, rid))
    if validator not in VALIDATORS:
        raise _bad("%s（%s）不认识的 validator %r（只认 %s）"
                   % (where, rid, validator, " / ".join(VALIDATORS)))

    # 只收真 bool：`"true"` / `1` / `"yes"` 都不算（同 `destructive` 那条判据）。
    supports = item.get("supports_encryption", False)
    if not isinstance(supports, bool):
        raise _bad("%s（%s）supports_encryption 必须是布尔，实得 %r"
                   % (where, rid, supports))
    # 声明要替换、却没有任何能判 HIGH 的校验器，当场说清楚。
    #   放过去的话，这条规则会永远只报不改：配置说"我在护着"、行为啥也没干，
    #   而两者都不出声 —— 正是「标签在说谎」的形状。
    if supports and validator not in _VALIDATOR_FNS:
        raise _bad("%s（%s）声明了 supports_encryption=true，但 validator=%r "
                   "永远判不出高置信，这条规则一辈子不会替换。要么给它一个校验器，"
                   "要么把 supports_encryption 写成 false。" % (where, rid, validator))

    filter_expr = item.get("filter", "")
    if not isinstance(filter_expr, str):
        raise _bad("%s（%s）filter 必须是字符串" % (where, rid))

    desc = item.get("description", "")
    if not isinstance(desc, str):
        raise _bad("%s（%s）description 必须是字符串" % (where, rid))

    return Rule(
        id=rid, description=desc, regex=pattern, secret_group=group,
        keywords=tuple(keywords), supports_encryption=supports,
        validator=validator, filter_expr=filter_expr, source=source,
    )


#: 内置规则的落点。随包（不是 `data/`）—— 理由见 `core/ice/rules/builtin.toml` 抬头。
_BUILTIN_PATH = Path(__file__).resolve().parent / "rules" / "builtin.toml"


@lru_cache(maxsize=1)
def builtin_rules() -> Tuple[Rule, ...]:
    """内置规则（进程内只读一次）。

    缓存用 `lru_cache` 不是模块级 dict —— census
    会抓「模块级且被写过的容器」，那种形状当场红。
    """
    return rules_from_toml(_BUILTIN_PATH.read_text(encoding="utf-8"),
                           source="builtin")


def builtin_path() -> Path:
    """内置规则文件的路径（测试直接断言它存在）。"""
    return _BUILTIN_PATH


def load_rules(path: Optional[str] = None) -> Tuple[Rule, ...]:
    """装载规则。path is None，内置那三条。"""
    if path is None:
        return builtin_rules()
    p = Path(path)
    return rules_from_toml(p.read_text(encoding="utf-8"), source="custom")


def rule_index(rules: Sequence[Rule]) -> Dict[str, int]:
    """id -> 声明顺序。重叠消解要用它（顺序即优先级）。"""
    return {r.id: i for i, r in enumerate(rules)}
