"""技能声明的「需要哪些配置」—— 声明、是否配好、去哪里配。

## 为什么单独一层

技能可以声明自己需要某个配置（`manifest.yaml` 的 `requires_config`）。
「配没配」的判据只有这里一份，三个读者共用：

  · `crannia doctor` —— 缺了报一条 WARN（不挡启动）；
  · `/config` —— 列出/查看配置入口（工具失败时回执指向它）；
  · 写配置的那条路 —— 第一次把凭据写进配置文件时提醒一次。

## 两种配置的家

| 形状 | 例子 | 判「配好了」 |
|---|---|---|
| 声明式（`settings.storage` + schema） | `media_gen` | 那个文件存在且不是空对象 |
| 旧特例（`settings.json` 的 `skill_configs.<name>`） | `mail_parser` / `github` | 那一格是非空对象 |

判据刻意宽松：只看"有没有东西"，不逐字段校验 `required`
—— 一个填了一半的配置与一个空配置，在"能不能用"这件事上常常一样，
而逐字段校验会把"可选字段"逼成必填。真正的失败仍由工具自己报。

## 这不是安全边界

`requires_config` 只是"说清缺什么、去哪配"的声明；不拦工具、不拦装载。
与 `requires`（框架能力）是两根轴：那个缺了不装载，这个缺了照样装。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "REQUIRES_KEY", "all_skills", "configured", "declared", "forget_session",
    "is_secret_target", "missing_for", "note_fill_once", "warnings", "where",
]

#: manifest 里的字段名。
REQUIRES_KEY = "requires_config"

_SETTINGS_PATH = Path("data") / "settings.json"

#: 本会话已经提醒过"凭据会进历史"的会话号。会话级状态，`/clear` 清。
_fill_noted: set[str] = set()


def _skills_dir(skills_dir: Path | str | None = None) -> Path:
    if skills_dir is not None:
        return Path(skills_dir)
    from core import paths as _paths
    return Path(_paths.asset_dir("skills"))


def _manifest_at(root: Path, name: str) -> dict:
    try:
        import yaml
        data = yaml.safe_load((root / name / "manifest.yaml").read_text(encoding="utf-8"))
    except Exception:                                  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def all_skills(skills_dir: Path | str | None = None) -> list[str]:
    """有 manifest 的技能目录名（排序确定）。"""
    root = _skills_dir(skills_dir)
    try:
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and (p / "manifest.yaml").is_file())
    except OSError:
        return []


def declared(skill_name: str, *, skills_dir: Path | str | None = None) -> tuple[str, ...]:
    """这个技能声明需要哪些配置（去重、保序）。"""
    raw = _manifest_at(_skills_dir(skills_dir), str(skill_name)).get(REQUIRES_KEY)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


def _settings(path: Path | str | None = None) -> dict:
    p = Path(path) if path is not None else _SETTINGS_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def configured(scope: str, *, settings_path: Path | str | None = None) -> bool:
    """这个配置项配好了没有。两种家分开判（见模块 docstring）。"""
    name = str(scope or "").strip()
    if not name:
        return False
    from core import skill_settings as _ss
    if _ss.available(name):
        return bool(_ss.load(name))
    saved = _settings(settings_path).get("skill_configs")
    item = saved.get(name) if isinstance(saved, dict) else None
    return isinstance(item, dict) and bool(item)


def where(scope: str) -> str:
    """给人看的配置入口：声明式给文件路径，旧特例给 settings.json 里的键。"""
    name = str(scope or "").strip()
    from core import skill_settings as _ss
    if _ss.available(name):
        path = _ss.storage_for(name)
        return str(path) if path else ""
    return "data/settings.json · skill_configs.%s" % name


def missing_for(skill_name: str, *,
                skills_dir: Path | str | None = None,
                settings_path: Path | str | None = None) -> list[str]:
    """这个技能声明了、但还没配的配置项。"""
    return [c for c in declared(skill_name, skills_dir=skills_dir)
            if not configured(c, settings_path=settings_path)]


def warnings(*, skills_dir: Path | str | None = None,
             settings_path: Path | str | None = None) -> list[dict]:
    """医生那一行要的数据：`[{skill, scope, where}]`，排序确定。"""
    out: list[dict] = []
    for name in all_skills(skills_dir):
        for scope in missing_for(name, skills_dir=skills_dir,
                                 settings_path=settings_path):
            out.append({"skill": name, "scope": scope, "where": where(scope)})
    return out


# -- 写配置时的"一次性提醒" ------------------------------------------------

def note_fill_once(session_id: str) -> bool:
    """本会话第一次把凭据写进配置 —— 返回 True，该提醒一次。

    没有会话号就返回 False：不为一个无主的调用造一条状态。
    """
    sid = str(session_id or "").strip()
    if not sid or sid in _fill_noted:
        return False
    _fill_noted.add(sid)
    return True


def forget_session(session_id: str) -> None:
    """`/clear` 的清点入口（登记在 `core/state_registry.py`）。"""
    _fill_noted.discard(str(session_id or "").strip())


def _resolve(path: Path | str) -> Path | None:
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _secret_fields(schema: dict) -> bool:
    """schema 里有没有 `x-secret: true` 的字段（递归）。"""
    if not isinstance(schema, dict):
        return False
    if schema.get("x-secret") is True:
        return True
    for key in ("properties",):
        node = schema.get(key)
        if isinstance(node, dict) and any(_secret_fields(v) for v in node.values()):
            return True
    addl = schema.get("additionalProperties")
    return _secret_fields(addl) if isinstance(addl, dict) else False


def is_secret_target(path: Path | str, *,
                     skills_dir: Path | str | None = None,
                     settings_path: Path | str | None = None) -> bool:
    """这次写的是不是"装凭据的那份文件"。

    两处：`settings.json`（`skill_configs` / `web_token` 都在里面），以及
    声明了 `x-secret` 字段的技能的存储文件。判的是目标，不猜写了什么 ——
    "内容里有没有像密钥的串"那种判据会误报，而误报要付的是一次多余提醒。
    """
    target = _resolve(path)
    if target is None:
        return False
    if target == _resolve(Path(settings_path) if settings_path is not None
                          else _SETTINGS_PATH):
        return True
    from core import skill_settings as _ss
    for name in all_skills(skills_dir):
        storage = _ss.storage_for(name)
        if storage is None:
            continue
        if not _secret_fields(_ss.schema_for(name)):
            continue
        if _resolve(storage) == target:
            return True
    return False
