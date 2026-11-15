"""周期任务的工具面 —— `cron_create` / `cron_list` / `cron_delete`。

## 为什么不只做面板

工具面不能省：「在对话里让模型建」是主路，面板是那个可选的半边 ——
只做面板等于把定时任务能不能用，绑在用户会不会去点它上面。

## 三条不许回退的判据

### ① 不写自然语言时间解析器

六家零先例 —— 全是模型把话转成结构化字段（不自己解析）。
这里只收结构化的 `rule` 对象；话术里明写"不要自己算绝对时间"。

### ② "投给谁"取不到就拒

投递目标 = 这一轮从哪个渠道来的（`request_snapshot.current_channel()`）。
它是推不出来的：会话键（`current_session_id()`）故意忽略 `platform`。
取不到时拒，理由说清楚 —— 建一条投不出去的任务比不建更坏
（不承诺兑现不了的事）。

### ③ 时间一律由框架算

`next_run` 在这里由 :func:`core.cron.expr.next_after` 算 一次并落库。
模型不许自己转 UTC、不许自己算"下一次是哪天"
（那正是"两个时钟"的来源）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from core.cron import expr
from core.cron.store import get_cron_store
from core.i18n import t as tr

#: 工具 description 刻意写短 —— 它按每条请求计费（规矩：
#: 改 schema 必须重量 token）。判据类的话只留最容易写错的那两句：
#: 星期几的口径、以及"别自己算时间"。
_WEEKDAY_NOTE = "weekdays 用 1=周一…7=周日（**不是** cron 的 0=周日）。"

CRON_CREATE_SCHEMA: dict = {
    "name": "cron_create",
    "description": (
        "建一条定时任务：到点时你去跑 `prompt` 那段话，结果发回用户。"
        "★ 时间不要自己算、不要转 UTC —— 把用户说的原样填成结构化字段。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "给人看的一行名字"},
            "prompt": {"type": "string", "description": "到点时你要做的那件事"},
            "rule": {
                "type": "object",
                "description": (
                    "repeat {kind,at:'HH:MM',tz?,selectors} · "
                    "every {kind,seconds≥300} · once {kind,at:'ISO'}。"
                    "selectors 每项 {type:weekdays|month_days|dates, values}，"
                    "四处**取并集**（命中任一即跑），空数组=每天。"
                    + _WEEKDAY_NOTE + " tz 不给=本机。"
                ),
                "properties": {
                    "kind": {"type": "string", "enum": ["repeat", "every", "once"]},
                    "at": {"type": "string"},
                    "tz": {"type": "string"},
                    "seconds": {"type": "integer"},
                    "selectors": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["kind"],
            },
            "deliver": {
                "type": "string",
                "enum": ["here", "new"],
                "description": "here=接着这段对话（默认）；new=另起一段交付。",
            },
        },
        "required": ["title", "prompt", "rule"],
    },
}

CRON_LIST_SCHEMA: dict = {
    "name": "cron_list",
    "description": "列出定时任务：下次什么时候跑、上次跑成什么样。",
    "inputSchema": {"type": "object", "properties": {}},
}

CRON_DELETE_SCHEMA: dict = {
    "name": "cron_delete",
    "description": "删掉一条定时任务及其运行记录。只想停掉不想删，让用户去面板暂停。",
    "inputSchema": {
        "type": "object",
        "properties": {"job_id": {"type": "string",
                                  "description": "cron_list 给的 id"}},
        "required": ["job_id"],
    },
}


def _channel_of_this_turn() -> str:
    """这一轮从哪个渠道来的。判据见模块 docstring ②。"""
    from core.request_snapshot import current_channel

    return current_channel()


def _now() -> datetime:
    return datetime.now().astimezone()


def preview_rule(rule: Any = None) -> dict:
    """规则的唯一校验器 + 唯一解释器（面板要用）。

    判据：面板要显示的那句中文（"每周周二、周四 的 09:00"）、它算出来的下次时刻、
    以及"这个规则合不合法"，全都出自这里 —— 前端一个字节都不重写规则语义
    （重写就是第二套真相；而两套解释器给出的答案看着都对，那正是最难查的一种）。

前端只做"把控件拼成一个 `rule` 对象"，然后问这里。
    """
    try:
        parsed = expr.parse_rule(rule)
    except expr.RuleError as exc:
        return {"ok": False, "error": str(exc)}
    nxt = expr.next_after(parsed, _now())
    return {
        "ok": True,
        "rule": parsed.as_json(),
        # 面板要显示的这句跟随界面语言；工具回执那几处仍走 `describe()`。
        "when": expr.describe_i18n(parsed),
        "next_run": nxt.isoformat() if nxt else None,
        # `next_run` 是 `None` 不是错误：`once` 的时刻过去了才会这样，
        #    而"下一次是什么时候"与"这条规则合不合法"是两个问题
        #    （建的时候会拒，预览时不拒 —— 面板要能把这句话显示出来）。
        "note": "" if nxt else tr("按这个规则算不出下一次（一次性任务的时刻已经过了？）"),
    }


def create_job(title: Any = "", prompt: Any = "", rule: Any = None,
               deliver: str = "here", channel: Optional[str] = None) -> dict:
    """建一条任务 —— 唯一的实现（工具与 HTTP 端点是两个入口，不是两套逻辑）。

    *channel* = 投给谁。`None`，从这一轮的请求快照上取（工具那条路）；
    给了就用它（HTTP 端点那条路：面板不是一个 agent 轮次，快照上什么都没有）。
    所有失败都带路线。
    """
    title = str(title or "").strip()
    prompt = str(prompt or "").strip()
    if not title:
        return {"success": False, "error": "title 不能为空（列表里要显示它）"}
    if not prompt:
        return {"success": False,
                "error": "prompt 不能为空 —— 到点时 agent 就是去跑这段话"}
    if deliver not in ("here", "new"):
        return {"success": False,
                "error": "deliver 只认 'here'（接着这条对话）或 'new'（另起一段）"}

    channel = channel if channel is not None else _channel_of_this_turn()
    if not channel:
        return {"success": False,
                "error": ("取不到这一轮是从哪个渠道来的，所以**建不了** —— "
                          "建一条投不出去的任务比不建更坏。"
                          "（这一轮若来自 Web/CLI，本不该发生；请把这条报给用户。）")}

    try:
        parsed = expr.parse_rule(rule)
    except expr.RuleError as exc:
        return {"success": False, "error": "时间规则不合法：%s" % exc}

    store = get_cron_store()
    if store.count() >= expr.MAX_JOBS:
        return {"success": False,
                "error": ("定时任务最多 %d 条（现在已满）。"
                          "先用 cron_list 看看哪条可以删，或让用户去面板里清理。"
                          % expr.MAX_JOBS)}

    now = _now()
    nxt = expr.next_after(parsed, now)
    if nxt is None:
        return {"success": False,
                "error": ("按这个规则算不出**下一次**（一次性任务的时刻已经过了？）—— "
                          "改一个还没到的时刻，或者改成 repeat。")}

    jid = store.add(title=title, prompt=prompt,
                    rule_json=json.dumps(parsed.as_json(), ensure_ascii=False),
                    channel=channel, deliver=deliver,
                    next_run=nxt.isoformat())
    return {
        "success": True,
        "job_id": jid,
        "when": expr.describe(parsed),
        "next_run": nxt.isoformat(),
        "deliver": deliver,
        "note": "建好了。可以把 job_id 告诉用户，也可以用 cron_delete 撤销。",
    }


def cron_create(title: Any = "", prompt: Any = "", rule: Any = None,
                deliver: str = "here") -> dict:
    """`cron_create` 的 handler —— 只比 `create_job` 多一件事：自己找渠道。

    别在这里加第二份校验/话术：工具的入口与 HTTP 的入口必须是同一个实现
    （同一个概念两处实现 = 两套真相，而它们给出的答案看着都对）。
    """
    return create_job(title, prompt, rule, deliver)


def _parse_actions(stats: dict) -> dict:
    """把台账那一行 JSON 折成给界面/模型看的形状。读不出来给空，不抛。"""
    raw = stats.get("last_actions_raw") or ""
    if not raw:
        return {"n": 0, "list": []}
    try:
        d = json.loads(raw)
    except ValueError:
        return {"n": 0, "list": [], "unreadable": True}
    items = d.get("list") or []
    return {
        "n": int(d.get("n") or len(items)),
        "list": [str(x.get("a") or "?") + ("" if x.get("ok", True) else "（失败）")
                 for x in items if isinstance(x, dict)],
    }


def cron_list() -> dict:
    """列出来（含派生统计 —— 判据见 `core/cron/store.py`）。"""
    rows = []
    for job in get_cron_store().list_with_stats():
        try:
            rule = json.loads(job.get("rule_json") or "{}")
        except ValueError:
            rule = {}
        try:
            when = expr.describe(expr.parse_rule(rule))
        except Exception:                          # noqa: BLE001
            when = "（规则读不出来）"
        rows.append({
            "job_id": job["id"],
            "title": job.get("title") or "",
            "when": when,
            "rule": rule,
            "enabled": bool(job.get("enabled")),
            "deliver": job.get("deliver") or "here",
            "next_run": job.get("next_run"),
            "last_run": job["stats"].get("last_run"),
            "last_status": job["stats"].get("last_status"),
            # 跑完了但一个字都没产出要看得见
            #   —— 否则它与一次正常成功逐字相同（`reason=empty_reply`）。
            "last_reason": job["stats"].get("last_reason") or "",
            "last_error": job["stats"].get("last_error") or "",
            # 台账：上一次它做了哪些动作。
            #   判据：到点时没人在场，"它做了什么"是唯一能事后核查的东西。
            #   `n` 是真数（记不下那么多时 `list` 是截断的）—— 见
            #   `runner.MAX_ACTIONS_RECORDED`（报数，不报字符）。
            "last_actions": _parse_actions(job["stats"]),
            "run_count": job["stats"].get("run_count", 0),
            "fail_streak": job["stats"].get("fail_streak", 0),
        })
    return {"success": True, "count": len(rows), "jobs": rows,
            "note": ("要停掉某条（不删）让用户去面板里暂停；"
                     "已跑完的一次性任务会自动停用。")}


def cron_delete(job_id: Any = "") -> dict:
    """删一条（连带台账）。"""
    jid = str(job_id or "").strip()
    if not jid:
        return {"success": False, "error": "要给 job_id（用 cron_list 拿）"}
    if not get_cron_store().delete(jid):
        return {"success": False, "error": "没有这条任务：%r（用 cron_list 看一眼）" % jid}
    return {"success": True, "job_id": jid, "note": "删掉了，它的运行记录也一起清了。"}
