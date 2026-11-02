"""「今天几号」的两个落点 —— system prompt 一行 + 跨零点时消息流补一条。

## 为什么不是「给模型一个取日期的工具」

模型被问「今天几号」时会绕一大圈 ——
走 `run_command`（ADMIN 档 + 审批）只为拿一个日期。
日期是背景事实（注入的必须是背景事实），
不是需要"去查"的东西；做成工具还有一层代价：模型未必想得到去调它。

同类实现的做法：上下文里直接写着当天日期。
全限定名与 `文件:行号` 一律不写在源码里。

## 两个落点，各管一半

① :func:`date_line` → system prompt 一行、每请求重算
   （产者 `core/prompt_assembler.py::_today_line`）。它保证模型任何时刻
   都读得到今天的日期。
② :func:`date_change_note` → 跨零点时往消息流补一条
   （消费点 `core/pipeline/stages/agent_loop.py`）。

## 代价如实记

日期进 system prompt，每跨一次零点断一次前缀缓存（前缀只许增长）。
一天一次，可接受，但这是有意的 —— 别在别处再写「缓存从不断」。

另一种取舍是把日期冻在 `messages[0]` 里故意让它过期，
靠 ② 那条附件告诉模型新日期 —— 理由是
"清那个缓存会把整段对话变成 cache_creation
（过夜会话每次跨零点约 920K 有效 token）"。我们的 system prompt 本来就
每轮重算，所以那一份代价对我们不构成额外开销。
"""

from __future__ import annotations

import datetime
from typing import Dict, Optional

#: 中文星期 —— 索引即 `datetime.weekday()`（周一 = 0）。从表里推，不写 if 链。
_WEEKDAYS_CN = ("一", "二", "三", "四", "五", "六", "日")

#: 每个会话最后一次发出去的日期（`YYYY-MM-DD`）。
#:
#: 它是会话级内存态，必须在会话级状态登记表里登记
#: （`/clear` 走 `purge_session` 清它）。不清的后果很轻：
#: 新一段对话的第一轮会多补一条日期附件（内容还是对的）—— 登记它是为了让
#: 普查看得见它，而不是因为它危险。
_last_dates: Dict[str, str] = {}


def local_iso_date(now: Optional[datetime.datetime] = None) -> str:
    """本地时区的 `YYYY-MM-DD`。

    本地，不是 UTC —— 问题问的是"我今天几号"，而 UTC 会在
    每天 08:00（东八区）才翻转，等于整个上午都答错一天。
    """
    return (now or datetime.datetime.now()).strftime("%Y-%m-%d")


def _fmt(d: datetime.datetime) -> str:
    """`YYYY-MM-DD（星期X）` —— 含星期。

    比常见做法多一格：只有 `YYYY-MM-DD` 的话，相对日期
    （「周三那个评审会」）对不上。记忆那条规矩正是"相对日期转绝对"。
    """
    return "%s（星期%s）" % (d.strftime("%Y-%m-%d"), _WEEKDAYS_CN[d.weekday()])


def date_line(now: Optional[datetime.datetime] = None) -> str:
    """system prompt 里那一行 —— 每次组装提示词都重算（见模块 docstring）。"""
    return "今天是 %s。" % _fmt(now or datetime.datetime.now())


def date_change_note(session_id: str,
                     now: Optional[datetime.datetime] = None) -> str:
    """跨零点了吗 —— 变了给一句，没变（或本会话第一次）给空串。

    有副作用：它同时记下今天。第一次调用也要记
    （第一轮只记录、不补附件），
    否则新会话的第一轮会平白多一条"日期变了"的附件。

    「没变」与「第一次」都返回空串，但原因不同 —— 调用方不需要区分，
    因为两边的动作都是"什么都不做"（这里没有第三个状态）。
    """
    d = now or datetime.datetime.now()
    today = d.strftime("%Y-%m-%d")
    prev = _last_dates.get(session_id)
    _last_dates[session_id] = today
    if prev is None or prev == today:
        return ""
    # 措辞（不要主动向用户提起，他已经知道了）—— 它是框架的话，
    # 形态与本仓既有的 `NEUTRALIZED_PENDING_NOTE` 同族（`[框架]` 打头）。
    return ("[框架] 日期已翻到新的一天 —— 今天是 %s。"
            "不要主动向用户提起这条，他已经知道了。" % _fmt(d))


def forget(session_id: str) -> None:
    """`/clear` 用 —— 丢掉这个会话记住的日期（`purge_session` 的唯一入口）。"""
    _last_dates.pop(session_id, None)
