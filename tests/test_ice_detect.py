"""ICE 检测层：规则装载 · 三条校验器 · supports_encryption 闸 · 重叠消解 · GBK。

判据：检测可以错（代价一行噪声），打码不能错（代价用户数据被改坏）。

两条纪律，本文件自己遵守：
  * 输出一律 ASCII —— GBK 控制台上，非 ASCII 的输出会让整个运行崩，
    而崩掉的输出看起来像测试失败。
  * 身份证/银行卡的向量只许取自权威出处，不许现编。
    一个"应该合法"但实际非法的身份证号会让真实现被判红
    —— 拿假标准验真实现，红的是实现，而实现是对的。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ice import detect as D                                    # noqa: E402
from core.ice import rules as R                                     # noqa: E402
from core.ice.types import Confidence                               # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# 向量
# 权威样例（GB 11643 文档里那份 1949 年的示例号）；负对照 = 把校验位换掉。
ID_OK = "11010519491231002X"
ID_BAD = "110105194912310021"
ID_OK2 = "440524188001010014"
ID_BAD2 = "440524188001010015"
# 这一条 同时 过身份证校验与 Luhn（用来钉重叠消解）
ID_OVERLAP = "110105194912310150"

CARD_OK = "4111111111111111"
CARD_BAD = "4111111111111112"
CARD_OK2 = "4242424242424242"

# 154 不在号段表里，但形状是 1[3-9] + 9 位
MOBILE_OK = "13800138000"
MOBILE_BADPREFIX = "15412345678"

TEXT = "客户 %s ，卡号 %s 。" % (ID_OK, CARD_OK)


def _find(findings, rule_id):
    return [f for f in findings if f.rule_id == rule_id]


# 一、规则装载

def test_builtin_rules_ship_with_package():
    rules = R.builtin_rules()
    ids = tuple(r.id for r in rules)
    check("builtin has exactly 3 rules", len(rules) == 3, "got %d" % len(rules))
    check("builtin rule ids are the known three",
          ids == ("cn_id_card", "cn_bank_card", "cn_mobile"), str(ids))
    # 断言条数，不是"不抛异常" —— 只有前者能抓到"干净 clone 里加载出 0 条规则"
    #   （那会让整个检测静默失效，而什么都不报）
    p = R.builtin_path()
    check("builtin toml ships inside the package",
          p.is_file() and p.parent.name == "rules" and p.parent.parent.name == "ice",
          str(p))


def test_bad_regex_fails_loud():
    src = '[[rules]]\nid = "x"\nregex = "([unclosed"\n'
    try:
        R.rules_from_toml(src)
        check("bad regex raises IceRuleError", False, "no exception")
    except R.IceRuleError:
        check("bad regex raises IceRuleError", True)


def test_missing_id_fails_loud():
    for src in ('[[rules]]\nregex = "a"\n', "[[rules]]\nid = \"  \"\nregex = \"a\"\n"):
        try:
            R.rules_from_toml(src)
            check("missing/blank id raises", False, src)
        except R.IceRuleError:
            check("missing/blank id raises", True)


def test_duplicate_id_fails_loud():
    src = ('[[rules]]\nid = "a"\nregex = "a"\n'
           '[[rules]]\nid = "a"\nregex = "b"\n')
    try:
        R.rules_from_toml(src)
        check("duplicate id raises", False)
    except R.IceRuleError as exc:
        check("duplicate id raises", "a" in str(exc))


def test_secret_group_out_of_range_fails_loud():
    src = '[[rules]]\nid = "a"\nregex = "(x)"\nsecretGroup = 5\n'
    try:
        R.rules_from_toml(src)
        check("secretGroup out of range raises", False)
    except R.IceRuleError:
        check("secretGroup out of range raises", True)


def test_supports_encryption_defaults_false():
    src = '[[rules]]\nid = "a"\nregex = "[0-9]{4}"\n'
    (rule,) = R.rules_from_toml(src)
    check("supports_encryption defaults to False", rule.supports_encryption is False)
    check("undeclared rule is not replaceable",
          R.is_replaceable(rule, Confidence.HIGH) is False)


def test_non_bool_supports_encryption_is_rejected():
    # 只收真 bool：`"true"`（字符串）/ `1` / `0` / `"yes"` 都不算
    #   （同本仓 `destructive` 那条判据；`isinstance(True, int)` 是 True 那个坑）。
    for raw in ('"true"', '"yes"', "1", "0"):
        src = '[[rules]]\nid = "a"\nregex = "[0-9]{4}"\nsupports_encryption = %s\nvalidator = "luhn"\n' % raw
        try:
            R.rules_from_toml(src)
            # TOML 里 "true"（带引号）是字符串，1 是整数 —— 两者都该被拒。
            check("non-bool supports_encryption rejected (%s)" % raw, False, "accepted")
        except R.IceRuleError:
            check("non-bool supports_encryption rejected (%s)" % raw, True)


def test_declaring_encryption_without_a_useful_validator_is_rejected():
    # 声明"我要替换"、却给了个永远判不出高置信的校验器，当场拒。
    #   放过去的话这条规则一辈子只报不改，而配置上写着它在护着（标签在说谎）。
    src = ('[[rules]]\nid = "a"\nregex = "[0-9]{4}"\n'
           'supports_encryption = true\nvalidator = "none"\n')
    try:
        R.rules_from_toml(src)
        check("encryption without a real validator rejected", False, "accepted")
    except R.IceRuleError:
        check("encryption without a real validator rejected", True)


def test_unknown_validator_name_is_rejected_at_load():
    src = '[[rules]]\nid = "a"\nregex = "[0-9]{4}"\nvalidator = "magic"\n'
    try:
        R.rules_from_toml(src)
        check("unknown validator name rejected at load", False, "accepted")
    except R.IceRuleError:
        check("unknown validator name rejected at load", True)


# 二、三条校验器

def test_cn_id_checksum():
    check("valid id passes (canonical #1)", R.cn_id_ok(ID_OK) is True)
    check("valid id passes (canonical #2)", R.cn_id_ok(ID_OK2) is True)
    check("flipped check digit fails", R.cn_id_ok(ID_BAD) is False)
    check("flipped check digit fails (2)", R.cn_id_ok(ID_BAD2) is False)
    check("lowercase x accepted", R.cn_id_ok(ID_OK.lower()) is True)
    check("17 digits rejected", R.cn_id_ok(ID_OK[:17]) is False)
    check("19 digits rejected", R.cn_id_ok(ID_OK + "0") is False)


def test_luhn_checksum():
    check("valid card passes", R.luhn_ok(CARD_OK) is True)
    check("valid card passes (2)", R.luhn_ok(CARD_OK2) is True)
    check("flipped last digit fails", R.luhn_ok(CARD_BAD) is False)
    check("short number rejected", R.luhn_ok("41111111111") is False)
    check("non-digits rejected", R.luhn_ok("4111-1111-1111-1111") is False)


def test_cn_mobile_prefix_table():
    check("known prefix passes", R.cn_mobile_ok(MOBILE_OK) is True)
    check("unknown prefix fails", R.cn_mobile_ok(MOBILE_BADPREFIX) is False)
    check("12 digits rejected", R.cn_mobile_ok(MOBILE_OK + "0") is False)
    # 号段表不是恒真：它必须真的拦掉一些形状合法的号
    check("prefix table is not vacuous",
          len(R.CN_MOBILE_PREFIXES) < 100 and "154" not in R.CN_MOBILE_PREFIXES)


def test_unknown_validator_never_yields_high():
    rule = R.Rule(id="a", regex=".*", validator="magic")
    check("unknown validator -> LOW", R.validate(rule, "anything") is Confidence.LOW)
    check("unknown validator -> not replaceable",
          R.is_replaceable(rule, Confidence.HIGH) is False)


def test_filter_forces_not_replaceable():
    rule = R.Rule(id="a", regex=".*", validator="luhn",
                  supports_encryption=True, filter_expr='entropy(finding["secret"])')
    check("rule with filter is never replaceable",
          R.is_replaceable(rule, Confidence.HIGH) is False)


def test_gate_requires_high_confidence():
    rule = R.Rule(id="a", regex=".*", validator="luhn", supports_encryption=True)
    check("HIGH passes the gate", R.is_replaceable(rule, Confidence.HIGH) is True)
    check("MEDIUM is blocked", R.is_replaceable(rule, Confidence.MEDIUM) is False)
    check("LOW is blocked", R.is_replaceable(rule, Confidence.LOW) is False)


# 三、detect_pii

def test_detect_finds_id_and_card():
    got = D.detect_pii(TEXT)
    idf = _find(got, "cn_id_card")
    bcf = _find(got, "cn_bank_card")
    check("one id finding", len(idf) == 1, str(len(idf)))
    check("one bank-card finding", len(bcf) == 1, str(len(bcf)))
    check("id is HIGH", idf and idf[0].confidence is Confidence.HIGH)
    check("id is replaceable", idf and idf[0].replaceable is True)
    check("spans line up with the text",
          idf and TEXT[idf[0].start:idf[0].end] == ID_OK)


def test_mobile_is_reported_but_never_replaced():
    got = D.detect_pii(MOBILE_OK)
    mob = _find(got, "cn_mobile")
    check("mobile is detected", len(mob) == 1)
    check("mobile is HIGH by the prefix table",
          mob and mob[0].confidence is Confidence.HIGH)
    # 这是有意的：手机号没有校验位，判据要求只有高置信才替换，只报不改。
    check("mobile is NOT replaceable", mob and mob[0].replaceable is False)


def test_low_confidence_id_is_reported_but_not_replaceable():
    got = D.detect_pii(ID_BAD)
    idf = _find(got, "cn_id_card")
    check("bad-checksum id still reported", len(idf) == 1)
    check("bad-checksum id is LOW", idf and idf[0].confidence is Confidence.LOW)
    check("bad-checksum id is not replaceable", idf and idf[0].replaceable is False)


def test_overlapping_checksums_yield_exactly_one_finding():
    # ID_OVERLAP 同时过身份证校验与 Luhn，两条规则都命中同一段。
    got = D.detect_pii(ID_OVERLAP)
    check("overlap collapses to exactly one finding", len(got) == 1, str(len(got)))
    check("the declaration-first rule wins the tie",
          got and got[0].rule_id == "cn_id_card", got[0].rule_id if got else "-")


def test_span_boundaries_do_not_carve_an_id_out_of_a_longer_run():
    # 前面多一位数字，前后 lookaround 必须拦住，不许从 19 位里切出 18 位
    got = D.detect_pii("9" + ID_OK)
    check("no id carved out of a 19-digit run", not _find(got, "cn_id_card"))


def test_fullwidth_digits_do_not_match():
    fw = "".join(chr(0xFF10 + int(c)) if c.isdigit() else c for c in ID_OK)
    got = D.detect_pii(fw)
    check("fullwidth digits are not digits", not _find(got, "cn_id_card"))


def test_keywords_prefilter_skips_the_rule():
    rule = R.Rule(id="k", regex="[0-9]{4}", keywords=("NEEDLE",))
    check("keyword missing -> rule skipped", D.detect_pii("1234", rules=(rule,)) == [])
    check("keyword present -> rule runs",
          len(D.detect_pii("NEEDLE 1234", rules=(rule,))) == 1)


def test_secret_group_selects_the_captured_span():
    rule = R.Rule(id="g", regex=r"ID:([0-9]{4})", secret_group=1)
    got = D.detect_pii("ID:9876", rules=(rule,))
    check("group 1 is the reported span",
          got and got[0].value == "9876" and (got[0].start, got[0].end) == (3, 7))


def test_finding_display_carries_no_value():
    got = D.detect_pii(TEXT)
    check("display has no true value",
          all(f.value not in f.display for f in got))
    check("summary has no true value",
          all(f.value not in D.detect_summary(got) for f in got))


def test_empty_text_is_empty():
    check("empty string -> []", D.detect_pii("") == [])
    check("no rules -> []", D.detect_pii(TEXT, rules=()) == [])


# 四、负对照（证明上面那些用例真的有分辨力）

def test_nc1_without_overlap_resolution_the_overlap_case_yields_two():
    """把消解拆掉，必须得到 2 条。

    这一条证明 `test_overlapping_checksums_yield_exactly_one_finding` 不是恒真的
    ——"1 条"只有在消解真的在工作时才成立。
    """
    original = D._resolve_overlaps
    D._resolve_overlaps = lambda fs, order: sorted(fs, key=lambda f: f.start)
    try:
        got = D.detect_pii(ID_OVERLAP)
    finally:
        D._resolve_overlaps = original
    check("NC1: without resolution the overlap yields 2", len(got) == 2, str(len(got)))


def test_nc2_forcing_the_gate_open_makes_low_confidence_replaceable():
    """把闸按下去，低置信的那条必须变得可以替换。

    这一条证明 `replaceable` 是闸算出来的，不是某种恒真的默认值。
    """
    original = D.is_replaceable
    D.is_replaceable = lambda rule, conf: True
    try:
        got = D.detect_pii(ID_BAD)
    finally:
        D.is_replaceable = original
    check("NC2: gate forced open flips replaceable",
          got and got[0].replaceable is True)


# 五、GBK —— 本模块的字面量一个都不许让控制台崩

def test_ice_module_literals_are_gbk_safe():
    """`core/ice/*.py` 里非 docstring 的字面量必须全部 GBK 可编码。

    扫那一处的那台机器只看 `tests/` + `scripts/`（`core/` 要 `--all` 才扫），
    所以这一条给 core/ice 单开。
    例：rules.py 的报错消息里出现 `，`（U+21D2）——
    GBK 编不出来，一旦真的抛出来就会崩在报错那行，而崩掉的输出看起来像别的问题。
    """
    import ast
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_gbk_safe import scan

    root = Path(__file__).resolve().parent.parent / "core" / "ice"
    files = sorted(root.rglob("*.py"))
    check("found the ice package to scan", len(files) >= 7, str(len(files)))

    bad = {}
    for p in files:
        res = scan(p)
        if res is None:
            continue
        crash, _doc = res
        if crash:
            bad[p.name] = crash
    detail = ", ".join("%s(%d)" % (k, len(v)) for k, v in sorted(bad.items()))
    check("no non-GBK literals in core/ice", not bad, detail)


def test_a_decimal_fraction_is_not_a_card_number():
    """小数点后的一串数字不是银行卡。

    症状：ICE 把 `0.0330999999999992` 判成 `cn_bank_card`（高置信、可替换）
    => 模型读到的是 `0.【PII:1】` —— 那是用户的数据被改坏，
    而判据是「打码不能错（代价用户数据被改坏）」。

    影响面：`workspace/表格1/ET.xlsx` 169,699 个浮点格里
    1,817 个被判成 PII，其中 11 个会被真的换掉（= 数据被改坏）；
    修正后 0 / 0。
    两个数分开报：`replaceable` 才是"改坏"，其余只是判定。

    判据是通用的：号段边界要排除「它是某个数字字面量的一部分」，
    而数字字面量由数字 + 小数点构成。
    """
    for label, s in (
        ("real-machine value", "0.0330999999999992"),
        ("negative fraction", "-0.0083000000000002"),
        ("card as the fraction", "0.%s" % CARD_OK2),
        ("card with cents", "%s.78" % CARD_OK),
    ):
        got = D.detect_pii(s)
        check("no finding in %s (%r)" % (label, s), got == [], str(got))

    # 负对照：独立的卡号仍然必须命中 —— 少了它，把规则整个删掉也能全绿。
    got = D.detect_pii("卡号 %s 请核对" % CARD_OK)
    check("a standalone card is still found",
          len(_find(got, "cn_bank_card")) == 1, str(got))
    # 英文句末那个句号放行（左右不对称是有意的：后面的 "." 两种含义都可能）
    got = D.detect_pii("Card number %s. Please verify." % CARD_OK)
    check("a card followed by a sentence period is still found",
          len(_find(got, "cn_bank_card")) == 1, str(got))


def main() -> int:
    print("=" * 62)
    print("ICE detect: rules / validators / gate / overlap / GBK")
    print("=" * 62)
    test_builtin_rules_ship_with_package()
    test_bad_regex_fails_loud()
    test_missing_id_fails_loud()
    test_duplicate_id_fails_loud()
    test_secret_group_out_of_range_fails_loud()
    test_supports_encryption_defaults_false()
    test_non_bool_supports_encryption_is_rejected()
    test_declaring_encryption_without_a_useful_validator_is_rejected()
    test_unknown_validator_name_is_rejected_at_load()
    test_cn_id_checksum()
    test_luhn_checksum()
    test_cn_mobile_prefix_table()
    test_unknown_validator_never_yields_high()
    test_filter_forces_not_replaceable()
    test_gate_requires_high_confidence()
    test_detect_finds_id_and_card()
    test_mobile_is_reported_but_never_replaced()
    test_low_confidence_id_is_reported_but_not_replaceable()
    test_overlapping_checksums_yield_exactly_one_finding()
    test_span_boundaries_do_not_carve_an_id_out_of_a_longer_run()
    test_a_decimal_fraction_is_not_a_card_number()
    test_fullwidth_digits_do_not_match()
    test_keywords_prefilter_skips_the_rule()
    test_secret_group_selects_the_captured_span()
    test_finding_display_carries_no_value()
    test_empty_text_is_empty()
    test_nc1_without_overlap_resolution_the_overlap_case_yields_two()
    test_nc2_forcing_the_gate_open_makes_low_confidence_replaceable()
    test_ice_module_literals_are_gbk_safe()

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
