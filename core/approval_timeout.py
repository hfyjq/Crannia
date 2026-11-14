"""审批超时 —— auto 档下"没人答"的那条出路。

## 它治什么

目标：拦截危险动作，但不要把模型堵死 —— 否则自动模式就没意义了
（用户离开了，框架却老是断掉）。

自动模式的前提就是"用户不在"，在那条路上，「停下来问用户」**不是一条出路，
是这一轮的终点**。

判据：auto 档下任何弹出来的窗，本来都是"分类器没能代答" ——
它判安全就预授权（不弹）、判危险就回一句拒绝（也不弹），
凡是弹出来的，一定是它没能代答（没答上来 / 熔断 / 不知道怎么放行）。
不用再去区分是哪一条回退路径，判据就是 `mode == "auto"`。

## 三条定案

| | |
|---|---|
| 范围 | `auto` 与 `full_access` 两档 —— 这两档的前提都是「用户可能不在」。手动 / 计划不挂：人在场，超时会把正要批的事毙掉 |
| 到点结算成什么 | `unavailable`，不是 `rejected` —— 逐字 *"没有可问的通道，不是用户的决定"*。模型该做的是"换个做法"，不是"停下来"。结算那句话不在这里写，走 `core/approval.py::outcome_line`（全仓唯一一处） |
| 秒数 | 默认 120，设置页可改，`0` = 关 |

## 失效方向（辅助件坏了，不替用户做决定）

这条是辅助件，不是判据。坏值（非整数 / `true` / 越大越离谱）一律
关掉（回旧行为：挂起），不是用默认值硬套 ——
「因为一个坏配置把用户的事自动毙掉」比「超时没生效」坏得多。
而且出声（`print` 一行）：静默忽略一个用户改过的键，比不许改更坏。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

__all__ = [
    "SETTING_KEY", "DEFAULT_SECONDS", "seconds", "should_arm", "note_bad_setting",
    "forget_noted",
]

#: 设置里那个键。
SETTING_KEY = "approval_timeout_seconds"

#: 默认秒数。 `0` 是合法值 = 关。
#:
#: 取 120：两分钟足够用户回来点一下，再长就等于"这一轮白等"。
#:   设置页那个键照旧可改（`0` = 关）。
DEFAULT_SECONDS = 120

#: 相对路径：`tests/_sandbox.py::sandbox()`
#: 靠 chdir 生效，写绝对路径就等于测试去动生产数据。
_SETTINGS_PATH = Path("data/settings.json")

_lock = threading.Lock()

#: 坏值只喊一次（免得每条请求打一行）。
_bad_noted: set[str] = set()


def note_bad_setting(session_id: str = "") -> None:
    """这个会话里已经因为坏配置喊过一次了。"""
    with _lock:
        _bad_noted.add(str(session_id or ""))


def _already_noted(session_id: str) -> bool:
    with _lock:
        return str(session_id or "") in _bad_noted


def forget_noted(session_id: str = "") -> None:
    """忘掉"这个会话已经喊过坏配置"—— `/clear` 时清（会话级状态登记表里有它）。

    不清的后果是实质的：用户改了一个看不懂的值，我们喊一声；他 `/clear`
    之后同一份坏配置不再出声，那就是"静默忽略一个用户改过的键"，
    而那比不许改更坏。

    `session_id` 为空串 = 清全部（给测试与"进程重建"用）。
    """
    with _lock:
        if not session_id:
            _bad_noted.clear()
        else:
            _bad_noted.discard(str(session_id))


def seconds(session_id: str = "") -> int:
    """设置里写的秒数。`0` = 关。

    读不到，默认（没有文件 / 没有这个键 / JSON 坏了）；
    读到了但是坏值，关 + 出声（见模块 docstring 的"失效方向"）。
    """
    try:
        if not _SETTINGS_PATH.exists():
            return DEFAULT_SECONDS
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or SETTING_KEY not in raw:
            return DEFAULT_SECONDS
        value = raw[SETTING_KEY]
    except Exception:
        # 文件坏了，这属于「读不到」，默认，不让一轮对话因为一个设置失败。
        return DEFAULT_SECONDS

    # `bool` 必须先挡 —— `isinstance(True, int)` 是 `True`
    # （`effort_level: true` 会被当成档 1）。
    if isinstance(value, bool) or not isinstance(value, int):
        if not _already_noted(session_id):
            note_bad_setting(session_id)
            print("[approval-timeout] 设置项 %r 不是整数（收到 %r）—— **本次不启用超时**"
                  "（回今天的行为：挂起）。改 data/settings.json 修好它。"
                  % (SETTING_KEY, value))
        return 0

    if value <= 0:
        return 0                      # 合法值：关
    if value > 3600:
        # 离谱的大数：多半是写错了（单位？）。同样往"关"那边倒。
        if not _already_noted(session_id):
            note_bad_setting(session_id)
            print("[approval-timeout] 设置项 %r = %d 秒，超过一小时 —— "
                  "**本次不启用超时**。确认你要的是这个数就改小它。"
                  % (SETTING_KEY, value))
        return 0
    return value


def should_arm(session_id: str = "") -> bool:
    """这次挂起要不要挂倒计时。

    挂 `auto` 与 `full_access` 两档。

    · 这两档的共同前提是「用户可能不在」，没人答就必须有条出路，
      否则整轮永远挂着；
    · 手动 / 计划不挂 —— 那两档人就在场，超时自动结算会把他正要批的
      那件事也毙掉（这条是原来的判据，仍然成立）。

    模式读不到 / 报错，不挂（保守侧）。
    """
    if seconds(session_id) <= 0:
        return False
    try:
        from core.modes import current_mode
        return current_mode(session_id) in ("auto", "full_access")
    except Exception:                                       # noqa: BLE001
        return False


# 定时器表 —— 放在本模块（模块级），不放在 `PipelineScheduler` 上
#
# 为什么必须是模块级：`state_registry.purge_session()` 的清理函数走
# `_lazy(module, func)`，只认模块级函数；而 scheduler 是个实例
# （没有模块级单例入口）。放实例上，`/clear` 清不掉它，上一段对话那条
# 倒计时接着跑、到点去动新一段对话的审批。
#
# 每个会话一格：`{"event": <那条真 MessageEvent>, "timer": <TimerHandle|None>}`。
# 存那条 event 是为了超时之后接着跑一轮时模型的话发得出去
#   （靠它的 `reply_callback`；造一条假 event 就没有回执通道）。
_timers: dict[str, dict] = {}


def remember(session_key: str, event) -> None:
    """轮首记下这条真事件（调度器每轮调一次）。"""
    slot = _timers.get(session_key)
    if slot is None:
        slot = {"event": None, "timer": None}
        _timers[session_key] = slot
    slot["event"] = event


def event_of(session_key: str):
    return (_timers.get(session_key) or {}).get("event")


def cancel(session_key: str) -> None:
    """撤掉这个会话的倒计时（不删那一格 —— event 还要留着给下一轮）。"""
    slot = _timers.get(session_key)
    if not slot:
        return
    h = slot.get("timer")
    if h is not None:
        try:
            h.cancel()
        except Exception:                      # noqa: BLE001
            pass
        slot["timer"] = None


def arm(session_key: str, loop, on_fire) -> bool:
    """该挂就挂。返回是否新挂了（已经挂着的返回 `False`，幂等）。"""
    if not should_arm(session_key):
        return False
    slot = _timers.get(session_key)
    if slot is None or slot.get("event") is None:
        return False                            # 没记住那条事件，挂不了
    if slot.get("timer") is not None:
        return False                            # 已经挂着
    delay = seconds(session_key)
    if delay <= 0:
        return False
    slot["timer"] = loop.call_later(float(delay), on_fire, session_key)
    return True


def mark_fired(session_key: str) -> None:
    """到点了 —— 把句柄清掉（回调第一件事）。"""
    slot = _timers.get(session_key)
    if slot:
        slot["timer"] = None


def forget(session_key: str = "") -> None:
    """清掉（`/clear` 时由 `state_registry.purge_session` 调）。

    不清的后果是实质的：上一段对话那条倒计时接着跑，
    到点会去动新一段对话的审批（它按会话键找 pending）。
    `session_key` 为空串 = 清全部。
    """
    keys = [session_key] if session_key else list(_timers)
    for k in keys:
        slot = _timers.pop(k, None) or {}
        h = slot.get("timer")
        if h is not None:
            try:
                h.cancel()
            except Exception:                  # noqa: BLE001
                pass
