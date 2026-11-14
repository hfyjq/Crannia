"""技能的声明式配置。

## 它解决什么

在此之前，一个 skill 的配置是框架里按技能名写死的特例：
`web/server.py` 里 `mail_parser` 一段、`github` 一段，凭据还要各自打码、
各自挑存放位置。每加一个带配置的 skill = 动本体 + 重新 build + 发一次版，
而那正是"升级就把插件弄坏"的机理（插件本身没坏，是界面和插件版本绑死）。

现在：skill 在 `manifest.yaml` 里声明 `settings`（一份 JSON Schema 子集）
与 `storage`（那个文件在哪），框架只知道怎么校验、怎么打码、往哪写，
不知道"生图"或"邮箱"是什么。

## 约定（判据，不许多一套）

    settings:
      storage: data/media_gen_credentials.json    # 相对仓库根；不进 settings.json
      type: object
      properties:
        default: {type: string}
        entries:
          type: object
          additionalProperties:            # 本条目的字段表
            type: object
            properties:
              api_key: {type: string, x-secret: true}
      required: [entries]

  * `x-secret: true` 的字段：`public()` 里一律换成 `****`；`save()` 时把
    `****` 换回旧值（界面不回显凭据，原样送回来不等于"清空"）。
  * 整份配置住在 `storage` 那个文件里 —— 所以凭据天然不进
    `settings.json`（导出包也不会带走它）。
  * 没声明 `settings` 的技能走原来的路（`server.py` 的旧特例），一个字节不变。

这是能力约束，不是安全边界：`x-secret` 只是"界面别回显"的约定，
同进程的 handler 照样读得到文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["available", "schema_for", "storage_for", "load", "public", "save",
           "MASK"]

MASK = "****"

_SCALARS = (str, int, float, bool)


def _manifest(skill_name: str) -> dict:
    """读 `skills/<name>/manifest.yaml`；读不到，空表（不抛）。

    目录只按安装根解析（`core.paths.asset_dir("skills")`）——
      打包安装时 skills 不在 cwd 下；不许留一个 `Path("skills")` 的兜底，
      那正是 `test_paths` 钉着的"按 cwd 取代码资产"（会随启动目录变）。
    """
    try:
        from core import paths as _paths
        base = Path(_paths.asset_dir("skills"))
    except Exception:                                     # noqa: BLE001
        return {}
    p = base / str(skill_name) / "manifest.yaml"
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def schema_for(skill_name: str) -> dict:
    m = _manifest(skill_name)
    s = m.get("settings")
    return s if isinstance(s, dict) else {}


def storage_for(skill_name: str) -> Path | None:
    m = _manifest(skill_name)
    s = m.get("settings")
    if not isinstance(s, dict):
        return None
    rel = str(s.get("storage") or "").strip()
    return Path(rel) if rel else None


def available(skill_name: str) -> bool:
    """这个技能能不能走声明式那条路（有 schema + 有 storage）。"""
    return bool(schema_for(skill_name)) and storage_for(skill_name) is not None


# 读 / 写

def load(skill_name: str) -> dict:
    """原样读配置文件（含明文凭据 —— 只给框架自己和 handler 用）。"""
    p = storage_for(skill_name)
    if p is None or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        print("[skill-settings] %s 读不出来：%s: %s" % (p, type(exc).__name__, exc))
        return {}
    return data if isinstance(data, dict) else {}


def _secret_paths(schema: dict) -> set[tuple[str, ...]]:
    """schema 里声明成 `x-secret` 的字段路径（相对一个条目）。"""
    out: set[tuple[str, ...]] = set()

    def walk(node, path):
        if not isinstance(node, dict):
            return
        if node.get("x-secret") is True and path:
            out.add(path)
        props = node.get("properties")
        if isinstance(props, dict):
            for k, v in props.items():
                walk(v, path + (str(k),))
        addl = node.get("additionalProperties")
        if isinstance(addl, dict):
            walk(addl, path + ("*",))

    walk(schema, ())
    return out


def _entry_schema(schema: dict, key: str) -> dict:
    props = schema.get("properties") or {}
    node = props.get(key) if isinstance(props, dict) else None
    if not isinstance(node, dict):
        return {}
    addl = node.get("additionalProperties")
    return addl if isinstance(addl, dict) else {}


def _mask_map(skill_name: str, cfg: dict) -> dict:
    """把 `entries` / `accounts` 里声明为 secret 的字段打码（不原地改）。"""
    schema = schema_for(skill_name)
    out = json.loads(json.dumps(cfg, ensure_ascii=False)) if cfg else {}
    for key in ("entries", "accounts"):
        table = out.get(key)
        if not isinstance(table, dict):
            continue
        es = _entry_schema(schema, key)
        secrets = {p[-1] for p in _secret_paths(es) if len(p) == 1}
        for entry in table.values():
            if not isinstance(entry, dict):
                continue
            for f in secrets:
                if entry.get(f):
                    entry[f] = MASK
    return out


def public(skill_name: str) -> dict:
    """给界面看的那一份（凭据打码）。"""
    return _mask_map(skill_name, load(skill_name))


def _keep_masked(skill_name: str, new_cfg: dict, old_cfg: dict) -> dict:
    """把新配置里的 `****` 换回旧值（界面不回显凭据，原样送回不是清空）。"""
    schema = schema_for(skill_name)
    out = json.loads(json.dumps(new_cfg, ensure_ascii=False)) if new_cfg else {}
    for key in ("entries", "accounts"):
        new_map, old_map = out.get(key), old_cfg.get(key)
        if not isinstance(new_map, dict) or not isinstance(old_map, dict):
            continue
        es = _entry_schema(schema, key)
        secrets = {p[-1] for p in _secret_paths(es) if len(p) == 1}
        for entry_name, entry in new_map.items():
            old_entry = old_map.get(entry_name)
            if not isinstance(entry, dict) or not isinstance(old_entry, dict):
                continue
            for f in secrets:
                if entry.get(f) == MASK and old_entry.get(f) not in (None, "", MASK):
                    entry[f] = old_entry[f]
    return out


def _check_type(value: Any, want: str) -> bool:
    if want == "string":
        return isinstance(value, str)
    if want == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if want == "boolean":
        return isinstance(value, bool)
    if want == "object":
        return isinstance(value, dict)
    if want == "array":
        return isinstance(value, (list, tuple))
    return True


def validate(skill_name: str, cfg: Any) -> str:
    """按 schema 校验；能存，空串，不能，一句给人看的原因。

    只校验 schema 里写了的那几层（够用即可）：类型、`required`、
      条目字段的类型。没有 schema 的技能不走这里。
    """
    schema = schema_for(skill_name)
    if not schema:
        return ""
    if not isinstance(cfg, dict):
        return "配置必须是一个对象"
    props = schema.get("properties")
    if not isinstance(props, dict):
        return ""
    for req in schema.get("required") or []:
        if req not in cfg:
            return "缺少必填项：%s" % req
    for key, value in cfg.items():
        node = props.get(key)
        if not isinstance(node, dict):
            continue
        want = str(node.get("type") or "")
        if want and not _check_type(value, want):
            return "字段「%s」类型不对（要 %s）" % (key, want)
        addl = node.get("additionalProperties")
        if want == "object" and isinstance(addl, dict) and isinstance(value, dict):
            sub_props = addl.get("properties") or {}
            if not isinstance(sub_props, dict):
                continue
            for name, entry in value.items():
                if not isinstance(entry, dict):
                    return "条目「%s」必须是一个对象" % name
                for f, fnode in sub_props.items():
                    if f not in entry or not isinstance(fnode, dict):
                        continue
                    fwant = str(fnode.get("type") or "")
                    if fwant and not _check_type(entry[f], fwant):
                        return "条目「%s」的「%s」类型不对（要 %s）" % (name, f, fwant)
    return ""


def save(skill_name: str, cfg: dict) -> str:
    """校验 + 把 `****` 换回旧值 + 写盘。返回错误串（成功，空串）。"""
    p = storage_for(skill_name)
    if p is None:
        return "这个技能没有声明配置存放位置"
    bad = validate(skill_name, cfg)
    if bad:
        return bad
    merged = _keep_masked(skill_name, cfg, load(skill_name))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:                                # noqa: BLE001
        return "写配置文件失败：%s" % exc
    return ""
