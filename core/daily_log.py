"""Daily log —— 一轮一段的框架直写日志。

    <storage_path>/logs/YYYY-MM-DD.md      （默认 data/memory/logs/）

三条设计判据：

  1. 框架直接写，不走 LLM（零 API 成本）。内容 = 一轮的用户话 + 助手话，
     各折叠空白 + 截断 —— 这是指路级的日报，不是全文转录；
     明细（全文）在 `short_term.db`，权威在那里。
  2. `/clear` 不清：文件按日期建、与会话无关 —— 这是它与「会话笔记」
     （`<storage_path>/sessions/<sid>.md`，随会话消失）的唯一但决定性的区别。
它绝不出现在 `purge_session` 的清单里（有测试钉着这条）。
  3. 保留 90 天：append 时顺手剪（一次 `iterdir`，删比今天老 90 天的
     `*.md`，出声不算静默）。日轮转 + 这一条 = 无界增长在这里不成立。

## 与「记忆巩固」的关系

巩固时把它当"那天发生了什么"的原料（`core/memory_organize.py` 的素材清单里
第一类就是它）。写入口径要经得起几周后被读：
带时间、带会话、截断在句子可能中断的地方要能看出来（`...`）。

## 两条写在案的取舍

- 路径由调用方传进来（manager 按 `storage_path` 推导，同 `sessions/` 那条）——
  不是 cwd 相对常量。带 `storage_path` 的测试因此自动落自己的临时目录，
  不用逐测试钉路径，也就没有"忘了钉 = 写生产"这个口子。
- 全局一把锁（不是每目录一把）：本地小文件 append，竞争可以忽略；
  每目录一把锁要多一张表，收益为零。并发下两个会话同一秒收尾也必须串好。

ASCII-only 打印（Windows GBK 控制台）。
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

#: 保留天数。判据是 `> RETENTION_DAYS` —— 第 90 天整天还在。
RETENTION_DAYS = 90

#: 每侧（用户话 / 助手话）的截断长度。日报是指路，不是转录。
_MAX_SIDE_CHARS = 300

#: 文件名形状（`YYYY-MM-DD.md`）—— 剪枝按 stem 解析日期，不认别的形状
#: （不认识的 `*.md` 一律不碰：宁可留下一个来路不明的文件，也不误删）。
_append_lock = threading.Lock()


def _collapse(text, limit: int = _MAX_SIDE_CHARS) -> str:
    """折叠空白 + 截断（截断点给 `...`，让几周后的读者看出来字被截过）。"""
    s = " ".join(str(text if text is not None else "").split())
    if len(s) > limit:
        s = s[:limit] + "..."
    return s


def _entry(session_id: str, user_text, assistant_text, now: datetime) -> str:
    return (
        f"\n## {now.strftime('%H:%M')} · {session_id}\n"
        f"- 用户: {_collapse(user_text)}\n"
        f"- 助手: {_collapse(assistant_text)}\n"
    )


def _prune(logs_dir: Path, now: datetime) -> int:
    """删掉比今天老 超过 `RETENTION_DAYS` 天的 `YYYY-MM-DD.md`。返回删了几个。"""
    cutoff = now.date() - timedelta(days=RETENTION_DAYS)
    removed = 0
    for p in logs_dir.glob("*.md"):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue                      # 不认识的形状：不碰（见模块 docstring）
        if d < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"[daily_log] 剪掉了 {removed} 个超过 {RETENTION_DAYS} 天的日报文件")
    return removed


def log_turn(logs_dir, session_id: str, user_text, assistant_text,
             now: Optional[datetime] = None) -> Optional[Path]:
    """把一轮追加进当天的日报。返回写入的文件路径；失败出声但返回 None。

    永不抛异常：它是记录不是主活 —— 日志写不进去不该连累对话
    （判据 4：不静默，也不阻断）。
    """
    now = now or datetime.now()
    logs_dir = Path(logs_dir)
    target = logs_dir / (now.strftime("%Y-%m-%d") + ".md")
    try:
        with _append_lock:
            logs_dir.mkdir(parents=True, exist_ok=True)
            _prune(logs_dir, now)
            fresh = not target.exists()
            with open(target, "a", encoding="utf-8", newline="\n") as fh:
                if fresh:
                    fh.write(f"# {now.strftime('%Y-%m-%d')}\n")
                fh.write(_entry(session_id, user_text, assistant_text, now))
        return target
    except OSError as exc:
        print(f"[daily_log] 写不进去（{type(exc).__name__}: {exc}）—— 本轮日报缺一段")
        return None
