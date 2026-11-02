"""提问面板 —— 模型主动向用户出一个结构化选择题。

## 它是什么

一个工具 `ask_user(question, header, options, multiSelect)`：模型把一件需要人决定
的事摆成 2–4 个选项（每项一句标签 + 一句说明），用户点一下，答案**作为这次调用的
工具结果**回到模型手上 —— 不是"在回复里写一段话，让用户自己打字回"。

它与「审批面板」是两种形状：
一个问意见、一个求授权 —— 选项归属 / 主体内容 / 逃生口三项全不同。
不统一成一个东西，只共用那条挂起/结算/重放的地基：
  · 审批 = 用户答完，发一张授权，工具重放后真的去做
  · 提问 = 用户答完，答案就是结果，不重放，直接把答案补回那次调用
    （`ctx.approval_replay["result"]`）

## 回传契约

用户没有选，而是想先聊 —— 那时回给模型的不是 "rejected"（那是一个确定的
错误语义，会让模型停手），而是：

    用户没有做完选择，有想讨论的地方，这意味着他们可能对你有额外的信息、
    背景或问题。请考虑他们的回答，必要时重新表述问题。首先询问他们希望讨论什么内容。

三条语义必须分得开：
`rejected`（人说不行）· `unavailable`（没人可问）· 这里：人还没决定。

## 通道

· Web/GUI：浮窗点选（`packages/ui/src/AskUser.jsx`）
· IM：提示语里带编号菜单，用户回数字（与审批共用那套数字契约）
· 子 agent / 定时任务：不许挂起（没有"回答变成新的一轮"那一轮），派发前拒
"""

from __future__ import annotations

from core.i18n import t as tr

__all__ = [
    "TOOL_NAME", "MAX_OPTIONS", "MIN_OPTIONS", "MAX_QUESTION", "MAX_HEADER",
    "MAX_LABEL", "MAX_DESCRIPTION", "AskUserError", "validate",
    "pending_payload", "answer_text", "FREE_TEXT_NOTE", "render_body",
    "ask_user", "SCHEMA",
]

TOOL_NAME = "ask_user"

MIN_OPTIONS = 2
MAX_OPTIONS = 4

MAX_QUESTION = 500
MAX_HEADER = 12
MAX_LABEL = 40
MAX_DESCRIPTION = 120

#: 回传契约的原文 —— 别再改写。
FREE_TEXT_NOTE = (
    "用户没有做完选择，有想讨论的地方，这意味着他们可能对你有额外的信息、"
    "背景或问题。请考虑他们的回答，必要时重新表述问题。首先询问他们希望讨论什么内容。"
)


class AskUserError(ValueError):
    """参数不合法。消息点得出是哪一条判据（模型照着改就能过）。"""


def _text(raw, limit: int, what: str, *, required: bool = False) -> str:
    s = str(raw if raw is not None else "").strip()
    if required and not s:
        raise AskUserError("%s不能空" % what)
    if len(s) > limit:
        raise AskUserError("%s太长（%d 字 > 上限 %d 字）" % (what, len(s), limit))
    return s


def validate(args) -> dict:
    """工具参数 → 规范化的一份。纯函数（零 IO、零副作用）。

    返回 `{question, header, options:[{key,label,description}], multiSelect}`。
    不合规，`AskUserError`（拒，不静默夹取）。
    """
    if not isinstance(args, dict):
        raise AskUserError("参数要是对象（收到 %s）" % type(args).__name__)

    question = _text(args.get("question"), MAX_QUESTION, "问题", required=True)
    header = _text(args.get("header"), MAX_HEADER, "标题（header）")
    raw_options = args.get("options")
    if not isinstance(raw_options, (list, tuple)):
        raise AskUserError("options 要是数组（2–%d 项）" % MAX_OPTIONS)
    if not (MIN_OPTIONS <= len(raw_options) <= MAX_OPTIONS):
        raise AskUserError("选项要给 %d–%d 个（收到 %d 个）"
                           % (MIN_OPTIONS, MAX_OPTIONS, len(raw_options)))

    multi = args.get("multiSelect", False)
    #: 只收真布尔（`"true"` / `1` / `yes` 都不算）。
    if not isinstance(multi, bool):
        raise AskUserError("multiSelect 要是 true / false（收到 %r）" % (multi,))

    options: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_options):
        if not isinstance(raw, dict):
            raise AskUserError("第 %d 个选项不是对象（收到 %s）"
                               % (i + 1, type(raw).__name__))
        label = _text(raw.get("label"), MAX_LABEL, "第 %d 个选项的 label" % (i + 1),
                      required=True)
        if label in seen:
            raise AskUserError("选项重复：%r（两个选项长得一样，用户分不出该点哪个）"
                               % label)
        seen.add(label)
        options.append({
            "key": str(i + 1),
            "label": label,
            "description": _text(raw.get("description"), MAX_DESCRIPTION,
                                 "第 %d 个选项的 description" % (i + 1)),
        })

    return {"question": question, "header": header, "options": options,
            "multiSelect": multi}


def render_body(question: dict) -> str:
    """文本通道看到的那段正文（编号菜单 + 说明）。唯一一处拼它。"""
    lines = [question["question"]]
    for opt in question["options"]:
        line = "%s. %s" % (opt["key"], opt["label"])
        if opt["description"]:
            line += " —— " + opt["description"]
        lines.append(line)
    return "\n".join(lines)


def pending_payload(question: dict) -> dict:
    """工具第一次调用时返回的挂起载荷（dict）。

    与审批载荷同一条规矩：不许包裹、不许按字符截断 ——
    它是框架 ↔ 模型之间的协议消息，不是"读来的内容"。

    必须返回 dict，不是 JSON 串。
      工具的返回值会过 `file_tools.as_text`，而它只对 `PLAIN_TEXT_TOOLS`
      里的工具原样放行，其余一律 `json.dumps`，交一个字符串出去会被
      再编码一次（`"{\"status\": …}"`），于是
      `_parse_approval_payload`（要求正文以 `{` 开头）认不出来：
      挂起没发生、模型把选项转述成一段文字，面板永远不弹。
与 `core.approval.pending_payload` 同形（它一直返回 dict）。
    """
    body = render_body(question)
    prompt = body + "\n\n" + tr("回复数字即可；想先说点别的就直接说。")
    return {
        "status": "pending_user_question",
        "kind": "ask_user",
        "ask_index": 1,
        # 浮窗的渲染源（`build_payload` 直接用它，不自己重拼）。
        "body": body,
        # 文本通道的提示语（IM 上就是它）。
        "prompt": prompt,
        "question": question["question"],
        "header": question["header"],
        "options": question["options"],
        "multiSelect": question["multiSelect"],
    }


def ask_user(question: str = "", options=None, header: str = "",
             multiSelect: bool = False) -> dict:
    """工具入口 —— 只做校验 + 造挂起载荷（真正的挂起在 agent 循环里）。

    它永远不会拿到"用户的回答"：答案走 `ctx.approval_replay["result"]`
    直接补回那次调用，不重放本函数（见模块头那段）。
    """
    q = validate({"question": question, "header": header,
                  "options": options, "multiSelect": multiSelect})
    return pending_payload(q)


def answer_text(*, question: dict, selected: list[dict],
                free_text: str = "") -> str:
    """把用户的选择排成这次工具调用的结果（直接进模型上下文）。

    纯文本、不套 JSON 信封（工具回执的形状由读者决定；这里读者是模型，
      它要的是一段话，不是一个 schema）。
    """
    if free_text:
        return ("[用户没有选选项，补充了这些话]\n%s\n\n（%s）"
                % (free_text, FREE_TEXT_NOTE))
    if not selected:
        return ("[用户没有做出选择]\n（%s）" % FREE_TEXT_NOTE)
    if len(selected) == 1:
        opt = selected[0]
        body = opt["label"]
        if opt.get("description"):
            body += "\n（%s）" % opt["description"]
        return "[用户的选择]\n" + body
    rows = []
    for opt in selected:
        line = "- " + opt["label"]
        if opt.get("description"):
            line += "（%s）" % opt["description"]
        rows.append(line)
    return "[用户的选择（多选）]\n" + "\n".join(rows)


SCHEMA: dict = {
    "description": (
        "向用户提一个**结构化选择题**，等他选完再继续。"
        "★ 什么时候用：有 2–4 个真的不同、而你无法替他决定的方案（选哪个目录 / "
        "哪种口径 / 先做哪一件）；或者继续下去需要一条只有他知道的信息。"
        "★ 别用它做：能自己查的事、只是在回复里确认一下（那种直接说就行）。"
        "每个选项给一句 label + 一句 description；用户也可以不选、直接说别的。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": "要问用户的那句话（清楚、自足）"},
            "header": {"type": "string",
                       "description": "很短的小标题（最多 12 字）"},
            "options": {
                "type": "array",
                "description": "2–4 个选项，每项 {label, description}",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string",
                                  "description": "选项名（最多 40 字）"},
                        "description": {"type": "string",
                                        "description": "一句说明（最多 120 字）"},
                    },
                    "required": ["label"],
                },
            },
            "multiSelect": {
                "type": "boolean",
                "description": "允许同时选多个（默认 false）",
            },
        },
        "required": ["question", "options"],
    },
}
