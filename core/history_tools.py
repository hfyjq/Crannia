"""查历史 —— 在一段会话存着的历史里搜。

## 它回答什么

对话历史有三条被处理掉的路，模型回不去：

  ① `load_session` 按预算没装载（行还在库里）  —— 本工具能找回来
  ② 滑窗静默丢弃（行也还在库里）                  —— 本工具能找回来
  ③ 压缩（`_persist_compaction`）              —— 也能找回来

压缩只追加（摘要行 + 边界行，见 `append_compaction`），
被压掉的原话一行不动地留在库里，而本工具读的就是那张表，搜得到。

本工具读的是全表（不是活动窗口） —— 这是故意的：活动窗口
（`live_message_rows`，压缩之后的部分）是给装载用的，而这里要搜的正是
被压掉的那一段。两件事、两个读法。

## 三条判据

- `0 个` 的两种含义分开说 ——「在窗口里搜完了没有」（结论）vs
  「窗口到边了」（还有更早的轮在库里 · 输出撞上限）。
  报数不报字符。
  「原件到边了（以摘要为界）」这种情况不存在 ——
  压缩不再删行，窗口外永远只是"还没搜到"，不是"搜不到了"。
- 常驻工具，schema 逐字节稳定；参数字典序固定。
- 与 `/clear` 自洽：`/clear` 清行，清完就查不到 —— 同一张表，天然成立，
  不要另立缓存（不新建存储）。

## 三条写在案的取舍

- 只搜一段会话（工具壳给 `_sid()`，端点给自己推的存储键）—— 跨会话是
  「你另一条线上聊过什么」，那是 `memory_search`（长期记忆）的活；
  两层不合并（同日报那条判据）。
- 窗口显式（`turns`，默认 20、上限 100）：不传"搜全部"会让工具在长会话里
  悄悄变慢变大，而模型看不见它搜了多远；显式窗口让代价可见。
- 只读：不写任何东西（"零风险"）。

## 核 / 壳拆开了

```
search_history(storage_id, query, turns) -> dict     ← 核：结构化，只认存储键
render_search(result) -> str                         ← 结构化 → 中文（一份话术）
history_search(query, turns) -> str                  ← 壳：解析当前会话 + 渲染（工具用）
```

为什么：HTTP 端点上没有"当前会话" —— `_sid()` 走
`request_snapshot.current_session_id()`，出了流水线那一轮就是空的，
端点若直接调 `history_search()` 只会永远收到"拿不到当前会话号"，
而症状看起来像"这段对话没内容"。
而"谁能搜、搜多远、压缩掉的原话怎么办"只许有一份真相，参数化，
不是在端点里另写一套搜索。

核还多给一样东西：每条命中的 `id`。工具不需要它，界面需要
（"点条目跳回那一行"）—— 而 `load_messages` 的返回值里没有 `id`
（它是给装载进上下文用的），所以核走的是 `short_term.load_message_rows`。

ASCII-only 打印（Windows GBK 控制台）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# 消息 `content` 的纯文本视图（全仓唯一转换处）。
from core.image_input import content_text

#: 默认 / 上限窗口（"轮" = 一条 user 消息起头的一段）。
DEFAULT_TURNS = 20
MAX_TURNS = 100

#: 命中词的左右各留多少字符（snippet）。
_SNIPPET_PAD = 60

#: 一次最多列几条命中；报数说清总共几条。
#: 这就是输出的口径上限 —— snippet 是定长（±`_SNIPPET_PAD`），
#: 8 条封顶后回执必然有界，不需要再挂一道字符截断（挂了也是够不着的死代码）。
_MAX_LISTED = 8

# 摘要行的文本视图 —— 直接复用压缩器那一个函数（一处判据、两个读者），
# 不再自己遍历标签。
#
# 正确语义是只取包裹里的正文：摘要在包裹外面带一句路标
# （`SUMMARY_POINTER`），减标签的写法会把它当成摘要正文
#搜「历史库」会把每条摘要都搜出来。
# 这个 import 是模块级的（不是惰性）—— 查过没有环：压缩器只 import
# `typing` / `content_sanitizer` / `image_input`，而这一层本来就开着
# `image_input`（见上一行）。跑不出环就别写惰性 import。
from core.memory.compressor import strip_summary_wrapper  # noqa: E402

#: 两句"接线故障"的话 —— 壳与核共用同一份（核里给端点，壳里给模型）。
_NO_MANAGER = ("查历史不可用：记忆库没有接线（框架启动时未注册 MemoryManager）。"
               "这是接线故障，不是「没搜到」。")
_NO_SESSION = "查历史不可用：拿不到当前会话号（请求快照是空的）。"


def _manager():
    from bus.registry import get_memory_manager
    return get_memory_manager()


def _sid() -> str:
    """`history_search` 要的是存储键 —— 它读的是消息行。

    `current_session_id()` 给的是通道键 ——
    直接拿去读消息会读一条空线程，而症状是
    「`history_search` 说这段对话没内容」（静默，且看起来像"历史真的没了"）。

    用纯查询 `bound_storage_id`，不用 `storage_id_for` ——
    一个读工具不许因为"查一下"就铸出一个会话出来。
    没绑过，`""`，搜不到（那时这个通道本来就还没开过口）。

    端点不走这里（它没有"请求快照"这个上下文）—— 见模块抬头那条。
    """
    from core.request_snapshot import current_session_id
    key = current_session_id()
    if not key:
        return ""
    mm = _manager()
    if mm is None:
        return ""
    return mm.short_term.bound_storage_id(key) or ""


def _text(content) -> str:
    """content 可能是富文本链（list）—— 统一成单行文本。

    块列表走 `content_text`（全仓唯一转换处）——
    `" ".join(str(x) for x in content)` 对图片块得到的是**带 base64 的
    repr**（几十万字符），而这一段的任务是"把这一轮压成一行摘要"。
    `str` 那一条路一个字节都不动（`content_text` 是恒等）——
    它读的是 `short_term.db`，库里本来就只有字符串，所以这是防御性写法。
    """
    if isinstance(content, (list, tuple)):
        return content_text(content).replace("\n", " ")
    return str(content or "")


#: 框架代笔的 user 行（不是用户说的话）—— 不进"对话行"。
#: 今天的产者：`scheduler._execute_serial`（后台回报 / 定时任务）与
#: `pre_process` 的审批结算（`[系统] [审批结果] …`）。
#: SQL 版在 `short_term.load_message_rows`，两边由 `test_history_api` 钉着。
FRAMEWORK_NOTE_KINDS = frozenset({"framework_note"})


def is_conversation_row(m: Dict) -> bool:
    """这条消息算不算「给人看的对话行」？

    一处判据、两个读者（界面不显示工具调用、只显示回复）：
      · 本模块 `_corpus_and_window` —— 搜索能搜什么
      · `short_term.load_message_rows(conversation_only=True)` —— 界面显示什么
        那个是同一个判据的 SQL 版（筛在 SQL 里是为了让"一页"名副其实，
          不然 50 条原始行里可能只剩 8 条对话行，「加载更早」点一次几乎没动静）。
        两处漂了就会出现「列表里有、搜不到」（或反过来）。
        机械读者按集合相等钉着：`tests/test_history_api.py`。

    判据 = `role ∈ {user, assistant}` 且正文非空：
      · `tool` 结果不是对话（那是过程，不是往来）
      · 空的 `assistant` 也不是（那是"这一轮只调了工具、一个字没说"的载体）
      · 摘要算（`kind=history_summary`）—— 它的 role 是 `user`、正文非空 ✓
    """
    if m.get("role") not in ("user", "assistant"):
        return False
    # 框架代笔的 user 行不是"往来"（`framework_note`）：后台任务回报 /
    #    审批结果这些是框架说给模型听的，重新加载后会长成一个用户气泡
    #    （重启后聊天记录里冒出一条绿色气泡，正文是
    #    "后台任务回报 · 这是框架转来的任务结果…"）。
    #    摘要（`kind=history_summary`）仍然算 —— 那条用户确实要看见。
    if m.get("kind") in FRAMEWORK_NOTE_KINDS:
        return False
    return bool(_text(m.get("content")).strip())


def _label(msg: Dict) -> str:
    if msg.get("kind") == "history_summary":
        return "摘要"
    return "用户" if msg.get("role") == "user" else "助手"


def _snippet(text: str, term: str) -> str:
    """命中词附近的一小段（折叠空白；找不到词就给开头）。"""
    flat = " ".join(text.split())
    pos = flat.lower().find(term)
    if pos < 0:
        return flat[: _SNIPPET_PAD * 2]
    start = max(0, pos - _SNIPPET_PAD)
    end = min(len(flat), pos + len(term) + _SNIPPET_PAD)
    out = flat[start:end]
    return ("..." if start else "") + out + ("..." if end < len(flat) else "")


def _corpus_and_window(rows: List[Dict], turns: int):
    """切出窗口并过滤可搜的行。返回 `(corpus, used_turns, available_turns, has_summary)`。

    "轮"的边界规则照抄压缩器（`_find_turn_boundaries`）：以 `role=user` 且
    `kind != history_summary` 为新的一轮 —— 摘要不算轮（它是被压缩掉的那些轮）。

    `rows` 必须是 `load_message_rows` 那种带 `id` 的行（见模块抬头）——
    `load_messages` 的返回值也行，但那样命中里就没有行号、界面点不回去。
    """
    boundaries = [
        i for i, m in enumerate(rows)
        if m.get("role") == "user" and m.get("kind") != "history_summary"
    ]
    available = len(boundaries)
    used = min(max(1, int(turns)), available) if available else 0
    start = boundaries[available - used] if used else 0
    # 摘要行要带上：窗口从某一轮切进来时它是窗口外那一头的内容，
    #   而它是"存着的历史"的一部分（一次压缩留一条，可能有好几条
    #   —— 每条都是那一段的压缩产物，都算存着的历史）。
    head = [m for m in rows[:start] if m.get("kind") == "history_summary"]
    window = head + rows[start:]
    # 走 `is_conversation_row` —— 同一个判据，界面上显示哪些就搜哪些。
    corpus = [m for m in window if is_conversation_row(m)]
    has_summary = any(m.get("kind") == "history_summary" for m in rows)
    return corpus, used, available, has_summary


def _err(code: str, message: str) -> Dict:
    """失败回执 —— 形状与成功时同形（少一个 `key` 就够让前端崩）。"""
    return {
        "ok": False, "code": code, "error": message, "query": "",
        "window": {"used": 0, "available": 0, "messages": 0},
        "has_summary": False, "notes": [], "total": 0, "listed": 0, "hits": [],
    }


def search_history(storage_id: str, query: str = "",
                   turns: int = DEFAULT_TURNS) -> Dict:
    """核：结构化地搜一段会话的存史。不解析会话键、不拼中文。

    `storage_id` 是存储键（消息行挂的那个 id）。给 `""`，报"拿不到会话号"
    （工具壳在请求快照为空时就是这条）。

    返回：
        {
          "ok": bool,            # 这次查询跑成了没有（不是"有没有命中"）
          "code": str,           # no_query / no_manager / no_session / bad_turns
                                 # / over_max / no_history / searched
          "error": str,          # 只在 ok=False 时有意义
          "query": str,
          "window": {"used": int, "available": int, "messages": int},
          "has_summary": bool,
          "notes": [str],        # "更早的已压成摘要（原文仍在库里）" /
                                 # "窗口外还有更早的 N 轮"
          "total": int,          # 命中总数（列出来的可能更少）
          "listed": int,         # 实际列了几条
          "hits": [{"index", "id", "role", "kind", "label", "snippet", "ts"}],
        }

    判据顺序不许重排：query → manager → session → turns。
    工具那边有一条测试钉着「manager 没接线」与「会话号为空」两句话不一样
    （`test_wiring_truthfulness`）—— 排反了它们会塌成一句。
    """
    q = " ".join(str(query or "").split())
    if not q:
        return _err("no_query", "没有给 query —— 说要搜什么词。")

    mgr = _manager()
    if mgr is None or not getattr(mgr, "short_term", None):
        return _err("no_manager", _NO_MANAGER)

    sid = str(storage_id or "")
    if not sid:
        return _err("no_session", _NO_SESSION)

    try:
        turns = int(turns)
    except (TypeError, ValueError):
        return _err("bad_turns", "turns 要是整数（最近多少轮）。")
    if turns > MAX_TURNS:
        return _err("over_max",
                    f"turns 超过上限 {MAX_TURNS} —— 从 {MAX_TURNS} 以内挑一个"
                    "（窗口要显式、可承受）。")

    rows, _more = mgr.short_term.load_message_rows(sid)
    if not rows:
        r = _err("no_history", "当前会话在库里没有历史（新会话，或 `/clear` 清过）。")
        r["ok"] = True          # 这不是"查询失败"，是"查了，里头什么都没有"
        r["query"] = q
        return r

    corpus, used, available, has_summary = _corpus_and_window(rows, turns)
    terms = [t.lower() for t in re.split(r"\s+", q) if t]
    matched = []
    for i, m in enumerate(corpus, 1):
        text = _text(m.get("content"))
        if m.get("kind") == "history_summary":
            text = strip_summary_wrapper(text)
        low = text.lower()
        if any(t in low for t in terms):
            matched.append((i, m, text))

    notes: List[str] = []
    if has_summary:
        # 这句话必须说清「原文仍在库里」—— 否则模型会以为老话找不回来。
        notes.append("更早的轮已压成摘要 —— 但**原文都还在库里**，"
                     "本工具搜的就是全表（连被压掉的那段一起）")
    if used < available:
        notes.append(f"窗口外还有更早的 {available - used} 轮在库里 —— 调大 turns 能搜到")

    matched.reverse()                       # 最近的在前
    listed = matched[:_MAX_LISTED]
    hits = [
        {
            "index": i,
            "id": m.get("id"),
            "role": m.get("role"),
            "kind": m.get("kind"),
            "label": _label(m),
            "snippet": _snippet(text, terms[0]),
            "ts": m.get("ts"),
        }
        for i, m, text in listed
    ]
    return {
        "ok": True, "code": "searched", "error": "", "query": q,
        "window": {"used": used, "available": available, "messages": len(corpus)},
        "has_summary": has_summary, "notes": notes,
        "total": len(matched), "listed": len(hits), "hits": hits,
    }


def render_search(r: Dict) -> str:
    """结构化 → 中文（唯一一处）。壳与端点若各写一份，两份都会漂。

    照抄原来的话术逐字——那些字有测试钉着（`test_history_search.py`）。
    """
    if not r.get("ok"):
        return r.get("error") or "查历史失败（没有说明原因）。"
    if r.get("code") == "no_history":
        return "当前会话在库里没有历史（新会话，或 `/clear` 清过）。"

    q = r.get("query") or ""
    win = r.get("window") or {}
    used = int(win.get("used") or 0)
    n = int(win.get("messages") or 0)
    notes = list(r.get("notes") or [])

    scope = (f"在最近 {used} 轮（{n} 条消息）里搜「{q}」" if used
             else f"在存着的 {n} 条消息里搜「{q}」")

    total = int(r.get("total") or 0)
    if not total:
        return _assemble(f"{scope}：**搜完了，没有匹配**。", notes)

    lines = [f"{scope}：命中 {total} 条" +
             (f"（只列最近 {_MAX_LISTED} 条，报数不报字符）"
              if total > _MAX_LISTED else "") + "："]
    for h in r.get("hits") or []:
        lines.append(f"[窗口内第 {h.get('index')} 条 · {h.get('label')}] "
                     f"{h.get('snippet')}")
    lines.append("要全文就问用户，或让我读出来（历史正文不在这里展开）。")
    return _assemble("\n".join(lines), notes)


def history_search(query: str = "", turns: int = DEFAULT_TURNS) -> str:
    """在当前会话存着的历史里搜。永远返回 str（纯文本工具）。

    壳 —— 只做两件事：解析当前会话（`_sid()`）· 把核的结果渲染成中文。
    真正的搜索在 `search_history`（HTTP 端点共用那一份）。
    """
    return render_search(search_history(_sid(), query, turns))


def _assemble(head: str, notes: List[str]) -> str:
    if not notes:
        return head
    return head + "\n（" + "；".join(notes) + "）"


# schema（每请求的永久税，改措辞必须重量一次）

HISTORY_SEARCH_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "name": "history_search",
    "description": (
        "在**当前会话**存着的历史里搜说过的话（默认最近 "
        f"{DEFAULT_TURNS} 轮）。被压缩掉的原话**也在库里**，一样搜得到。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "要搜的词（可多个，空格分隔）"},
            "turns": {"type": "integer",
                      "description": f"搜最近多少轮（默认 {DEFAULT_TURNS}，上限 {MAX_TURNS}）"},
        },
        "required": ["query"],
    },
}
