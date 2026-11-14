"""技能声明的外部程序（`manifest.yaml` 的 `external:`）。

一个技能可能要好东西才有完整能力 —— `ffmpeg`、`ghidra`、`tshark`、一个 JAR。
把这份依赖写进 manifest 之后，框架能回答一句话：

    这台机器现在能做什么，还差什么，差的东西怎么补。

三条判据：

  * 缺了不阻止装载 —— 与 `optional` 同一条：能降级就别拒装。
    一个"没有 ffmpeg 就整包不装"的技能，会把本来能用的九成能力一起拿走。
  * 每次都当场查 —— 外部程序是这台机器的属性，不是这个包的属性。
    用户刚装完 `ffmpeg`，技能页下一次刷新就该变绿；不缓存、不落盘。
  * 说得出缺什么、怎么补 —— 一句「不满足依赖」等于没说。

## 声明形状

```yaml
external:
  - ffmpeg                                   # 只在 PATH 里找
  - name: ghidra
    env: GHIDRA_INSTALL_DIR                  # 环境变量指向一个已存在的目录
    hint: 装 Ghidra 12.x 并设置 GHIDRA_INSTALL_DIR
  - name: jadx
    path: tools/jadx/bin/jadx                # 相对技能目录的固定路径
  - name: mycli
    command: /opt/mycli/bin/mycli            # 指定可执行文件
```

四种找法按 `command` > `path` > `env` > `name` 的顺序取第一个给了的。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

__all__ = ["normalise", "check", "ready", "describe"]


def normalise(specs: Any, *, root: "Path | str | None" = None) -> list[dict]:
    """把 manifest 的 `external` 归一成 `[{name, command, env, path, hint}]`。

    接受字符串或对象；认不出的条目丢掉（一个坏条目不该让整份声明消失）。
    `path` 相对技能目录时按 *root* 解析成绝对路径。
    """
    if specs is None:
        return []
    if isinstance(specs, (str, dict)):
        specs = [specs]
    if not isinstance(specs, (list, tuple)):
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for raw in specs:
        if isinstance(raw, str):
            item = {"name": raw.strip()}
        elif isinstance(raw, dict):
            item = {k: raw.get(k) for k in ("name", "command", "env", "path", "hint")}
            if isinstance(item.get("name"), str):
                item["name"] = item["name"].strip()
        else:
            continue
        name = item.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        entry = {"name": name}
        for key in ("command", "env", "path", "hint"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        if "path" in entry and root is not None:
            p = Path(entry["path"]).expanduser()
            if not p.is_absolute():
                p = Path(root) / p
            entry["path"] = str(p)
        out.append(entry)
    return out


def _resolve(entry: dict) -> str | None:
    """这一条现在能不能用；能用返回「在哪」，不能返回 None。"""
    command = entry.get("command")
    if command:
        return shutil.which(command) or (command if Path(command).is_file() else None)

    path = entry.get("path")
    if path:
        p = Path(path).expanduser()
        return str(p) if p.exists() else None

    env = entry.get("env")
    if env:
        value = os.environ.get(env, "").strip()
        if not value:
            return None
        p = Path(value).expanduser()
        return str(p) if p.exists() else None

    return shutil.which(entry["name"])


def ready(entry: dict) -> dict:
    """单条的结论：`{name, ok, path, hint}`。"""
    found = _resolve(entry)
    return {
        "name": entry["name"],
        "ok": found is not None,
        "path": found or "",
        "hint": entry.get("hint", ""),
    }


def check(specs: Any, *, root: "Path | str | None" = None) -> dict:
    """整份声明的结论：`{ok, present, missing, total}`。

    `missing` 里每条都带 `hint`（声明里给了才有）—— 界面直接念给用户听。
    没有声明时返回 `ok=True, total=0`："没要求"与"全都有"对界面是同一件事。
    """
    entries = normalise(specs, root=root)
    rows = [ready(e) for e in entries]
    missing = [{k: r[k] for k in ("name", "path", "hint")} for r in rows if not r["ok"]]
    return {
        "ok": not missing,
        "total": len(rows),
        "present": [{"name": r["name"], "path": r["path"]} for r in rows if r["ok"]],
        "missing": missing,
    }


def describe(status: dict) -> str:
    """一句给界面用的话；全都就绪时返回空串（界面上就不占位置）。"""
    missing = status.get("missing") or []
    if not missing:
        return ""
    names = "、".join(m["name"] for m in missing)
    hints = [m["hint"] for m in missing if m.get("hint")]
    if hints:
        return "缺少外部程序：%s —— %s" % (names, "；".join(hints))
    return "缺少外部程序：%s" % names
