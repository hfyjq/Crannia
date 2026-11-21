"""工具结局在界面上的说法。

词表在 crannia 那边（`core/tool_outcome.py`）：`outcome` 五选一决定颜色，
`reason` 细分决定文案。这里只做翻译，不另造语义，也不从回执正文里猜 ——
修前 TUI 靠拉轨迹正文认"被拒绝"，正是要撤掉的那条路。

两个白点例外：`rejected` 下的 `cancelled`（用户打断并补充）与 `recused`
（框架没办成）都不是"用户说不行"，所以不染红。
"""

from __future__ import annotations

from .i18n import t

#: 结局 → 圆点颜色。`unknown` 不是后端给的：它表示"这一轮结束了，
#: 但那个工具的结束事件没来"（`ToolCard.orphan`）。
DOT = {
    "completed": "green",
    "waiting": "yellow",
    "error": "white",
    "denied": "red",
    "rejected": "red",
    "unknown": "yellow",
}

#: `rejected` 里这两条用白点。
_WHITE_REJECTED = {"cancelled", "recused"}

#: (结局, 细分) → 卡片括号里那句话。细分缺省时用 (结局, "") 那一行。
_LABELS = {
    ("completed", ""): "已完成",
    ("waiting", ""): "待审批",
    ("error", ""): "失败",
    ("error", "failed"): "失败",
    ("error", "refused"): "未执行",
    ("denied", ""): "自动审查拦截",
    ("denied", "auto-review-blocked"): "自动审查拦截",
    ("rejected", ""): "被拒绝",
    ("rejected", "user"): "被拒绝",
    ("rejected", "unavailable"): "被拒绝",
    ("rejected", "auto-review-unavailable"): "被拒绝",
    ("rejected", "cancelled"): "未采纳",
    ("rejected", "recused"): "未执行",
}


def dot(outcome: str, reason: str = "") -> str:
    """这个结局的圆点颜色。"""
    if outcome == "rejected" and reason in _WHITE_REJECTED:
        return "white"
    return DOT.get(outcome or "", "yellow")


def label(outcome: str, reason: str = "") -> str:
    """这个结局在卡片括号里的那句话。

    认不出来的组合给"未完成"：那是实话，也不会让一个没跑成的调用
    看起来像跑完了。
    """
    return t(_LABELS.get((outcome or "", reason or ""))
             or _LABELS.get((outcome or "", ""))
             or "未完成")
