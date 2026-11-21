"""一次工具调用的结局 —— `EVT_TOOL_END` 的 `outcome` / `reason` 词表。

## 治的是什么

`EVT_TOOL_END` 原来只带 `ok`，而 `ok=True` 盖住了几种完全不同的情况：
顺利跑完、挂起等审批、自动审查拦下、用户拒绝、框架拒绝。
客户端只能猜：TUI 原来靠拉轨迹正文去认"被拒绝"，而"等审批"根本认不出来，
卡片会把一个什么都没执行的调用显示成"已完成"。

## 两格，不是一个

`outcome` 决定圆点颜色，`reason` 决定文案 —— 一个字段装不下：
同一个 `rejected` 下面有"未采纳 / 未执行 / 被拒绝"三句话，
同一个 `error` 下面有"失败 / 未执行"两句。

`reason` 永远存在，`completed` / `waiting` 时是空串。不要用"字段在不在"
表达"不适用"：那会让"读不到"和"没有"长得一样。

## 词表只此一处

`core/auto_review.py` 的 `OUTCOME_*` 是审计口径（只记"没跑成"的四种），
不抢它的名字 —— 这里负责把它们映射成事件口径，映射表在 `_BY_REVIEW`。

## 判据顺序（承重）

结算语义（调用方当场知道的）> 挂起 > 失败 > 拒绝 > 回执内容 > 完成。

结算语义排第一：结算器知道用户点了什么，回执只是它的渲染 ——
让一个已经知道答案的调用方去从文字里认，是在制造第二个真相源。
"""

from __future__ import annotations

from core import auto_review as _AR

#: 结局。客户端按它决定颜色。
OUTCOME_COMPLETED = "completed"
OUTCOME_WAITING = "waiting"
OUTCOME_ERROR = "error"
OUTCOME_DENIED = "denied"
OUTCOME_REJECTED = "rejected"

#: 细分。客户端按它决定文案。
REASON_FAILED = "failed"
REASON_REFUSED = "refused"
REASON_AUTO_REVIEW_BLOCKED = "auto-review-blocked"
REASON_AUTO_REVIEW_UNAVAILABLE = "auto-review-unavailable"
REASON_CANCELLED = "cancelled"
REASON_RECUSED = "recused"
REASON_USER = "user"
REASON_UNAVAILABLE = "unavailable"

#: 审批结算的四种语义 → 事件口径。四种都归 `rejected`（都没有得到批准），
#: 区别只在文案。颜色由客户端按 `reason` 再分一次：`cancelled` / `recused`
#: 是框架侧或"用户没说不"的形状，用白点；其余用红点。
_BY_SETTLEMENT = {
    "rejected": REASON_USER,
    "cancelled": REASON_CANCELLED,
    "unavailable": REASON_UNAVAILABLE,
    "recused": REASON_RECUSED,
}

#: 自动审查那条线的四个审计值 → 事件口径。
_BY_REVIEW = {
    _AR.OUTCOME_AUTO_REVIEW_BLOCKED: (OUTCOME_DENIED, REASON_AUTO_REVIEW_BLOCKED),
    _AR.OUTCOME_AUTO_REVIEW_UNAVAILABLE: (OUTCOME_REJECTED,
                                          REASON_AUTO_REVIEW_UNAVAILABLE),
    _AR.OUTCOME_REJECTED_BY_USER: (OUTCOME_REJECTED, REASON_USER),
    _AR.OUTCOME_PENDING_APPROVAL: (OUTCOME_WAITING, ""),
}


def of_settlement(token: str) -> tuple[str, str]:
    """审批结算语义 → `(outcome, reason)`。

    认不出来的 token 按"用户拒绝"收口：`outcome_line` 对未知语义直接抛，
    所以线上到不了这里；留这一手只是不想让事件层自己炸掉一轮。
    """
    return OUTCOME_REJECTED, _BY_SETTLEMENT.get(str(token or ""), REASON_USER)


def of_settlement_receipt(content) -> tuple[str, str]:
    """结算回执（信封 + `APPROVAL_HEAD` + `<token>（…）`）→ `(outcome, reason)`。"""
    return of_settlement(_AR.settlement_outcome_of(content))


def classify(content, *, failed: bool = False, refused: bool = False,
             pending: bool = False, settlement: str = "") -> tuple[str, str]:
    """算出一次调用的 `(outcome, reason)`。

    *failed* / *refused* / *pending* 由调用方按自己已经有的判据传入
    （它们都不是从回执正文里猜出来的）；*content* 只用于认回执头。
    """
    if settlement:
        return of_settlement(settlement)
    if pending:
        return OUTCOME_WAITING, ""
    if failed:
        return OUTCOME_ERROR, REASON_FAILED
    if refused:
        return OUTCOME_ERROR, REASON_REFUSED
    token = _AR.settlement_outcome_of(content)
    if token in _BY_SETTLEMENT:
        return OUTCOME_REJECTED, _BY_SETTLEMENT[token]
    review = _AR.outcome_of_tool_result(content)
    if review in _BY_REVIEW:
        return _BY_REVIEW[review]
    return OUTCOME_COMPLETED, ""
