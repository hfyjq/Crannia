"""出厂基准 —— 「恢复默认设置」恢复成的那一份。

## 它是什么

`data/settings.example.json` —— 一份已跟踪的文件（`git ls-files` 找得到），
里面是键名 + 出厂默认值，不含凭据、不含个人信息。同一份文件有两个读者：

| 读者 | 什么时候 |
|---|---|
| TUI 首次启动 | `heal()` 在文件不存在 / 坏了时按这份播种（GUI 侧仍不播种，走 inline 兜底） |
| ~~新装环境~~ | 没有这个读者（GUI 不播种）—— `data/settings.json` 不存在时，各模块的 inline 兜底生效，「缺键 = 出厂默认」这条语义因此活着（以后改默认值，已装用户拿得到） |
| 「恢复默认设置」 | 以它为准覆盖回去 —— 这就是本模块要的东西 |

## 为什么要把它提出来：「出厂默认」没有一处能回答

默认值散在各处的 inline 字面量里：
`core/workspace.py` 的 `settings.get("auto_approve", False)` ·
`core/memory_store.py` 的 `data.get("memory_write", True)` ·
`core/updater.py` 的 `settings.get("update_auto_check", True)` ·
`core/memory_organize.py` 的 `data.get("memory_organize", True)` …… 十几处。

「恢复默认」在没有基准之前无法实现（恢复到哪儿去？）。
「口径各一处」——这一份就是"出厂默认"那一处。

##  「恢复默认」有爆炸半径 —— 有一类键永远不许被它碰

基准文件里同时躺着用户数据和凭据（`web_token` 登录令牌 · `skill_configs`
邮箱账户与口令 · `work_groups` 用户配的工作组 · `watchers` 监听器 ·
`forbidden_directories` 规避目录 · `workspace` 工作区路径）。

它们出现在基准文件里是"首次运行生成时用"的，不是"恢复默认要写回去"的。
真按基准写回去，用户被登出、邮箱账户清空、工作组与监听器全丢 ——
而那会被读成"点了一下『恢复默认』，我的东西没了"。
`NEVER_RESTORE` 那张名单是这个模块存在的主要理由之一。

## 边界（如实标）

只有「恢复默认」这一条路读基准（`web/server.py` 的
`/api/settings/restore-default`）。散在各模块的那十几处 inline 默认值还没搬过来
—— 搬它们要逐个核"读不到时的行为"，是另一件活。搬完之前，
`tests/test_settings_baseline.py` 有一条机械读者盯着基准与模块常量不许分叉。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

#: 基准文件。模块相对，不是相对 cwd —— 它是一个仓库制品
#: （跟着代码走、进 git、不随运行位置变），而 `data/settings.json` 那种运行数据
#: 才是相对 cwd 的（`tests/_sandbox.py` 靠 chdir 把它们钉在临时目录）。
#: 写成 `Path("data/settings.example.json")` 会在 sandbox 里读不到
#: （测试 chdir 到临时目录，文件不在那儿）—— 那会让「恢复默认」在测试里
#: 静默变成空操作，而空操作看起来像"跑过了"。
#: 同类做法：`core/window_estimate.py::TABLE_PATH` · `core/updater.py::_PROJECT_ROOT`。
BASELINE_PATH = Path(__file__).resolve().parent.parent / "data" / "settings.example.json"

#: 基准里这几个键是用户数据 / 凭据，不是偏好 ——
#: 「恢复默认」永不许写回它们。逐条说得出理由：
#:
#: * `web_token` —— 写回空串 = 把用户登出
#: * `skill_configs` —— 里面是邮箱账户与明文口令（凭据排除的由来）
#: * `workspace` —— 用户的工作区路径
#: * `work_groups` / `watchers` —— 用户一条条配出来的，写回 `[]` = 全没
#: * `forbidden_directories` —— 用户加的规避目录，同上
NEVER_RESTORE: frozenset[str] = frozenset({
    "web_token",
    "skill_configs",
    "workspace",
    "work_groups",
    "watchers",
    "forbidden_directories",
})

_warned = False


def baseline(path: Optional[Path] = None) -> dict:
    """读基准文件（去掉 `_comment`）。读不到，空 dict + 出声一行。

    读不到 ≠ 默认值是空：这里返回 `{}` 的意思是
    「这一层没有意见」，调用方该退回自己的兜底 —— 而不是"把设置清空"。
    `plan_restore` 因此对空基准是空操作，不是"全清"。
    """
    global _warned
    p = path or BASELINE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not _warned:
            _warned = True
            print(f"[settings] 出厂基准读不到（{p}）—— 「恢复默认」会变成空操作")
        return {}
    except (OSError, ValueError) as exc:
        if not _warned:
            _warned = True
            print(f"[settings] 出厂基准坏了（{p}）：{type(exc).__name__}: {exc}")
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def restorable(path: Optional[Path] = None) -> dict:
    """基准里可以写回去的那部分 —— 见 `NEVER_RESTORE`。"""
    return {k: v for k, v in baseline(path).items() if k not in NEVER_RESTORE}


def plan_restore(current: Optional[dict] = None,
                 path: Optional[Path] = None) -> tuple[dict, dict, list]:
    """纯函数：算出"恢复默认之后长什么样"。

    返回 `(新设置, 将要改变的格, 被跳过的键)`：

    * `新设置` = 当前设置 ∪ 基准（基准覆盖）
    * `将要改变的格` = `{键: [现在, 基准]} ` —— 只列真的会变的，
      界面拿它显示"将要改变哪几格"（不许一键静默重置）
    * `被跳过的键` = 基准里有、但因为在 `NEVER_RESTORE` 里而没动的

    不落盘、不读当前文件 —— 当前值由调用方交进来。这样它可以被单测
    直接喂，也不会在"读一个计划"的时候顺手改东西。
    """
    cur = dict(current or {})
    base = baseline(path)
    keep = {k: v for k, v in base.items() if k not in NEVER_RESTORE}
    skipped = sorted(k for k in base if k in NEVER_RESTORE)

    new = dict(cur)
    changed: dict = {}
    for k, v in keep.items():
        if k not in cur or cur[k] != v:
            changed[k] = [cur.get(k), v]
            new[k] = v
    return new, changed, skipped


def _atomic_write(path: Path, data: dict) -> None:
    """同目录临时文件 + replace —— 读侧永远看不到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path, stamp: Optional[str] = None) -> Path:
    """把坏文件挪走。不覆盖既有备份（同一秒坏两次也不丢第一份）。"""
    base = stamp or time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(path.name + ".bad-" + base)
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(path.name + ".bad-" + base + "-%d" % n)
    os.replace(path, target)
    return target


def heal(path: Path, *, baseline_path: Optional[Path] = None,
         stamp: Optional[str] = None,
         overrides: Optional[dict] = None) -> dict:
    """确保设置文件可用：缺文件就用基准播种；坏了就先备份再用基准重建；好的不动。

    这是 TUI 首次启动那一步。GUI 侧的口径仍是"不播种"：
    不存在的文件照旧走各模块的 inline 兜底。
    两边不冲突，一份完整的基准快照与"缺键 = 出厂默认"读到的是同一组值。

    `overrides` 只在这一次播种时改写基准里的那几格 —— TUI 用它把
    `background_services` 关掉（TUI 首启不跑通道与定时任务）。
    已有文件一个字节也不动：那里面是用户的决定，不是我们的默认值。

    返回 `{"action": "ok"|"seeded"|"backup"|"error", "backup": <路径或空>,
    "error": <原因或空>}`。写不进去不抛：设置文件处置失败不该挡住启动。
    """
    p = Path(path)
    backup = ""
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {"action": "ok", "backup": "", "error": ""}
        except (OSError, ValueError):
            pass
        try:
            backup = str(_backup(p, stamp))
        except OSError as exc:
            return {"action": "error", "backup": "",
                    "error": "坏文件备份失败：%s" % exc}
    base = baseline(baseline_path)
    if not base:
        return {"action": "error", "backup": backup, "error": "出厂基准读不到"}
    if overrides:
        base = {**base, **{k: v for k, v in overrides.items()
                           if not str(k).startswith("_")}}
    try:
        _atomic_write(p, base)
    except OSError as exc:
        return {"action": "error", "backup": backup,
                "error": "写入设置失败：%s" % exc}
    return {"action": "backup" if backup else "seeded",
            "backup": backup, "error": ""}
