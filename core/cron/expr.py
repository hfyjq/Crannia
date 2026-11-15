"""规则对象 → 下一次触发时刻。纯函数，没有副作用、没有时钟、不碰数据库。

## 为什么不是 cron 字符串

两条表达不了：

  · 「(4月,21) 或 (6月,1)」 —— cron 的「月」与「日」是 AND，配不出成对的并集；
  · 「每天除法定节假日」 —— cron 没有任何排除/取反算子。

存的是结构化规则对象。本模块是它唯一的解释器
（面板要显示中文预览也走 :func:`describe` —— 别在别处再解释一遍规则，那是第二套真相）。

## 规则对象

```jsonc
{"kind": "repeat", "at": "08:30", "tz": "Asia/Shanghai",
 "selectors": [{"type": "weekdays",   "values": [4, 7]},
               {"type": "month_days", "values": [21, 22, 23]},
               {"type": "dates",      "values": [[4, 21], [6, 1]]}]}
{"kind": "every", "seconds": 180000}
{"kind": "once",  "at": "<ISO 时刻，带偏移>"}
```

`selectors` 的判据只有一句：每条选择器独立判断、命中任一即跑（并集）。
不必为"每天 + 每周四"发明语义 —— 给了 `selectors` 就表示"每天"那条不参与。
空数组 = 每天（`{"type":"daily"}` 是等价的写法，允许但没必要）。

`weekdays` 的数字口径是 ISO：`1 = 周一 … 7 = 周日`（`datetime.isoweekday()` 同一套）。
这与 cron 的 `0/7 = 周日` 不一样，而两个都叫"星期几"，
:func:`parse_rule` 对 `0` 明确报错并给路线（那是 cron 口径，很容易写错）。

## 语义边界（写在这里，免得调用方各自猜）

  · `now` 必须是带时区的 `datetime`；返回值也是带时区的。
  · 返回值一律严格晚于 `now`（`once` 已经过点了就返回 `None`）。
    `next_after` 对 `repeat` 不会返回 `now` 那一刻 ——
    判据是「下一次」，把"现在这一刻"还回去会让调度器在同一秒里反复跑。
  · `every` 不锚定：给 `prev` 就按 `prev + N` 推（保住节拍、不让轮询间隔吃掉精度），
    没给就 `now + N`。
  · DST 空洞不特判 —— 本机（中国）没有夏令时；遇到空洞的时区，行为是
    "按本地墙钟解释"，不保证是那个时区的合法瞬间。没做，不是忘了。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

try:  # 标准库；Windows 上要有 `tzdata` 才认 IANA 名（见 requirements.txt）
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment]


#: 固定间隔的下限（秒）—— 5 分钟。
#: 超下限拒，不静默夹到 300。
MIN_INTERVAL_SECONDS = 300

#: 条数上限，超了拒、不静默顶掉。
MAX_JOBS = 50

_KINDS = ("repeat", "every", "once")
_SELECTOR_TYPES = ("daily", "weekdays", "month_days", "dates")
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RuleError(ValueError):
    """规则不合法。消息里必须带路线（不承诺兑现不了的事、也不空手拒）。"""


# ------------------------------------------------------------------
# 规则对象
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Selector:
    type: str
    values: tuple = ()


@dataclass(frozen=True)
class Rule:
    kind: str
    hour: int = 0
    minute: int = 0
    tz: Optional[str] = None
    selectors: tuple = field(default=())
    seconds: int = 0
    when: Optional[datetime] = None

    def as_json(self) -> dict:
        """回到 wire 形状（`cron_list` 要把规则原样给模型看）。"""
        if self.kind == "once":
            return {"kind": "once", "at": self.when.isoformat()} if self.when else {"kind": "once"}
        if self.kind == "every":
            return {"kind": "every", "seconds": self.seconds}
        out: dict[str, Any] = {"kind": "repeat",
                               "at": "%02d:%02d" % (self.hour, self.minute)}
        if self.tz:
            out["tz"] = self.tz
        out["selectors"] = [{"type": s.type, "values": list(s.values)}
                            for s in self.selectors]
        return out


def _tzinfo(name: Optional[str]) -> Optional[Any]:
    if not name:
        return None
    if ZoneInfo is None:
        raise RuleError("这台机器上没有 zoneinfo，不能按时区名解析；"
                        "去掉 tz 字段按本机时区走，或升级 Python")
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise RuleError("认不出时区 %r（要 IANA 名，如 Asia/Shanghai）：%s"
                        % (name, exc)) from exc


def _hhmm(raw: Any) -> tuple:
    if not isinstance(raw, str) or not _HHMM.match(raw):
        raise RuleError("at 要是 24 小时制的 \"HH:MM\"（例 \"08:30\"），收到 %r" % (raw,))
    hh, mm = raw.split(":")
    return int(hh), int(mm)


def _values(sel_type: str, values: Any) -> tuple:
    if not isinstance(values, (list, tuple)) or not values:
        raise RuleError("selectors 里 type=%r 的 values 要是非空数组" % sel_type)
    if sel_type == "weekdays":
        out = []
        for v in values:
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 7):
                raise RuleError(
                    "weekdays 用 **ISO 口径 1=周一 … 7=周日**（不是 cron 的 0=周日）；"
                    "收到 %r" % (v,))
            out.append(v)
        return tuple(sorted(set(out)))
    if sel_type == "month_days":
        out = []
        for v in values:
            if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 31):
                raise RuleError("month_days 要是 1..31 的整数（每月几号），收到 %r" % (v,))
            out.append(v)
        return tuple(sorted(set(out)))
    # dates: [[月, 日], …]
    out = []
    for v in values:
        if (not isinstance(v, (list, tuple)) or len(v) != 2
                or not all(isinstance(x, int) and not isinstance(x, bool) for x in v)):
            raise RuleError("dates 要是 [[月, 日], …]（例 [[4,21],[6,1]]），收到 %r" % (v,))
        m, d = int(v[0]), int(v[1])
        if not (1 <= m <= 12) or not (1 <= d <= 31):
            raise RuleError("dates 的月要 1..12、日要 1..31，收到 [%d, %d]" % (m, d))
        if d > calendar.monthrange(2000, m)[1] and d != 29:
            # 2000 是闰年，monthrange 给的是该月上限。【不拒 2/29】
            raise RuleError("%d 月没有 %d 日" % (m, d))
        out.append((m, d))
    return tuple(sorted(set(out)))


def parse_rule(raw: Any) -> Rule:
    """把 wire 上的 dict 解析成 :class:`Rule`。不合法一律 :class:`RuleError`。

    错误消息带路线（写错星期几是最常见的一种，所以那一条专门点名 ISO 口径）。
    """
    if not isinstance(raw, dict):
        raise RuleError("rule 要是一个对象（dict），收到 %r" % (type(raw).__name__,))

    kind = raw.get("kind")
    if kind not in _KINDS:
        raise RuleError("kind 要是 %s 之一，收到 %r"
                        % (" / ".join(repr(k) for k in _KINDS), kind))

    tz_name = raw.get("tz") or None
    _tzinfo(tz_name)          # 早失败：非法时区名在这里就拒

    if kind == "once":
        at = raw.get("at")
        if not isinstance(at, str) or not at.strip():
            raise RuleError("kind=once 要有 at（ISO 时刻，**带偏移**最好，"
                            "例 \"2026-10-20T09:00:00+08:00\"）")
        try:
            when = datetime.fromisoformat(at.strip())
        except ValueError as exc:
            raise RuleError("at 不是合法 ISO 时刻：%r" % (at,)) from exc
        if when.tzinfo is None:
            # 宽松收下（模型容易漏偏移），按任务的 tz / 本机解释 —— 但要说出来
            when = when.replace(tzinfo=_tzinfo(tz_name) or datetime.now().astimezone().tzinfo)
        return Rule(kind="once", when=when, tz=tz_name)

    if kind == "every":
        secs = raw.get("seconds")
        if not isinstance(secs, int) or isinstance(secs, bool):
            raise RuleError("kind=every 要有 seconds（整数秒），收到 %r" % (secs,))
        if secs < MIN_INTERVAL_SECONDS:
            raise RuleError("固定间隔最短 %d 秒（%d 分钟）；收到 %d 秒"
                            % (MIN_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS // 60, secs))
        return Rule(kind="every", seconds=secs, tz=tz_name)

    # kind == "repeat"
    hour, minute = _hhmm(raw.get("at"))
    sels_raw = raw.get("selectors") or []
    if not isinstance(sels_raw, (list, tuple)):
        raise RuleError("selectors 要是数组（给空数组 = 每天）")
    sels = []
    for item in sels_raw:
        if not isinstance(item, dict):
            raise RuleError("selectors 的每一项要是对象，收到 %r" % (type(item).__name__,))
        st = item.get("type")
        if st not in _SELECTOR_TYPES:
            raise RuleError("selector 的 type 要是 %s 之一，收到 %r"
                            % (" / ".join(repr(t) for t in _SELECTOR_TYPES), st))
        if st == "daily":
            sels.append(Selector("daily"))
        else:
            sels.append(Selector(st, _values(st, item.get("values"))))
    return Rule(kind="repeat", hour=hour, minute=minute,
                tz=tz_name, selectors=tuple(sels))


# ------------------------------------------------------------------
# 下一次触发时刻
# ------------------------------------------------------------------

def _aware_now(now: datetime, tzinfo: Optional[Any]) -> datetime:
    if now.tzinfo is None:
        raise RuleError("now 必须是**带时区**的 datetime（裸的本地时间没有唯一解释）")
    return now.astimezone(tzinfo) if tzinfo is not None else now


def _at_local(day: date, hour: int, minute: int, tzinfo: Any) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=tzinfo)


def _next_daily(now: datetime, hour: int, minute: int) -> datetime:
    today = _at_local(now.date(), hour, minute, now.tzinfo)
    return today if today > now else today + timedelta(days=1)


def _next_weekdays(now: datetime, hour: int, minute: int, days: Iterable[int]) -> Optional[datetime]:
    want = set(days)
    for k in range(0, 8):
        day = now.date() + timedelta(days=k)
        cand = _at_local(day, hour, minute, now.tzinfo)
        if cand <= now:
            continue
        if day.isoweekday() in want:
            return cand
    return None


def _next_month_days(now: datetime, hour: int, minute: int, days: Iterable[int]) -> Optional[datetime]:
    """该月没有那一天，这个月不触发（照 cron 的既有语义），不夹到月末。"""
    want = set(days)
    y, m = now.year, now.month
    for _ in range(0, 60):                      # 60 个月够了（5 年）
        last = calendar.monthrange(y, m)[1]
        for d in sorted(d for d in want if d <= last):
            cand = _at_local(date(y, m, d), hour, minute, now.tzinfo)
            if cand > now:
                return cand
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return None


def _next_dates(now: datetime, hour: int, minute: int, pairs: Iterable[tuple]) -> Optional[datetime]:
    """每年的那几个 (月, 日)。该年没有（如 2/29 的平年），跳到下一年。"""
    for year in range(now.year, now.year + 9):
        cands = []
        for (m, d) in pairs:
            if d > calendar.monthrange(year, m)[1]:
                continue
            cands.append(_at_local(date(year, m, d), hour, minute, now.tzinfo))
        for cand in sorted(cands):
            if cand > now:
                return cand
    return None


def next_after(rule: Rule, now: datetime, prev: Optional[datetime] = None) -> Optional[datetime]:
    """严格晚于 *now* 的下一次触发时刻；没有下一次则 `None`。

    *prev* 只对 `every` 有意义（保住节拍）。其余 kind 忽略它。
    """
    tzinfo = _tzinfo(rule.tz)
    now = _aware_now(now, tzinfo)

    if rule.kind == "once":
        if rule.when is None:
            return None
        when = rule.when if tzinfo is None else rule.when.astimezone(tzinfo)
        return when if when > now else None

    if rule.kind == "every":
        step = timedelta(seconds=rule.seconds)
        base = prev if prev is not None else now
        if base.tzinfo is None:
            base = base.replace(tzinfo=now.tzinfo)
        else:
            base = base.astimezone(now.tzinfo)
        if base > now:
            return base
        n = int((now - base) / step) + 1        # 错过就跳过（不补积压），跳到下一个锚点
        return base + step * n

    if not rule.selectors:
        return _next_daily(now, rule.hour, rule.minute)

    cands = []
    for sel in rule.selectors:
        if sel.type == "daily":
            cands.append(_next_daily(now, rule.hour, rule.minute))
        elif sel.type == "weekdays":
            cands.append(_next_weekdays(now, rule.hour, rule.minute, sel.values))
        elif sel.type == "month_days":
            cands.append(_next_month_days(now, rule.hour, rule.minute, sel.values))
        elif sel.type == "dates":
            cands.append(_next_dates(now, rule.hour, rule.minute, sel.values))
    cands = [c for c in cands if c is not None]
    return min(cands) if cands else None


# ------------------------------------------------------------------
# 中文预览（面板要显示；解释规则只许在这一处）
# ------------------------------------------------------------------

_WEEK_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}


def describe(rule: Rule) -> str:
    """一句话说清"什么时候跑"。面板与工具回执共用（别在别处再写一份）。"""
    if rule.kind == "once":
        return "仅一次：%s" % (rule.when.strftime("%Y-%m-%d %H:%M") if rule.when else "?")
    if rule.kind == "every":
        secs = rule.seconds
        if secs % 3600 == 0:
            return "每 %d 小时" % (secs // 3600)
        if secs % 60 == 0:
            return "每 %d 分钟" % (secs // 60)
        return "每 %d 秒" % secs

    hhmm = "%02d:%02d" % (rule.hour, rule.minute)
    if not rule.selectors:
        return "每天 %s" % hhmm
    parts = []
    for sel in rule.selectors:
        if sel.type == "daily":
            parts.append("每天")
        elif sel.type == "weekdays":
            parts.append("每周" + "、".join("周" + _WEEK_CN[v] for v in sel.values))
        elif sel.type == "month_days":
            parts.append("每月 " + "/".join(str(v) for v in sel.values) + " 日")
        elif sel.type == "dates":
            parts.append("每年 " + "、".join("%d月%d日" % (m, d) for m, d in sel.values))
    return "%s 的 %s" % (" 或 ".join(parts), hhmm)


#: 面板那句要跟随界面语言，所以星期也有一份可翻译的键（`describe()` 用不着）。
#: 写成模块级常量是为了扫描器收得到（它只收字面量，不收 `"周" + x` 拼出来的）。
_WEEK_DAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def describe_i18n(rule: Rule) -> str:
    """同 `describe()`，但按界面语言说 —— 只给面板。

    工具回执仍走 `describe()`：那是给模型看的，不翻（`locales/README.md` §2）。
    分支与 `describe()` 一一对应；解释规则这件事仍然只在这一处
      （两个函数挨着放，改规则时一起改）。
    """
    from core.i18n import current, t, tr_fmt

    if rule.kind == "once":
        at = rule.when.strftime("%Y-%m-%d %H:%M") if rule.when else "?"
        return tr_fmt("仅一次：{at}", at)
    if rule.kind == "every":
        secs = rule.seconds
        # `n == 1` 单列一档：英文 "Every 1 hours" 是病句（中文这边
        #   「每 1 小时」本来也拗口）。
        if secs % 3600 == 0:
            n = secs // 3600
            return tr_fmt("每小时") if n == 1 else tr_fmt("每 {n} 小时", n)
        if secs % 60 == 0:
            n = secs // 60
            return tr_fmt("每分钟") if n == 1 else tr_fmt("每 {n} 分钟", n)
        return tr_fmt("每 {n} 秒", secs)

    hhmm = "%02d:%02d" % (rule.hour, rule.minute)
    if not rule.selectors:
        return tr_fmt("每天 {at}", hhmm)
    #: 列表分隔符 —— 不进表：扫描器只收汉字，纯标点（`、`）永远成不了一行
    #:它是排版不是句子。加一门语言时在这里补一个。
    sep = "、" if current() == "zh" else ", "
    parts = []
    for sel in rule.selectors:
        if sel.type == "daily":
            parts.append(t("每天"))
        elif sel.type == "weekdays":
            days = sep.join(t(_WEEK_DAYS[v - 1]) for v in sel.values)
            parts.append(tr_fmt("每周{days}", days))
        elif sel.type == "month_days":
            parts.append(tr_fmt("每月 {days} 日",
                                "/".join(str(v) for v in sel.values)))
        elif sel.type == "dates":
            parts.append(tr_fmt("每年 {dates}", sep.join(
                tr_fmt("{m}月{d}日", m, d) for m, d in sel.values)))
    return tr_fmt("{parts} 的 {at}", (" " + t("或") + " ").join(parts), hhmm)
