"""邮箱配置（`skill_configs.mail_parser`）的唯一读取口。

## 它为什么存在

设置页（`web/server.py`）写这份配置 —— 而且写之前会先真发一封测试邮件
UI 上那句「已配置」是验证过的，不是摆设。

可是读它的人如果各读各的，就会出现：`deliver_file`（交付到邮箱那条路）读了，
而 `send_email`（`skills/mail_parser/handler.py`）一个字都不读 ——
它的 manifest 里 `smtp_server` / `email_address` / `password` 全是 `required`
模型只能回头向用户讨授权码。

用户看到的结果正是它的后果：设置页说「已配置」，而模型还追着要授权码。

修法不是往 `send_email` 里再抄一份（那就是三个副本 = 同一个病），
而是抽出一份权威读取口，两个消费者都收到它下面。

## 三个口径，各一处

| 口径 | 在哪 |
|---|---|
| 写 | `web/server.py`（设置页，写前先发测试邮件） |
| 读 | 本模块 |
| 用 | `deliver_file`（附件交付）与 `send_email`（模型发信） |

别再在任何 `skills/` 里内联 `settings.get("skill_configs")`** ——
`tests/test_mail_config.py` 有一条按 AST 反查的读者钉着这一条。

## 路径是相对的（不是"忘了写绝对路径"）

同 `core/tiers.py::_SETTINGS_PATH` 那条规矩：相对路径让 `tests/_sandbox.py::sandbox()`
（chdir）把它解析到临时目录里，测试不会读到开发机的真配置，
也就不会真的连上 SMTP 发信。改成绝对路径会当场破掉这条。

## 读不到怎么办：返回空，不抛

调用方要能区分「没配」与「配了但坏了」—— 但那两件事在这里都归为"取不到"，
由调用方决定说什么（`send_email` 会回一句"请先到设置页配"）。
"读不到"不许塌成"有一个值"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "SCOPE",
    "load_config", "active_account", "smtp_params", "resolve",
]

#: 相对路径（见模块 docstring 最后一段）。别改成绝对路径。
#: 名字带下划线是有意的 —— `tests/_sandbox.py::neutralise_settings()`
#:    按 `_SETTINGS_PATH` 这个属性名统一把输入源指走
#:    （同 `core/tiers` / `core/modes` / `core/retention` / `core/ice.guard`）。
#:    改名成公开的 `SETTINGS_PATH` 会让它悄悄不再被指走
#:用例又开始读开发机的真配置（里面有授权码）。
_SETTINGS_PATH = Path("data/settings.json")

#: 配置在 `skill_configs` 里的那个键 —— 与技能目录同名。
SCOPE = "mail_parser"

#: 取不到 `_provider` 时的兜底（与 `deliver_file` 的行为一致，逐字不动）。
_DEFAULT_PROVIDER = "qq"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """`skill_configs.mail_parser` 那一格。读不到 / 坏了，返回 `{}`（不抛）。"""
    p = path or _SETTINGS_PATH
    try:
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[mail] 读不到邮箱配置（{type(exc).__name__}）—— 本次视为未配置")
        return {}
    cfg = (raw.get("skill_configs") or {}).get(SCOPE) or {}
    return cfg if isinstance(cfg, dict) else {}


def active_account(path: Optional[Path] = None) -> Dict[str, Any]:
    """当前生效的那个账号。

    两种格式都认（与 `deliver_file` 的行为一致，逐字不动）：
    新的多账号 `{"active": "<id>", "accounts": {...}}`，与旧的扁平格式。

    `active` 指到了一个不存在的 id，退而取第一个账号
    （与 `deliver_file` 的行为一致：那种状态下"有配置"比"什么都不用"好）。
    """
    cfg = load_config(path)
    if not cfg:
        return {}
    if "accounts" in cfg:
        accounts = cfg.get("accounts") or {}
        if not isinstance(accounts, dict) or not accounts:
            return {}
        acc = accounts.get(cfg.get("active") or "")
        if not isinstance(acc, dict) or not acc:
            acc = next((a for a in accounts.values() if isinstance(a, dict)), {})
        return acc or {}
    return cfg                      # 旧扁平格式


def smtp_params(account: Dict[str, Any]) -> Tuple[str, int]:
    """`(smtp_server, smtp_port)` —— 账号里没写就走 manifest 的 provider 预设。

    预设表在 `skills/mail_parser/manifest.yaml` 的 `config.provider_presets`；
    最后一层兜底是 QQ（`smtp.qq.com` / `465`），照原实现。
    """
    preset = (account or {}).get("_provider", _DEFAULT_PROVIDER)
    defaults: Dict[str, Any] = {}
    try:
        import yaml
        from core import paths as _paths
        mf = _paths.asset_dir("skills") / "mail_parser" / "manifest.yaml"
        if mf.exists():
            m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            defaults = ((m.get("config") or {}).get("provider_presets")
                        or {}).get(preset, {}).get("defaults", {}) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[mail] 读 provider 预设失败（{type(exc).__name__}）—— 落回兜底")
    server = (account or {}).get("smtp_server", "") or defaults.get(
        "smtp_server", "smtp.qq.com")
    try:
        port = int((account or {}).get("smtp_port", 0)
                   or defaults.get("smtp_port", 465))
    except (TypeError, ValueError):
        port = 465
    return str(server), port


def resolve(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """配好的发信参数；没配 / 缺件，`None`。

    返回 `{email_address, password, smtp_server, smtp_port}`。

    `password` 是"缺件"的判据之一 —— 光有邮箱地址发不出去，
    而那种状态下回一句"请去设置页配"比回一句 SMTP 认证失败有用得多。
    """
    acc = active_account(path)
    if not acc:
        return None
    email = str(acc.get("email_address") or "").strip()
    pwd = str(acc.get("password") or "")
    if not email or not pwd:
        return None
    server, port = smtp_params(acc)
    return {"email_address": email, "password": pwd,
            "smtp_server": server, "smtp_port": port}
