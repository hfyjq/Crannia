# -*- coding: utf-8 -*-
"""`skill_config_write` —— 给模型一个受控的"写技能配置"入口（P3）。

## 为什么需要它

`ssh` 这类技能的配置里既有普通字段（默认用户名），也有秘密
（口令 / 密码 / sudo 密码）。用户把私钥路径或密码打在对话里时，
模型能帮填 —— 但用户不该被迫手改 `data/settings.json`。
这条入口把"帮填"变成一次有审批的动作。

## 四条硬规矩

1. 审批：它是 `destructive=True` 且没有自己的审批发射点 ——
   兜底闸在派发前问一次（subject = 工具名）。
2. 秘密只写不回显：口令 / 密码类字段写进去，回执与审计只出现字段名；
   读回接口里它们仍然是 `x-secret`（设置页那一层负责打码）。
3. 空值不覆盖：给空串等于"不改这一格" —— 静默把口令清掉是最坏的一种
   "帮了倒忙"。
4. 字段白名单：每个技能只认它 manifest 里声明过的键。写一个不存在的键
   就是一次拼错，拒掉并列出可写的键。

## 它不做什么

不改模型/安全档位（那些只能由用户在设置页改），也不删条目。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ["TOOL_NAME", "SCHEMA", "skill_config_write", "register"]

TOOL_NAME = "skill_config_write"

_SETTINGS = Path("data/settings.json")

#: 技能 -> 可写字段。秘密字段单独一张表（回执只记名字）。
_FIELDS: dict[str, tuple[str, ...]] = {
    "ssh": ("key_path", "passphrase", "user", "password", "sudo_password"),
}
_SECRET_FIELDS: frozenset[str] = frozenset({"passphrase", "password", "sudo_password"})

SCHEMA = {
    "description": (
        "写入一个技能的配置条目（目前只支持 ssh）。秘密字段（口令/密码）"
        "只写不回显，回执里只出现字段名；空值表示不修改该字段。"
        "这是改动持久安全配置的动作，会先经过一次审批。"
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "skill": {"type": "string", "enum": sorted(_FIELDS)},
            "entry": {"type": "string",
                      "description": "条目名（设置页里那一行；新建会直接用这个名字）"},
            "fields": {"type": "object",
                       "description": "要写的字段（键 -> 值）；空串表示不修改"},
            "active": {"type": "boolean", "default": False,
                       "description": "是否把这个条目设为当前使用的那一条"},
        },
        "required": ["skill", "entry", "fields"],
    },
}


def _err(msg: str) -> dict:
    return {"success": False, "tool": TOOL_NAME, "data": {}, "error": msg}


def _load_settings() -> dict:
    try:
        raw = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_settings(data: dict) -> None:
    _SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = _SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _SETTINGS)


def skill_config_write(skill: str, entry: str, fields: dict,
                       active: bool = False) -> dict:
    sk = str(skill or "").strip()
    name = str(entry or "").strip()
    if sk not in _FIELDS:
        return _err("这个技能没有配置写入口：%s（可写：%s）"
                    % (sk or "(空)", " / ".join(sorted(_FIELDS))))
    if not name:
        return _err("entry 不能为空（它是设置页里那一行的名字）。")
    if not isinstance(fields, dict) or not fields:
        return _err("fields 必须是非空对象（键 -> 值）。")

    allowed = _FIELDS[sk]
    bad = sorted(k for k in fields if k not in allowed)
    if bad:
        return _err("不认识的字段：%s（这个技能可写：%s）"
                    % ("、".join(bad), "、".join(allowed)))

    data = _load_settings()
    block = data.get("skill_configs")
    if not isinstance(block, dict):
        block = {}
        data["skill_configs"] = block
    section = block.get(sk)
    if not isinstance(section, dict):
        section = {}
        block[sk] = section
    entries = section.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        section["entries"] = entries
    row = entries.get(name)
    if not isinstance(row, dict):
        row = {}
        entries[name] = row

    written: list[str] = []
    skipped: list[str] = []
    for k in allowed:
        if k not in fields:
            continue
        v = fields.get(k)
        s = str(v if v is not None else "").strip()
        if not s:
            # 空值不覆盖：静默清掉口令是最坏的一种"帮了倒忙"。
            skipped.append(k)
            continue
        row[k] = s
        written.append(k)

    if not written and not skipped:
        return _err("没有可写的字段。")
    if not written:
        return _err("所有字段都是空值（空值表示不修改）；什么都没写。")

    if active:
        section["active"] = name
    _save_settings(data)

    return {
        "success": True,
        "tool": TOOL_NAME,
        "data": {
            "skill": sk,
            "entry": name,
            "written": written,
            #: 秘密只记名字，不记值。
            "secret_fields": [k for k in written if k in _SECRET_FIELDS],
            "unchanged_empty": skipped,
            "active": bool(active),
        },
        "error": "",
    }


def register() -> None:
    from bus.registry import register_tool
    register_tool(TOOL_NAME, skill_config_write, SCHEMA)
