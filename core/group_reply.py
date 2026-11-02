"""无主理人群聊的发言路由与互见摘要。

## 这个模块只做三件纯事

1. 点名解析 —— 用户消息里的 `@名字`（含 `@全员` / `@all`）；
2. 路由解析 —— 一次小 LLM 调用返回的 JSON（`responders` / `confidence`）；
3. 文本拼装 —— 路由器的输入、成员的系统块、成员的尾部、互见摘要。

真正的「调模型 / 跑成员 / 落库」在 `core/pipeline/stages/group_reply.py`。
把纯函数留在这里，是为了能逐条单测（`tests/test_group_reply.py`），
不必启动整条管线。

## 判据

- 无主理人 + 没有 @，优先一次小 LLM 调用判断谁合适；
- 路由低置信 / 空 / 失败，回退全员回复；
- 不新增 `skills` / `typical` 字段 —— 用户不一定明白怎么选 skill；
  路由只看现有的 `role`（职责）；
- 有 @，只让被点名的成员回复，不跑路由；
- 路由不是主理人：不说话、不汇总、不占成员位、不落库。
"""

from __future__ import annotations

import json
import re

#: `@` 后面这些词 = 全员（大小写不敏感；中文词按原样）。
ALL_MENTION_TOKENS = ("@all", "@everyone", "@所有人", "@全员", "@全体")

#: 摘要里每条结论最多保留多少字符。
DIGEST_ITEM_CHARS = 200


# ----------------------------------------------------------------------
# 点名
# ----------------------------------------------------------------------

def _member_names(group: dict) -> list[str]:
    return [str(m.get("name") or "") for m in (group or {}).get("members") or []
            if str(m.get("name") or "")]


def parse_mentions(text: str, names: list[str]) -> tuple[list[str], list[str]]:
    """从 *text* 里抽 `@名字`。

    返回 `(命中的名字, 没认出来的 @token)`。

    判据是最长名字优先 + 区间不重叠：
      `@worker10` 不该同时命中 `worker1`。中文名没有空格，不能靠 `\\b`；
      所以按长度从长到短扫，命中过的区间不再参与。
    `@全员` / `@all` 这类在 `ALL_MENTION_TOKENS` 里 —— 由调用方展开成全员。
    """
    src = str(text or "")
    low = src.lower()
    if any(tok in low for tok in ALL_MENTION_TOKENS):
        return list(dict.fromkeys(names)), []

    spans: list[tuple[int, int]] = []
    hits: list[str] = []
    for name in sorted(set(names), key=len, reverse=True):
        token = "@" + name
        start = 0
        while True:
            i = src.find(token, start)
            if i < 0:
                break
            j = i + len(token)
            if not any(not (j <= a or i >= b) for a, b in spans):
                spans.append((i, j))
                if name not in hits:
                    hits.append(name)
            start = j

    # 没认出来的 @token（只用于「用户可能打错了名字」那条提示）。
    unknown: list[str] = []
    for m in re.finditer(r"@[^\s@，。！？、,!.?：:；;]{1,40}", src):
        tok = m.group(0)
        if any(tok == "@" + n for n in names):
            continue
        if tok.lower() in ALL_MENTION_TOKENS:
            continue
        # 落在已命中区间里的跳过（`@worker1` 是 `@worker10` 的前缀时）。
        if any(not (m.end() <= a or m.start() >= b) for a, b in spans):
            continue
        unknown.append(tok)
    return hits, unknown


# ----------------------------------------------------------------------
# 路由器
# ----------------------------------------------------------------------

ROUTER_SYSTEM_PROMPT = (
    "你是群聊的分派员，不是发言人。你的唯一任务是判断用户这条消息应该由哪些成员回复。\n"
    "只输出一个 JSON 对象，不要输出任何解释、Markdown 或代码围栏。\n"
    "格式：{\"responders\": [\"名字\"], \"confidence\": \"high|low\", \"reason\": \"一句话\"}\n"
    "规则：\n"
    "- 只从名册里选名字；不在名册里的名字不要写；\n"
    "- 宁少勿多：只选真正相关的人；不相关的人不要选；\n"
    "- 如果问题是综合性的、跨多人职责的，或你不确定，confidence 写 low；\n"
    "- 如果用户说「大家 / 全员 / 所有人」，把名册里所有人都选上；\n"
    "- 如果用户是在追问上一位发言人，优先选上一位；\n"
    "- 不要替成员回答，只做分派。"
)


def roster_text(group: dict) -> str:
    """名册：`名字 — 职责`。职责为空时也写出来（用户能看出为什么路由不准）。"""
    lines = []
    for m in (group or {}).get("members") or []:
        name = str(m.get("name") or "")
        role = str(m.get("role") or "").strip() or "（未写职责）"
        lines.append("- %s — %s" % (name, role))
    return "\n".join(lines) or "（名册为空）"


def build_router_user_message(group: dict, user_text: str,
                              digest_text: str = "",
                              last_speaker: str = "") -> str:
    """路由器的输入（只放小上下文：名册 + 上一轮摘要 + 当前消息）。"""
    bits = [
        "群名：%s" % (str((group or {}).get("name") or "未命名群聊")),
        "名册：",
        roster_text(group),
    ]
    brief = str((group or {}).get("brief") or "").strip()
    if brief:
        bits += ["", "群背景/目标：", brief]
    if last_speaker:
        bits += ["", "上一位发言人：%s" % last_speaker]
    if digest_text.strip():
        bits += ["", "上一轮摘要：", digest_text.strip()]
    bits += ["", "用户消息：", str(user_text or "")]
    return "\n".join(bits)


def parse_router_reply(content: str, names: list[str]) -> dict:
    """解析路由器返回的 JSON。任何异常都回 `low`（调用方据此回退全员）。

    返回 `{"responders": [...], "confidence": "high"|"low", "reason": "..."}`。
    """
    raw = str(content or "").strip()
    # 容忍 `json ... ` 与前后废话：取第一个 `{` 到最后一个 `}`。
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"responders": [], "confidence": "low", "reason": "no_json"}
    try:
        data = json.loads(raw[i:j + 1])
    except Exception:                                           # noqa: BLE001
        return {"responders": [], "confidence": "low", "reason": "bad_json"}
    if not isinstance(data, dict):
        return {"responders": [], "confidence": "low", "reason": "not_object"}

    wanted = data.get("responders")
    if isinstance(wanted, str):
        wanted = [wanted]
    if not isinstance(wanted, list):
        wanted = []
    valid = {str(n) for n in names}
    got: list[str] = []
    for item in wanted:
        name = str(item or "").strip().lstrip("@")
        if name in valid and name not in got:
            got.append(name)

    conf = str(data.get("confidence") or "").strip().lower()
    if conf not in ("high", "low"):
        conf = "low"
    return {"responders": got, "confidence": conf,
            "reason": str(data.get("reason") or "")[:200]}


def _members_for(group: dict, names: list[str]) -> list[dict]:
    want = set(names)
    return [dict(m) for m in (group or {}).get("members") or []
            if str(m.get("name") or "") in want]


def select_responders(group: dict, user_text: str, digest_text: str,
                      router_call) -> dict:
    """决定这一轮谁回复。

    *router_call* 是一个 `callable(router_user_message) -> str`（返回模型原文）；
    它抛异常 = 路由失败，回退全员。

    返回 `{"members": [...], "mode": "mention|router|all|unknown_mention",
    "unknown": [...], "reason": "..."}`。
    """
    names = _member_names(group)
    if len(names) <= 1:
        return {"members": [dict(m) for m in (group or {}).get("members") or []],
                "mode": "all", "unknown": [], "reason": "single_member"}
    mentioned, unknown = parse_mentions(user_text, names)
    if mentioned:
        return {"members": _members_for(group, mentioned), "mode": "mention",
                "unknown": [], "reason": ""}
    if unknown:
        return {"members": [], "mode": "unknown_mention",
                "unknown": unknown, "reason": ""}

    digest = str(digest_text or "")
    last_speaker = ""
    for line in reversed(digest.splitlines()):
        m = re.match(r"-\s*([^（(]+?)[（(]", line.strip())
        if m:
            last_speaker = m.group(1).strip()
            break

    try:
        raw = router_call(build_router_user_message(
            group, user_text, digest, last_speaker))
    except Exception as exc:                                    # noqa: BLE001
        return {"members": [dict(m) for m in (group or {}).get("members") or []],
                "mode": "all", "unknown": [],
                "reason": "router_error:%s" % type(exc).__name__}

    parsed = parse_router_reply(raw, names)
    if parsed["confidence"] == "high" and parsed["responders"]:
        return {"members": _members_for(group, parsed["responders"]),
                "mode": "router", "unknown": [],
                "reason": parsed.get("reason") or ""}
    return {"members": [dict(m) for m in (group or {}).get("members") or []],
            "mode": "all", "unknown": [],
            "reason": "router_low:%s" % (parsed.get("reason") or "")}


# ----------------------------------------------------------------------
# 圆桌
# ----------------------------------------------------------------------
#
# 三条：
#   ① 发言顺序 = 自动接力（一次小 LLM 调用决定下一位）；
#   ② 一条消息最多 3 位（硬上限 —— 不设硬轮数上限的 auto-mode 是反面教材）；
#   ③ 身份 = 群级 brief + 成员自己的 `role`（brief 进 system，role 也进
#      system —— 两者对本轮所有发言人都成立，不动缓存前缀）。

#: 一轮最多几位发言人（硬上限，不跟策略走）。
ROUNDTABLE_MAX_SPEAKERS = 3

#: 接力器的提示词。与路由器（`ROUTER_SYSTEM_PROMPT`）不同的一件：
#: 路由器选"谁该回答用户"，接力器选"下一位还有没有实质补充" —— 后者允许空。
RELAY_SYSTEM_PROMPT = (
    "你在主持一个圆桌讨论。看名册、用户的最后一句话、以及刚刚已经说过的人，"
    "判断下一位该由谁补充发言。\n"
    "★ 只选**真的还有实质内容**的人：换个说法重复上一位 ⇒ 不要选。\n"
    "★ 每个人一轮最多说一次；已经说过的不再选。\n"
    "★ 只输出一行 JSON：{\"next\": \"名字或空串\", \"reason\": \"一句话\"}。\n"
    "★ 不确定 / 没有人还有实质补充 ⇒ next 写空串。"
)


def build_relay_user_message(group: dict, user_text: str, spoken: list[dict],
                             digest_text: str = "") -> str:
    """接力器的输入 —— 只放小上下文（名册 + 已发言 + 当前消息）。"""
    said = []
    for s in spoken or []:
        content = str(s.get("content") or "").strip()
        if len(content) > 600:
            content = content[:600] + "…"
        said.append("- %s：%s" % (s.get("name") or "", content or "（空）"))
    bits = [
        "群名：%s" % (str((group or {}).get("name") or "未命名群聊")),
        "名册：",
        roster_text(group),
    ]
    brief = str((group or {}).get("brief") or "").strip()
    if brief:
        bits += ["", "群背景/目标：", brief]
    bits += ["", "用户消息：", str(user_text or "")]
    if said:
        bits += ["", "这一轮已经说过的人："] + said
    else:
        bits += ["", "这一轮还没有人发言。"]
    if digest_text.strip():
        bits += ["", "上一轮摘要：", digest_text.strip()]
    return "\n".join(bits)


def parse_relay_reply(content: str, names: list[str]) -> dict:
    """解析接力器返回的 JSON。任何异常都回空（调用方据此停止接力）。"""
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j <= i:
        return {"next": "", "reason": "no_json"}
    try:
        data = json.loads(raw[i:j + 1])
    except Exception:                                           # noqa: BLE001
        return {"next": "", "reason": "bad_json"}
    if not isinstance(data, dict):
        return {"next": "", "reason": "not_object"}
    nxt = str(data.get("next") or "").strip().lstrip("@")
    if nxt not in {str(n) for n in names}:
        nxt = ""
    return {"next": nxt, "reason": str(data.get("reason") or "")[:200]}


def select_next_speaker(group: dict, user_text: str, spoken: list[dict],
                        relay_call) -> dict:
    """叫下一位。回 `{member|None, reason}`。

    已经说过的人不再选（每人一轮最多一次）；全部说过，停。
    接力器坏了，停（不是"回退全员"）：圆桌没有"全员兜底"这一说，
      硬塞一个下去只会让用户看到一堆重复。
    """
    members = [dict(m) for m in (group or {}).get("members") or []]
    said = {str(s.get("name") or "") for s in spoken or []}
    left = [m for m in members if str(m.get("name") or "") not in said]
    if not left:
        return {"member": None, "reason": "everyone_spoke"}
    if len(left) == 1 and not said:
        #: 只有一个成员，不用问模型（省一次调用；结果也一样）。
        return {"member": left[0], "reason": "single_member"}
    names = [str(m.get("name") or "") for m in left]
    try:
        raw = relay_call(build_relay_user_message(group, user_text, spoken))
    except Exception as exc:                                    # noqa: BLE001
        return {"member": None, "reason": "relay_error:%s" % type(exc).__name__}
    parsed = parse_relay_reply(raw, names)
    if not parsed["next"]:
        return {"member": None, "reason": "relay_stop:%s" % (parsed.get("reason") or "")}
    return {"member": _members_for(group, [parsed["next"]])[0],
            "reason": parsed.get("reason") or ""}


def build_roundtable_turn_text(member: dict, spoken: list[dict],
                               minutes_text: str = "") -> str:
    """圆桌的这一轮发言块（用户消息尾部；不落库、不进历史）。

    与 `build_member_user_text` 的差别：那里是"用户对你说了这句话"，
      这里是"共享历史已经在你眼前，现在轮到你接话" —— 所以不重复
      用户消息原文（它就在上一条 user 消息里）。
    """
    name = str(member.get("name") or "")
    role = str(member.get("role") or "").strip()
    bits = ["〔圆桌发言〕现在轮到你（%s）接话。" % name]
    if role:
        bits.append("你的职责：%s" % role)
    if spoken:
        names = "、".join(str(s.get("name") or "") for s in spoken)
        bits.append("这一轮已经说过的人：%s。" % names)
        bits.append("★ 别重复他们已经说过的；有不同意见就直说。")
    else:
        bits.append("★ 你是这一轮第一位发言的人。")
    if minutes_text.strip():
        bits += ["", "会议纪要（跨轮次的事实）：", minutes_text.strip()[:2000]]
    bits += ["", "直接给出你的发言（自足：结论 + 依据 + 必要时下一步）。"]
    return "\n".join(bits)


# ----------------------------------------------------------------------
# 互见摘要
# ----------------------------------------------------------------------

def digest_items(group: dict, replies: list[dict]) -> list[dict]:
    """把本轮成员回复压成摘要条目：`{name, role, conclusion}`。"""
    roles = {str(m.get("name") or ""): str(m.get("role") or "")
             for m in (group or {}).get("members") or []}
    items: list[dict] = []
    for r in replies or []:
        name = str(r.get("name") or "")
        if not name:
            continue
        content = str(r.get("content") or "").strip()
        if len(content) > DIGEST_ITEM_CHARS:
            content = content[:DIGEST_ITEM_CHARS] + "…"
        items.append({"name": name, "role": roles.get(name, ""),
                      "conclusion": content})
    return items


def format_digest(items: list[dict]) -> str:
    """摘要文本（注入路由器和下轮成员；不显示给用户）。"""
    lines: list[str] = []
    for it in items or []:
        name = str(it.get("name") or "")
        role = str(it.get("role") or "").strip()
        concl = str(it.get("conclusion") or "").strip()
        head = "- %s%s" % (name, ("（%s）" % role) if role else "")
        lines.append(head + " → 结论：" + (concl or "（没有结论）"))
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 成员提示词
# ----------------------------------------------------------------------

def build_member_system_block(group: dict, member: dict) -> str:
    """群规则 + "你是谁"（system 层）。

    只放对本轮所有成员都成立的群规则 + 这一位成员的身份；
      互见摘要与"用户点了谁"放用户消息尾部（`build_member_user_text`），
      因为那两块每轮都会变 —— 放 system 会把缓存前缀改写掉。
    """
    gname = str((group or {}).get("name") or "未命名群聊")
    count = len((group or {}).get("members") or [])
    name = str(member.get("name") or "")
    role = str(member.get("role") or "").strip()
    brief = str((group or {}).get("brief") or "").strip()
    bits = [
        "## GROUP CHAT",
        "这是一个**无主理人**的协作群聊「%s」（%d 人）。用户直接对所有人说话。" % (gname, count),
        "★★ 本轮由你（%s）回复。" % name,
        "你的职责：%s" % (role or "（未写职责）"),
        "★ 你只代表你自己：不要替其他成员说话，不要写「他们会说……」；"
        "也不要调用 `ask_member`（无主理人群聊里成员不再向下派活）。",
    ]
    if brief:
        bits += ["", "群背景/目标：", brief]
    bits += ["", "需要工具就照常使用；结论要自足（谁、在哪、是什么）。"]
    return "\n".join(bits)


def build_member_user_text(user_text: str, member: dict,
                           mentioned: bool, digest_text: str = "",
                           minutes_text: str = "") -> str:
    """成员看到的用户消息 = 原始消息 + 尾部块（不落库、不进历史）。

    尾部块只影响这一次请求；`ctx.messages` 里存的仍是原始用户消息。
    `minutes_text` = 群聊会议纪要（`core/group_minutes.py`）。
      它是跨轮次的事实（群目标/名册/决策/未决/产物），不随压缩消失
      —— 因为它不在 `messages` 里。放在 digest 之前：先读长期，再读上一轮。
    """
    name = str(member.get("name") or "")
    bits = [str(user_text or "")]
    tail = ["<group_turn speaker=\"%s\">" % name]
    if mentioned:
        tail.append("用户点名了你（或点名的人里有你）—— 这一轮请优先回应。")
    else:
        tail.append("分派器判断这条消息与你的职责相关 —— 这一轮由你回复。")
    tail.append("只回你自己的结论；不要替其他成员回答。")
    tail.append("</group_turn>")
    bits.append("\n".join(tail))
    # 框架块不变量：纪要与摘要都是模型写的内容 ——
    #   正文里一个伪 `</group_minutes>` / `</group_digest>` 就能越出包裹，
    #   冒充框架对下一位成员说话。内容过 `sanitize_text`，包裹照发。
    if minutes_text.strip():
        bits.append("<group_minutes>\n%s\n</group_minutes>"
                    % _sanitize_block(minutes_text))
    if digest_text.strip():
        bits.append("<group_digest>\n%s\n</group_digest>"
                    % _sanitize_block(digest_text))
    return "\n\n".join(bits)


def _sanitize_block(text: str) -> str:
    """框架块的内容侧打码。失败时原样返回 —— 不因打码挂掉一轮。"""
    try:
        from core.content_sanitizer import sanitize_text
        return sanitize_text(str(text or "").strip())
    except Exception:                                            # noqa: BLE001
        return str(text or "").strip()


def member_reply_content(name: str, content: str) -> str:
    """写进群历史的成员回复 —— 带名字前缀。

    为什么要前缀：历史里的消息只有 `role` / `content`，`speaker` 字段
      不会上 wire；不给前缀，下一位成员看到的就是一条"自己说过的话"。
      前缀让模型读得出"这是谁说的"；GUI 靠 `speaker` 字段渲染，
      显示时会把前缀剥掉（`histRow`）。
    """
    return "【%s】%s" % (name, str(content or "").strip())


def strip_member_prefix(name: str, content: str) -> str:
    """给 GUI 用：把 `【名字】` 前缀剥掉（只剥自己那一层）。"""
    prefix = "【%s】" % (name or "")
    text = str(content or "")
    return text[len(prefix):] if name and text.startswith(prefix) else text


def strip_repeated_prefix(name: str, content: str) -> str:
    """剥掉模型自己模仿出来的重复 `【名字】` 前缀（一层或多层）。

    失败模式：共享历史里的助手消息长成 `【worker1】…`，
      下一位模型于是把 `【worker3】` 也写进正文；框架再套一层，
      库里 `【worker3】【worker3】…`，而界面只剥一层，用户看到多余前缀。
    只认自己的名字：别的名字可能是在引用（`【worker1】说得对…`），
      删掉就改变了原意。
    """
    text = str(content or "")
    if not name:
        return text
    while True:
        nxt = strip_member_prefix(name, text)
        if nxt == text:
            return text
        text = nxt


def approval_route_text(name: str, payload: dict) -> str:
    """群成员碰到审批时的拒绝 + 路线。

    ## 为什么不是"把审批浮窗弹出来"

    审批的结算模型是「挂起 → 用户回答 → 下一轮重放那次调用」。
    而群成员是一次独立的 `agent_loop`：它的 tool 轨迹没有落进群会话
    （我们只把最终结论写成一条消息）。挂起之后，下一轮 `AgentLoopStage`
    找不到那条 tool 消息，也就没法把结果补回原位。
与其留一个"模型在等、用户只看到一片安静"的死锁，
      不如当场拒绝并给路线（拒绝不许悬空）。

    这不是"成员没有权限"：自动模式下分类器本来就会代答
      （安全，预授权放行；危险/没答上来，拒绝）。只有真的走到"问用户"
      那一支（手动档 / 熔断 / 安全但不知道怎么预授权）才会拿到 `pending_*`。
    """
    kind = str((payload or {}).get("kind") or "").strip()
    subject = str((payload or {}).get("subject")
                  or (payload or {}).get("prompt") or "").strip()
    if len(subject) > 120:
        subject = subject[:120] + "…"
    what = subject or kind or "需要审批的操作"
    return (
        "成员 %s 在执行「%s」时被审批拦住了。\n"
        "群聊成员**不能直接弹审批浮窗**（审批的回答要靠「下一轮重放」，"
        "而成员的中间轨迹不落进群历史）。\n"
        "⇒ 请在主对话里做这一步；或在设置里把交互模式切到「自动」，"
        "让分类器代答之后再来群里做。" % (name, what)
    )
