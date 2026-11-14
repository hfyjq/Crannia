"""设置包的导出 / 导入。

## 包里有什么

| 段 | 默认带 | 内容 |
|---|---|---|
| `settings` | ✅ | `data/settings.json` 里可搬运的偏好（= 出厂基准 ∪ 当前，去掉本地项与派生值） |
| `allowlist` | ✅ | `data/allowlist.json` 的四份名单 |
| `model_configs` | ✅ | 端配置账本（配置名 / 模型名 / 地址）—— 不含 api |
| `gui` | ✅（前端填） | 四个外观键（`lv.colors` / `lv.colors.dark` / `lv.theme` / `lv.sideW`）。后端只搬运、不解释 |
| `enc` | 勾选才有 | 凭据（`.env` / 账本 api / `skill_configs` / `web_token` / 各家的令牌文件），AES-GCM + scrypt |

## 三条不许回退的判据

1. 同一份 `NEVER_RESTORE`（`core/settings_baseline.py`）：
   `web_token` · `skill_configs` · `workspace` · `work_groups` · `watchers` ·
   `forbidden_directories` 永远不进 `settings` 段 —— 前两个只进加密段，
   后四个根本不进包（别人的路径，导过去只会错）。导入一侧同样不拿空值覆盖它们。
2. 两段式：`dry_run` 只报"将要改变哪几格 / 跳过哪几格"，一个字节都不写；
   与「恢复默认」（`settings_baseline.plan_restore`）同形。
3. 派生值不进包（`DERIVED_KEYS`）：那些是 `GET /api/settings` 当场算出来的，
   导出去再导回来 = 第二套真相源。

## 机器相关项（导出带上、导入逐项勾选，默认不选）

`MACHINE_KEYS` 那五格。理由：它们说的是这台机器（注册表自启 / 代理 / 上次检查时间），
换一台机器照搬 = 替用户做决定，而其中 `boot_on_startup` 还会改注册表。
导入时勾了 `boot_on_startup` 必须走 `web/server.py::_apply_settings_side_effects`
（那条路会落到注册表）—— 不许只写 `settings.json`，否则设置与注册表分叉。

## 口令丢了没有后悔药

`enc` 段用口令派生密钥（scrypt），没有找回通道。界面上必须把这句话说出来；
包里的 `credential_names` 是明文清单（只列名字，不含值），好让人先看见包里有什么。
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

#: 包的 schema。读到更高的 = 拒（不猜）。
SCHEMA = 1

#: 包的身份证（格式判据；不是密码学材料）。
KIND = "crannia-settings"

#: 加密时绑进 AAD 的串 —— 换包格式时顺手换它，解出来的东西自然对不上。
AAD = b"crannia-settings-v1"

#: 机器相关项：导出带上，导入逐项勾选（默认不选）。
MACHINE_KEYS: tuple = ("boot_on_startup", "proxy_mode", "proxy_url",
                       "update_last_check", "update_ignored_version")

#: `GET /api/settings` 当场算出来的派生值 —— 不是设置，谁也不许进包。
DERIVED_KEYS: frozenset = frozenset({
    "data_dir", "allowlist_egress_all", "allowlist_command_all",
    "ice_effective", "ice_canary", "proxy_state", "proxy_modes",
    "interface_languages", "response_languages", "response_language_effective",
    "effort_levels", "effort_error", "modes", "mode", "mode_error",
    "framework_presets", "framework_preset_custom", "leaf_md",
    "subagent_model_options", "subagent_model_legacy",
    "file_history_effective_days", "file_history_effective_max_mb",
})

#: 凭据文件（原样搬运，不解析 —— 每一份的格式都不归我们管）。
#: `(路径, 包里的键)`。路径是相对 cwd 的，与它们各自的写入口一致。
CRED_FILES: tuple = (
    (".env", "env"),
    ("data/api_keys.json", "api_keys"),
    ("data/wechat_credentials.json", "wechat_credentials"),
    ("data/media_gen_credentials.json", "media_gen_credentials"),
    ("data/media_gen_key.txt", "media_gen_key"),
    ("data/runtime/provider_credentials.json", "provider_credentials"),
    # MCP 服务器的 `env` / `headers` 里通常就是令牌，归凭据，绝不进默认包。
    ("data/mcp_servers.json", "mcp_servers"),
)

#: 只进加密段的两个设置键（见 `NEVER_RESTORE`）。
CRED_SETTINGS: tuple = ("skill_configs", "web_token")

#: 默认包里永不出现的四格（别人的路径，导过去只会错）。
LOCAL_ONLY: frozenset = frozenset({
    "workspace", "work_groups", "watchers", "forbidden_directories",
})


# 小工具

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text) -> bytes:
    return base64.b64decode(str(text or "").encode("ascii"))


def _as_dict(value) -> dict:  # noqa: ANN001
    """`dict` 或空 `dict` —— 外来数据只认这一种形状（别的都当"没有"）。"""
    return value if isinstance(value, dict) else {}


def _read_text(path: str) -> str:
    try:
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    return ""


# 导出：挑出可以搬运的那部分设置

def exportable_settings(current: Optional[dict] = None) -> dict:
    """`settings.json` 里可以搬走的部分 = 基准 ∪ 当前（当前赢）。

    为什么补基准：`settings.json` 只装用户改过的键（新装甚至没有这个文件）
只导出它，包里就是一本"半截设置"，导到新机器上等于什么都没设。
    补基准不是播种（不落盘）：这是"导出这一份快照"时的现算
      —— 与"不播种"的取舍不冲突。
    `NEVER_RESTORE` / `LOCAL_ONLY` / `DERIVED_KEYS` 一律剔掉；`_comment` 同理。
    """
    from core.settings_baseline import baseline, NEVER_RESTORE

    def _skip(k: str) -> bool:
        return (not isinstance(k, str) or k.startswith("_")
                or k in NEVER_RESTORE or k in LOCAL_ONLY or k in DERIVED_KEYS)

    out: dict = {}
    for k, v in baseline().items():
        if not _skip(k):
            out[k] = v
    for k, v in (current or {}).items():
        if not _skip(k):
            out[k] = v
    return out


def collect_credentials(settings: Optional[dict] = None) -> dict:
    """凭据段的明文（只在加密前存在于内存里）。

    只收存在的那几份（不塞空串 —— "没有这个文件"与"这个文件是空的"
      在导入侧要分开处理：前者不写，后者写了就等于清空本机的凭据）。
    """
    out: dict = {}
    for path, name in CRED_FILES:
        text = _read_text(path)
        if text:
            out[name] = text
    for k in CRED_SETTINGS:
        if isinstance(settings, dict) and k in settings:
            out[k] = settings[k]
    return out


# 加密（scrypt 派生 + AES-GCM）

#: scrypt 参数。 `n=214` 是交互式口令**的常用档（约 16 MiB / 一次）；
#: 参数写进包里，将来调大也解得开旧包。
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def encrypt(payload: dict, password: str) -> dict:
    """把凭据字典封成一个 blob。口令为空，`ValueError`（不许默默不加密）。"""
    if not str(password or ""):
        raise ValueError("要带凭据就必须给口令")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = Scrypt(salt=salt, length=32, **_SCRYPT).derive(
        str(password).encode("utf-8"))
    blob = AESGCM(key).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False).encode("utf-8"), AAD)
    return {"alg": "scrypt-aesgcm", "nonce": _b64(nonce),
            "salt": _b64(salt), "ct": _b64(blob), **_SCRYPT}


def decrypt(enc: dict, password: str) -> dict:
    """解回来。口令不对 / 被改过，`ValueError`（调用方要原样说给用户听）。"""
    if not isinstance(enc, dict) or enc.get("alg") != "scrypt-aesgcm":
        raise ValueError("不认识的加密格式")
    if not str(password or ""):
        raise ValueError("这个包带凭据，需要口令")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    try:
        key = Scrypt(salt=_unb64(enc.get("salt")), length=32,
                     n=int(enc.get("n") or _SCRYPT["n"]),
                     r=int(enc.get("r") or _SCRYPT["r"]),
                     p=int(enc.get("p") or _SCRYPT["p"])).derive(
            str(password).encode("utf-8"))
        plain = AESGCM(key).decrypt(_unb64(enc.get("nonce")),
                                    _unb64(enc.get("ct")), AAD)
        raw = json.loads(plain.decode("utf-8"))
    except Exception as exc:                                # noqa: BLE001
        raise ValueError("口令错误，或者包体被篡改（%s）"
                         % type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise ValueError("凭据段的形状不对（不是对象）")
    return raw


# 导出

def build_bundle(current: Optional[dict] = None, *, include_credentials: bool = False,
                 password: str = "", gui: Optional[dict] = None,
                 app_version: str = "") -> dict:
    """造一个包（不写盘，由端点直接回给浏览器）。

    `gui` 是前端塞进来的四个外观键 —— 后端看不懂也不校验它们，
      只负责原样搬回去（它们在 `crannia-app://` 那个 origin，后端根本读不到）。
    """
    from core import allowlist as _AL
    from core import model_configs as _MC

    cur = dict(current or {})
    ledger = _MC.all_entries()
    bundle = {
        "kind": KIND,
        "schema": SCHEMA,
        "app_version": str(app_version or ""),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "settings": exportable_settings(cur),
        "allowlist": {k: list(_AL.list_for(k)) for k in _AL.KINDS},
        "model_configs": {
            "configs": ledger.get("configs") or {},
            "default": ledger.get("default") or "",
        },
        "gui": dict(gui or {}),
        "credentials": False,
    }
    if include_credentials:
        payload = collect_credentials(cur)
        bundle["enc"] = encrypt(payload, password)
        bundle["credentials"] = True
        #: 明文清单（只有名字）—— 让人在导入前先看见包里有什么。
        bundle["credential_names"] = sorted(payload)
    return bundle


def summarize(bundle: dict) -> dict:
    """给界面看的"这个包里有什么"（不解密）。"""
    b = _as_dict(bundle)
    settings = _as_dict(b.get("settings"))
    allow = _as_dict(b.get("allowlist"))
    mc = _as_dict(b.get("model_configs"))
    mc_configs = _as_dict(mc.get("configs"))
    return {
        "kind": b.get("kind"),
        "schema": b.get("schema"),
        "app_version": b.get("app_version") or "",
        "exported_at": b.get("exported_at") or "",
        "settings_keys": len(settings),
        "allowlist_items": sum(len(v) for v in allow.values() if isinstance(v, list)),
        "model_configs": len(mc_configs),
        "gui_keys": len(b.get("gui") or {}),
        "credentials": bool(b.get("credentials")),
        "credential_names": list(b.get("credential_names") or []),
    }


# 导入：计划（纯函数）

def _bad_shape(bundle) -> str:
    if not isinstance(bundle, dict):
        return "包不是一个对象"
    if bundle.get("kind") != KIND:
        return "错误包体（kind=%r）" % (bundle.get("kind"),)
    try:
        schema = int(bundle.get("schema") or 0)
    except (TypeError, ValueError):
        return "包的 schema 读不出来（%r）" % (bundle.get("schema"),)
    if schema > SCHEMA:
        return "包体版本更新，需要优先更新程序"
    return ""


def _settings_in(bundle: dict) -> dict:
    """包里 `settings` 段里真正可以写回的键（防御性过滤一遍）。"""
    from core.settings_baseline import NEVER_RESTORE

    raw = bundle.get("settings")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and not k.startswith("_")
            and k not in NEVER_RESTORE and k not in LOCAL_ONLY
            and k not in DERIVED_KEYS}


def plan_import(bundle, current: Optional[dict] = None, *,
                password: str = "", selections: Iterable[str] = (),
                with_credentials: bool = False,
                with_allowlist: bool = True,
                with_model_configs: bool = True) -> dict:
    """算出"导入之后会变成什么样"（不写盘）。

    返回的 `changed` 形状与「恢复默认」逐字一致：`{键: [现在, 包里]}` ——
    界面拿它一格格说清楚（`auto_approve` / `relax_command_tier` 也在这里面，
    所以不许静默整表替换）。
    """
    err = _bad_shape(bundle)
    if err:
        return {"ok": False, "error": err, "changed": {}, "skipped": [],
                "machine": {}, "allowlist": {}, "model_configs": {},
                "credentials": {}, "warnings": []}

    cur = dict(current or {})
    #: 比的是"现在生效什么"，不是"文件里有什么"：`settings.json` 只装
    #:   用户改过的键，而包里是"基准 ∪ 当前"，拿文件去比，会把一堆**本来就是
    #:   默认值**的格子报成"将要改变"（用户被几十行噪声吓住，真正该看的那几行被埋掉）。
    effective = exportable_settings(cur)
    incoming = _settings_in(bundle)
    warnings: list = []

    # 机器相关项：单独摆出来，默认不应用
    picked = [k for k in (selections or ()) if k in MACHINE_KEYS]
    machine = {k: [effective.get(k), incoming[k]] for k in MACHINE_KEYS
               if k in incoming and effective.get(k) != incoming[k]}
    changed: dict = {}
    unchanged = 0
    for k, v in incoming.items():
        if k in MACHINE_KEYS:
            continue
        if k not in effective or effective[k] != v:
            changed[k] = [effective.get(k), v]
        else:
            unchanged += 1
    #: 包里没被导入的键（本地项 / 派生值 / 永远不动的）
    raw_settings = bundle.get("settings") if isinstance(bundle.get("settings"), dict) else {}
    skipped = sorted(k for k in raw_settings
                     if not isinstance(k, str) or k not in incoming)

    # 白名单：按域比
    allow_changes: dict = {}
    if with_allowlist and isinstance(bundle.get("allowlist"), dict):
        try:
            from core import allowlist as _AL
            for kind in _AL.KINDS:
                now = list(_AL.list_for(kind))
                want = bundle["allowlist"].get(kind)
                if isinstance(want, list) and [str(x) for x in want] != now:
                    allow_changes[kind] = [now, [str(x) for x in want]]
        except Exception as exc:                            # noqa: BLE001
            warnings.append("白名单那一段没收下（%s）" % type(exc).__name__)

    # 端配置账本：并（不删）
    mc_report: dict = {}
    if with_model_configs and isinstance(bundle.get("model_configs"), dict):
        try:
            from core import model_configs as _MC
            have = _MC.all_entries()
            want = bundle["model_configs"].get("configs") or {}
            if isinstance(want, dict):
                new_ids = [i for i in want if i not in (have.get("configs") or {})]
                upd_ids = [i for i in want if i in (have.get("configs") or {})
                           and (have["configs"].get(i) or {}) != want.get(i)]
                mc_report = {"added": len(new_ids), "updated": len(upd_ids),
                             "kept": len(have.get("configs") or {}),
                             "default": ("keep" if have.get("default")
                                         else (bundle["model_configs"].get("default") or ""))}
        except Exception as exc:                            # noqa: BLE001
            warnings.append("端配置账本那一段没收下（%s）" % type(exc).__name__)

    # 凭据：只有勾了才解（口令不对在这里就红，不等到写盘那一刻）
    cred_report: dict = {}
    payload: dict = {}
    if bundle.get("credentials"):
        if not with_credentials:
            cred_report = {"present": True, "will_apply": False,
                           "names": list(bundle.get("credential_names") or [])}
        else:
            try:
                payload = decrypt(bundle.get("enc") or {}, password)
                cred_report = {"present": True, "will_apply": True,
                               "names": sorted(payload)}
            except ValueError as exc:
                warnings.append(str(exc))
                cred_report = {"present": True, "will_apply": False,
                               "names": list(bundle.get("credential_names") or []),
                               "error": str(exc)}

    return {
        "ok": True,
        "summary": summarize(bundle),
        "changed": changed,
        "unchanged": unchanged,
        "skipped": skipped,
        "machine": machine,
        "machine_selected": picked,
        "allowlist": allow_changes,
        "model_configs": mc_report,
        "credentials": cred_report,
        #: 四个外观键原样交回前端（后端读不到那个 origin，只能搬运）。
        "gui": dict(bundle.get("gui") or {}),
        "warnings": warnings,
    }


# 导入：真写

def apply_import(bundle, plan: dict, current: Optional[dict] = None, *,
                 password: str = "", selections: Iterable[str] = (),
                 with_credentials: bool = False,
                 with_allowlist: bool = True,
                 with_model_configs: bool = True) -> dict:
    """真写白名单 / 账本 / 凭据文件；`settings.json` 由调用方写。

    为什么设置那一半不在这里写：写完之后还有一串"推进活着的东西"
      （环境变量 / i18n 缓存 / ICE 单例 / 工作区 / 注册表）住在 `web/server.py`
      —— 那一条收尾只许有一份（`_apply_settings_side_effects`）。
    本函数不重算计划：`plan` 与 `bundle` 必须来自同一次调用
      （`plan_import(...)` 的返回值原样传进来）—— 否则两次之间包被换掉，
      用户确认的和落盘的就不是同一件事。
    """
    if not isinstance(plan, dict) or not plan.get("ok"):
        raise ValueError("计划无效，不写")

    cur = dict(current or {})
    incoming = _settings_in(bundle)
    picked = [k for k in (selections or ()) if k in MACHINE_KEYS]
    new_settings = dict(cur)
    written: list = []
    # 只写用户看见过的那几格（`plan["changed"]` + 勾了的机器项）——
    #    计划与落盘必须是同一件事；顺手把"本来就等于默认值"的键写进
    #    `settings.json` 也只会让那个文件越来越胖。
    changed = plan.get("changed") if isinstance(plan.get("changed"), dict) else {}
    for k, pair in changed.items():
        if isinstance(pair, list) and len(pair) == 2:
            new_settings[k] = pair[1]
            written.append(k)
    for k in picked:
        if k in incoming:
            new_settings[k] = incoming[k]
            written.append(k)

    applied: dict = {"settings": len(written)}

    # 白名单：整域替换（计划里已经一域一域比过了）
    if with_allowlist and isinstance(bundle.get("allowlist"), dict):
        try:
            from core import allowlist as _AL
            data = _AL.load()
            n = 0
            for kind in _AL.KINDS:
                want = bundle["allowlist"].get(kind)
                if isinstance(want, list):
                    data[kind] = [str(x) for x in want]
                    n += len(data[kind])
            _AL.save(data)
            applied["allowlist"] = n
        except Exception as exc:                            # noqa: BLE001
            applied["allowlist_error"] = "%s: %s" % (type(exc).__name__, exc)

    # 端配置账本：并（不删本机条目）
    if with_model_configs and isinstance(bundle.get("model_configs"), dict):
        try:
            from core import model_configs as _MC
            want = bundle["model_configs"].get("configs") or {}
            if isinstance(want, dict):
                applied["model_configs"] = _MC.merge_ledger(
                    want, default=bundle["model_configs"].get("default") or "")
        except Exception as exc:                            # noqa: BLE001
            applied["model_configs_error"] = "%s: %s" % (type(exc).__name__, exc)

    # 凭据：解密 → 原样写回那几份文件 / 那两格设置
    if with_credentials and bundle.get("credentials"):
        try:
            payload = decrypt(bundle.get("enc") or {}, password)
            n = 0
            for path, name in CRED_FILES:
                text = payload.get(name)
                if not isinstance(text, str) or not text:
                    continue          # 包里没有 / 是空的，不写（不拿空清本机）
                p = Path(path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                n += 1
            for k in CRED_SETTINGS:
                if k in payload:
                    new_settings[k] = payload[k]
                    written.append(k)
                    n += 1
            applied["credentials"] = n
        except ValueError as exc:
            applied["credentials_error"] = str(exc)

    applied["written"] = sorted(set(written))
    return {"settings": new_settings, "applied": applied}
