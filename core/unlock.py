# -*- coding: utf-8 -*-
"""权限解禁 —— 完全权限模式下把剩下的那几道阻力也拿掉。

设置 → 安全设置里有一个开关：**打开后完全权限模式下
agent 的行动不再有任何阻力**。界面那一侧必须先警告一次（3 秒倒计时）。

## 它到底解什么

完全权限（`full_access`）今天已经做到「审批不问而放」，留着的是**四道确定性
阻力**，它们不是审批、所以 `_full_access_gate` 够不着：

| | 今天 | 解禁后 |
|---|---|---|
| 钩子说 `ask` | 弹窗 | 不问，照常执行 |
| 批量删除熔断 | 仍问一次 | 不问 |
| `deny` 档命令（`curl`/`wget`/`ssh`…） | 确定性拒绝 | 放行（等于出网不再有地板） |
| 受保护路径（`.env` / `data/memory/` / `logs/` / 系统目录…） | `FORBIDDEN` | 当普通路径处理 |
| ICE 信息层 | 完全权限下已派生关闭 | 同左（这一条 `full_access` 本来就管了） |

钩子的 `blocked`（退出码 2）不在解禁范围内 —— 那是用户自己写的规则，
不是框架的保守设定。解禁解的是框架的阻力；用户明说"别做"的那一句照旧生效。

接管（`computer` / `browser`）仍然会问 —— 那是逐应用的授权
（`core/computer/session.py`），没有"一次全批"的载体；而它不在常规执行路径上。
真的碰到了也不会卡死：`auto` / `full_access` 两档都有 120 秒无人应答闸
（`core/approval_timeout.py`）。

## 两条硬约束

1. 只在完全权限模式下生效：判据是 `enabled() and mode == "full_access"`。
   别的模式打开这个开关 = 什么都不变（免得"按了好像有点用"）。
2. 只认真布尔：`"true"` / `1` / `yes` 一律当没写（与其余布尔配置同一条）。
   读不到，关。

## 怎么传到执行层

审批与钩子在 `agent_loop` 里跑，而 `deny` 档、`FORBIDDEN`、批量删除检查都在
工具 handler 里面（它们拿不到 `session_id`），用 `ContextVar` 传：
`AgentLoopStage.process` 每轮 `pin(session_id)`（形状照
`core/session_workspace.pin`），handler 里问 `bypassing()`。

`run_in_executor` 那条并发路要 `copy_context`（`A7` 那处已经包了）——
不传的话解禁在并发批里静默失效，而症状是"有时有效有时无效"。
"""

from __future__ import annotations

import json
import threading
from contextvars import ContextVar
from pathlib import Path

__all__ = [
    "SETTING_KEY", "enabled", "active", "bypassing", "pin", "reset",
]

#: `data/settings.json` 里的键。
SETTING_KEY = "full_access_unlock"

_SETTINGS_PATH = Path("data/settings.json")

_lock = threading.Lock()
#: 坏值只喊一次（`(path, mtime_ns, size)` 变了就重新读）。
_cache: tuple = ()
_cache_value: bool = False
_bad_noted = False

#: 本轮是不是在"解禁"下跑。默认 `False`（fail-closed）。
_BYPASS: ContextVar[bool] = ContextVar("crannia_unlock_bypass", default=False)


def _read() -> bool:
    """读设置。读不到，关；坏值，关 + 出声一次。"""
    global _cache, _cache_value, _bad_noted
    try:
        st = _SETTINGS_PATH.stat()
    except OSError:
        return False
    key = (str(_SETTINGS_PATH), st.st_mtime_ns, st.st_size)
    with _lock:
        if key == _cache:
            return _cache_value
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return False
    value = raw.get(SETTING_KEY) if isinstance(raw, dict) else None
    #: 只认真布尔 —— `isinstance(True, int)` 是 `True`，所以先挡 bool 之外的一切。
    on = value is True
    if value is not None and not isinstance(value, bool) and not _bad_noted:
        _bad_noted = True
        print("[unlock] 设置项 %r 不是布尔（收到 %r）—— 按**关**处理。"
              "改 data/settings.json 里的 true/false。" % (SETTING_KEY, value))
    with _lock:
        _cache, _cache_value = key, on
    return on


def enabled() -> bool:
    """这个开关打开了吗（不看模式）。"""
    try:
        return _read()
    except Exception:                                       # noqa: BLE001
        return False


def active(session_id: str = "") -> bool:
    """这一轮是不是在解禁下：开关开着 且 这一段是完全权限。"""
    if not enabled():
        return False
    try:
        from core.modes import current_mode
        return current_mode(session_id) == "full_access"
    except Exception:                                       # noqa: BLE001
        return False


def pin(session_id: str = "") -> bool:
    """把这一轮的解禁状态钉进上下文（只设置、不重置，一轮一个上下文）。"""
    on = active(session_id)
    _BYPASS.set(on)
    if on:
        print("[unlock] 权限解禁生效：完全权限 + 开关打开 —— "
              "审批 / deny 档 / 受保护路径 / 批量删除熔断**都不再拦**")
    return on


def reset() -> None:
    """还原（测试用；生产那条路是"一轮一个新上下文"）。"""
    _BYPASS.set(False)


def bypassing() -> bool:
    """执行层问这一句：这次调用要不要跳过框架的阻力。"""
    try:
        return bool(_BYPASS.get())
    except Exception:                                       # noqa: BLE001
        return False
