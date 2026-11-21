"""`EVT_TOOL_END` 的 outcome / reason 词表。

钉住四件事：

  1. 每个结算语义都映射到固定的一对 (outcome, reason)；
  2. 四种"没跑成"各有各的 reason，不许塌成一个 ——
     客户端靠它分"未采纳 / 未执行 / 被拒绝"三句话；
  3. `reason` 永远是字符串（空串 = 不适用），不是 None、也不是缺键；
  4. 判据顺序：结算 > 挂起 > 失败 > 拒绝 > 回执内容 > 完成。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_tool_outcome.py
"""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.auto_review as AR  # noqa: E402
from core import tool_outcome as T  # noqa: E402
from core.approval import outcome_line, pending_payload  # noqa: E402

_N = 0


def check(cond: bool, msg: str) -> None:
    global _N
    _N += 1
    if not cond:
        raise AssertionError("#%d %s" % (_N, msg))


def _receipt(token: str) -> str:
    """与结算器同形：`[系统] ` 信封 + `outcome_line` 那句头。"""
    return "[系统] " + outcome_line(token, "route")


def test_completed_is_the_default():
    check(T.classify("plain text") == (T.OUTCOME_COMPLETED, ""),
          "普通回执必须落 completed / 空 reason")
    check(T.classify(None) == (T.OUTCOME_COMPLETED, ""), "None 也落 completed")
    print("  [ok] default = completed")


def test_flags_take_precedence():
    check(T.classify("x", failed=True) == (T.OUTCOME_ERROR, T.REASON_FAILED),
          "failed -> error/failed")
    check(T.classify("x", refused=True) == (T.OUTCOME_ERROR, T.REASON_REFUSED),
          "refused -> error/refused")
    check(T.classify("x", pending=True) == (T.OUTCOME_WAITING, ""),
          "pending -> waiting/空")
    # 两者不会同时出现；顺序钉住，免得以后谁把拒绝挪到失败前面而没人发现。
    check(T.classify("x", failed=True, refused=True)[1] == T.REASON_FAILED,
          "failed 必须压过 refused")
    print("  [ok] flags: failed / refused / pending")


def test_settlement_tokens_map_one_to_one():
    table = [("rejected", T.REASON_USER),
             ("cancelled", T.REASON_CANCELLED),
             ("unavailable", T.REASON_UNAVAILABLE),
             ("recused", T.REASON_RECUSED)]
    for token, reason in table:
        check(T.of_settlement(token) == (T.OUTCOME_REJECTED, reason),
              "settlement %s -> rejected/%s" % (token, reason))
        # 从回执正文认，结果必须一样：[系统] 信封不许把它吃掉。
        check(T.of_settlement_receipt(_receipt(token)) ==
              (T.OUTCOME_REJECTED, reason),
              "receipt %s 没从正文里认出来" % token)
        check(T.classify(_receipt(token)) == (T.OUTCOME_REJECTED, reason),
              "classify 没认出 %s 的回执" % token)
    print("  [ok] settlement: 4 tokens -> 4 reasons")


def test_unknown_settlement_does_not_crash():
    check(T.of_settlement("nonsense") == (T.OUTCOME_REJECTED, T.REASON_USER),
          "未知 token 按用户拒绝收口（线上不可达，但不许炸）")
    check(AR.settlement_outcome_of("plain text") == "",
          "不是回执就该回空串，不许猜")
    print("  [ok] unknown token -> rejected/user (no crash)")


def test_review_receipts():
    check(T.classify(AR.AUTO_BLOCK_HEAD) ==
          (T.OUTCOME_DENIED, T.REASON_AUTO_REVIEW_BLOCKED),
          "自动审查拦下 -> denied/auto-review-blocked")
    check(T.classify(AR.AUTO_UNAVAILABLE_HEAD)[0] == T.OUTCOME_REJECTED,
          "判定没作出 -> rejected")
    check(T.classify(AR.AUTO_UNAVAILABLE_HEAD)[1] ==
          T.REASON_AUTO_REVIEW_UNAVAILABLE,
          "判定没作出 -> reason=auto-review-unavailable")
    # run_command 那条会被包进隔离包裹（`<user_document>`）—— 不许被包挡住。
    wrapped = ('<user_document trust="untrusted">\n' + AR.AUTO_BLOCK_HEAD +
               "\n</user_document>")
    check(T.classify(wrapped)[0] == T.OUTCOME_DENIED,
          "被隔离包裹的自动审查回执也必须认出来")
    print("  [ok] auto-review receipts (plain + wrapped)")


def test_pending_payload_content():
    body = json.dumps(pending_payload("command"), ensure_ascii=False)
    check(T.classify(body) == (T.OUTCOME_WAITING, ""),
          "待审批载荷 -> waiting（不是 completed）")
    print("  [ok] pending payload -> waiting")


def test_reason_is_always_a_string():
    samples = [
        T.classify("x"),
        T.classify("x", failed=True),
        T.classify("x", refused=True),
        T.classify("x", pending=True),
        T.classify("x", settlement="cancelled"),
        T.of_settlement("recused"),
        T.of_settlement_receipt(_receipt("unavailable")),
    ]
    for outcome, reason in samples:
        check(isinstance(outcome, str) and bool(outcome), "outcome 必须是非空串")
        check(isinstance(reason, str), "reason 必须是字符串（空串 = 不适用）")
    print("  [ok] reason always a string")


def main() -> int:
    test_completed_is_the_default()
    test_flags_take_precedence()
    test_settlement_tokens_map_one_to_one()
    test_unknown_settlement_does_not_crash()
    test_review_receipts()
    test_pending_payload_content()
    test_reason_is_always_a_string()
    print("\n" + "=" * 54)
    # 汇总行走主 runner 认识的格式（`N/M passed`）；`checks:` 那一种它读不出来，
    # 会被报成"跑不出汇总"（读不到 ≠ 通过）。
    print("%d/%d passed" % (_N, _N))
    return 0


if __name__ == "__main__":
    sys.exit(main())
