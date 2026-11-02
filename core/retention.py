"""留档的生命周期（`/clear` 的收口）。

判据：`/clear` 是「清」不是「删」 —— 从当前会话里摘掉的
派生物留在盘上。而留档不等于永久：用一个数控制它活多久。

## 覆盖谁（以及为什么日报不在这里）

| 制品 | 保留 | 为什么 |
|---|---|---|
| 归档的会话笔记 `sessions/<键>.<时间戳>.md` | 本模块 | `/clear` 的产物 |
| 计划文件 `data/plans/*.md` | 本模块 | `/clear` 之后不再被引用的产物 |
| 日报 `logs/<日期>.md` | 它自己那套（`core/daily_log.py::RETENTION_DAYS = 90`） | 见下 |

日报有意不并进这一个数：
日报是用户清空之后仅存的对话记录（一轮一段、框架直写、零 LLM），
它比归档笔记还该留久 —— 把 90 天缩到 30 天会加重"删得太激进"那件事。
一条"统一成一个数"的整洁，不该换来一次静默的数据缩水。（要统一就把它改成读这里那个键，一行的事。）

## 读不到保留期，不清理，不是"默认 30 天再删"

这与本仓那条「读不到，默认」方向相反，是有意的，两个独立出处：

- `core/daily_log.py::_prune` 自己写着：*「不认识的形状一律不碰：
  宁可留下一个来路不明的文件，也不误删」*；
- 设置校验出错且用户显式设过保留期，整个清理跳过，不回退默认 —— 理由是
  宁可不清理，也不按默认值误删（用户可能只是想要另一个保留期）

一个「删」的动作，读不到值时的真值不是"默认"，是"不动"。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

#: 默认保留天数。只管归档件与计划文件，
#: 日报有它自己那个 90（见模块 docstring，那里写了为什么）。
DEFAULT_CLEANUP_PERIOD_DAYS = 30

#: 设置文件（与 `core/tiers.py` / `core/allowlist.py` 同一份）。
#:
#: 键名 `cleanupPeriodDays` 是互操作，不是我们的命名习惯：已经配过它的人
#: 换过来不用重配。所以哪怕哪天改自己的字段名，这一个也别动。
SETTING_KEY = "cleanupPeriodDays"
_SETTINGS_PATH = Path("data/settings.json")

#: 兜底上限/下限 —— 防一个手抖写出来的 `cleanupPeriodDays: 0` 把今天刚归档的
#: 东西立刻删掉。`0` 不是"立刻清"，是"读不懂"（见 docstring 那条方向）。
_MIN_DAYS = 1
_MAX_DAYS = 3650


def configured_cleanup_days(path: Optional[Path] = None) -> Optional[int]:
    """读保留天数；读不到 / 坏值，`None`（调用方的约定是"那就一个都别删"）。

    坏值包括：文件不存在 · JSON 坏了 · 不是对象 · 值不是整数 · 布尔 ·
    越界。 `isinstance(True, int)` 是 `True`，布尔要单独挡。
    """
    p = path or _SETTINGS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get(SETTING_KEY, DEFAULT_CLEANUP_PERIOD_DAYS)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not (_MIN_DAYS <= value <= _MAX_DAYS):
        return None
    return value


def _unlink_if_old(path: Path, cutoff: datetime) -> bool:
    """mtime 早于 `cutoff`，删。只认 mtime。"""
    try:
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            path.unlink()
            return True
    except OSError:
        pass
    return False


def prune_archived_notes(sessions_dir, days: Optional[int] = None,
                         now: Optional[datetime] = None) -> int:
    """清掉过期的归档会话笔记，返回删了几个。

    只碰归档件（`SessionMemory.is_archive_stem`）—— 活的 `<键>.md`
    是当前会话正在用的那份，任何时候都不许动。
    判据出口只有一个（`is_archive_stem`），别在这儿再写一份正则。
    """
    from core.memory.session_memory import SessionMemory

    if days is None:
        days = configured_cleanup_days()
    if days is None:
        return 0                       # 读不到，不清理（见模块 docstring）
    root = Path(sessions_dir)
    if not root.is_dir():
        return 0
    cutoff = (now or datetime.now()) - timedelta(days=days)

    removed = 0
    for p in root.glob("*.md"):
        if not SessionMemory.is_archive_stem(p.stem):
            continue
        if _unlink_if_old(p, cutoff):
            removed += 1
    if removed:
        print("[retention] 清掉了 %d 份超过 %d 天的归档会话笔记" % (removed, days))
    return removed


def prune_generic(directory, days: Optional[int] = None,
                  now: Optional[datetime] = None,
                  suffixes: tuple = (".md",)) -> int:
    """通用版：某个目录下按扩展名 + mtime 清（`data/plans/` 用得上）。

    与 `prune_archived_notes` 分开是有意的：那一份只许碰归档件，
    而这一份会删目录里所有匹配扩展名的文件 —— 混用一次就会把活的笔记删掉。
    """
    if days is None:
        days = configured_cleanup_days()
    if days is None:
        return 0
    root = Path(directory)
    if not root.is_dir():
        return 0
    cutoff = (now or datetime.now()) - timedelta(days=days)
    removed = 0
    for suffix in suffixes:
        for p in root.glob("*%s" % suffix):
            if _unlink_if_old(p, cutoff):
                removed += 1
    return removed


#: 框架自己写的运行目录 —— `(目录, 后缀（`None` = 全部文件）, 保留天数（`None` = 读设置）)`。
#:
#: 这些目录原来谁都不收。判据与 `prune_generic` 那条一致：
#: 一个「删」的动作，读不到保留期就一个都不删；只删文件、不删目录。
#: 加一条之前先回答"这里的东西还有没有人按路径去找" ——
#:   工具结果与通道收件媒体都会在历史消息里留下指针，所以它们用的是
#:   同一个（较长的）保留期，而不是"随便清"。
_RUNTIME_DIRS = (
    # 计划文件：`core/plan.py` 的落点，`/clear` 之后不再被引用。
    ("data/plans", (".md",), None),
    # 工具结果落盘（指针在历史消息里）：那个目录是可读区，保留期用同一个数。
    ("data/workdata/tool_results", None, None),
    # 四个通道的收件媒体（附件原始件）。
    ("data/workdata/feishu_files", None, None),
    ("data/workdata/telegram_files", None, None),
    ("data/workdata/dingtalk_files", None, None),
    ("data/workdata/wechat_files", None, None),
    # 后台任务日志：纯诊断，短一点。
    ("data/runtime/tasks", (".log",), 7),
    # 事故账本：按天分文件，但它此前没有保留期，只会一直长。
    #   90 天够查一次事故，与日报同一个量级。
    ("data/incidents", (".jsonl",), 90),
)


def prune_runtime_dirs(now: Optional[datetime] = None) -> dict:
    """把上面那张表扫一遍，返回 `{相对目录: 删掉的个数}`。

    只删文件；目录结构留着（它们是下一次写入的落点）。
    读不到保留期 -> 对应那几条一个都不删（与模块 docstring 同一条方向）。
    """
    out: dict = {}
    cutoff_days = configured_cleanup_days()
    for directory, suffixes, days in _RUNTIME_DIRS:
        use_days = days if days is not None else cutoff_days
        if use_days is None:
            out[directory] = 0            # 读不到保留期：不动
            continue
        root = Path(directory)
        if not root.is_dir():
            out[directory] = 0
            continue
        cutoff = (now or datetime.now()) - timedelta(days=use_days)
        patterns = ["*"] if suffixes is None else ["*%s" % s for s in suffixes]
        removed = 0
        for pattern in patterns:
            # 递归（任务日志是 `tasks/<存储 id>/<任务 id>.log` 两层）。
            for p in root.rglob(pattern):
                if p.is_file() and _unlink_if_old(p, cutoff):
                    removed += 1
        out[directory] = removed
        if removed:
            print("[retention] %s：清掉 %d 个超过 %d 天的文件"
                  % (directory, removed, use_days))
    return out


__all__: List[str] = [
    "DEFAULT_CLEANUP_PERIOD_DAYS", "SETTING_KEY",
    "configured_cleanup_days", "prune_archived_notes", "prune_generic",
    "prune_runtime_dirs",
]
