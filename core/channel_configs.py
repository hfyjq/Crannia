"""通道的「配置条目」账本 —— 一个通道可以存几套凭据，一套标默认。

在此之前每个通道只有一份配置，住在 `config.yaml` 的 `channel.<名>` 段里，
凭据由 `${ENV}` 解析、真值落在 `.env`。于是：想留一套测试机器人、一套正式
机器人，只能来回改那一份；而邮箱与模型早就是「命名条目 + 一条默认」了。

## 形状（照模型配置条目那一套）

    data/runtime/channel_configs.json      账本，不含凭据
    data/runtime/channel_credentials.json  凭据，只此一处装明文

    {
      "version": 1,
      "seeded_at": 0,                      从旧配置播种过没有
      "platforms": {
        "feishu": {"default": "<id>", "entries": {"<id>": {"id", "name", ...}}}
      }
    }

## 判据

1. 只有默认那条会被用到。这不是"同时跑几个机器人" —— 切换默认之后
   要重启框架才生效（与模型条目同一条判据：运行中的通道实例是进程内长活的）。
2. 只有一处出口给明文：`credentials_for()`。其余读到的一律打码。
3. 默认那条不许删（与模型条目同一条），拒的时候给路线。
4. 不静默丢：名字空、超长、一个字段都没填，一律拒并说清缺什么。
5. 播种只一次，判据是 `seeded_at == 0`；从 `config.yaml` 与
   `data/qq_bot_credentials.json` 把现有那份搬进来，让老用户升级后
   看到的就是自己原来那套。
6. `/clear` 不清它 —— 配置不是会话态。登记在 `core/state_registry.py`
   的 `PROCESS_SHARED` 那一列（与模型条目同一格）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

__all__ = [
    "ChannelConfigError", "PLATFORMS", "MAX_PER_PLATFORM", "MAX_NAME",
    "entries_for", "entry_of", "default_id", "credentials_for",
    "create", "update", "delete", "set_default", "seed_from_legacy",
    "masked_fields", "load",
]

#: 每种平台的字段表。`secret` 里的那几格界面不打码回显、日志里也不打印。
#: 这是本模块唯一一份字段定义，`web/server.py` 与界面都从这里取。
PLATFORMS: dict[str, dict] = {
    "feishu": {"fields": ("app_id", "app_secret"), "secret": ("app_secret",)},
    "dingtalk": {"fields": ("app_key", "app_secret"), "secret": ("app_secret",)},
    "telegram": {"fields": ("bot_token",), "secret": ("bot_token",)},
    "qq_bot": {"fields": ("app_id", "app_secret"), "secret": ("app_secret",)},
}

#: 每种平台最多几条。超了拒，不静默丢。
MAX_PER_PLATFORM = 10
#: 条目名的长度上限。截断改的是用户起的名，所以超了是拒。
MAX_NAME = 40
#: 单格值的长度上限（凭据不该有这么长的，超了多半是粘错了）。
MAX_VALUE = 400

_LEDGER = Path("data/runtime/channel_configs.json")
_CREDS = Path("data/runtime/channel_credentials.json")

_VERSION = 1

# 打码用的字符。用 chr() 拼不写字面量：它不能进 GBK 控制台，
#   而 GBK 棘轮只看字面量，写成字面量就会多记一处「会崩」。
_MASK = chr(0x2022)

#: `config.yaml` 里凭据的写法是 `${FEISHU_APP_ID}`，真值在环境变量里。
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
#: 解析完还长这样 = 这一格其实没配（占位符没对上任何变量）。
_ENV_ONLY_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def _resolve_env(value: str) -> str:
    return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)


class ChannelConfigError(Exception):
    """拒绝一条操作。消息直接给用户看，所以要把路线写进去。"""


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def new_id() -> str:
    """铸一个条目 id。

    不用名字当键：名字要能改，而凭据是按 id 存的 —— 改名不该丢凭据。
    也不用会话键（`/clear` 会复用它）。
    """
    return "c" + secrets.token_hex(4)


def _empty() -> dict:
    return {"version": _VERSION, "seeded_at": 0, "platforms": {}}


def load(*, seed: bool = True) -> dict:
    """读账本。第一次读时顺手播种（从 `config.yaml` 里现有的那套）。"""
    data = _read(_LEDGER)
    if not isinstance(data.get("platforms"), dict) or not data.get("version"):
        data = _empty()
    if seed and not data.get("seeded_at"):
        try:
            seed_from_legacy(data)
        except Exception as exc:                              # noqa: BLE001
            print("[channels] 播种失败（%s: %s）" % (type(exc).__name__, exc))
    return data


def _platform(data: dict, name: str) -> dict:
    p = (data.get("platforms") or {}).get(name)
    if not isinstance(p, dict):
        p = {"default": "", "entries": {}}
        (data.setdefault("platforms", {}))[name] = p
    p.setdefault("default", "")
    if not isinstance(p.get("entries"), dict):
        p["entries"] = {}
    return p


def masked_fields(platform: str, fields: dict) -> dict:
    """按字段表打码。密钥一律八颗点，其余留头尾各四位。"""
    spec = PLATFORMS.get(platform) or {}
    secret = set(spec.get("secret") or ())
    out = {}
    for key in spec.get("fields") or ():
        value = str((fields or {}).get(key) or "")
        if not value:
            continue
        if key in secret:
            out[key] = _MASK * 8
        elif len(value) <= 8:
            out[key] = _MASK * len(value)
        else:
            out[key] = value[:4] + "…" + value[-4:]
    return out


def entries_for(platform: str) -> list[dict]:
    """这个通道的条目列表，凭据已打码。默认那条排在最前。"""
    data = load()
    p = _platform(data, platform)
    creds = _read(_CREDS)
    rows = []
    for cid, entry in (p.get("entries") or {}).items():
        if not isinstance(entry, dict):
            continue
        rows.append({
            "id": cid,
            "name": str(entry.get("name") or ""),
            "is_default": cid == p.get("default"),
            "fields": masked_fields(platform, creds.get(cid) or {}),
            "created_at": entry.get("created_at") or 0,
        })
    rows.sort(key=lambda r: (not r["is_default"], r["created_at"], r["name"]))
    return rows


def entry_of(cid: str) -> dict | None:
    """按 id 找一条（不打码的账本项 + 平台名）。找不到返回 None。"""
    data = load()
    for platform, p in (data.get("platforms") or {}).items():
        entry = (p.get("entries") or {}).get(cid)
        if isinstance(entry, dict):
            return {"platform": platform, **entry, "id": cid}
    return None


def default_id(platform: str) -> str:
    return str(_platform(load(), platform).get("default") or "")


def credentials_for(platform: str) -> dict:
    """默认那条的明文凭据。全仓唯一的明文出口。

    读不到就是空字典（没配过 / 老用户还没播种）。调用方自己决定回退到
    `config.yaml` 或环境变量 —— 那条回退只在通道构造时走一次。
    """
    data = load()
    cid = str(_platform(data, platform).get("default") or "")
    if not cid:
        return {}
    creds = (_read(_CREDS).get(cid) or {})
    return {k: str(v) for k, v in creds.items() if str(v)}


def _clean_fields(platform: str, fields: dict) -> dict:
    spec = PLATFORMS.get(platform) or {}
    out = {}
    for key in spec.get("fields") or ():
        value = str((fields or {}).get(key) or "").strip()
        if not value:
            continue
        if len(value) > MAX_VALUE:
            raise ChannelConfigError("「%s」太长了（上限 %d 字符）" % (key, MAX_VALUE))
        out[key] = value
    return out


def _check_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise ChannelConfigError("条目名不能为空")
    if len(name) > MAX_NAME:
        raise ChannelConfigError("条目名最多 %d 个字" % MAX_NAME)
    return name


def create(platform: str, name: str, fields: dict) -> dict:
    """新建一条。名字在同一个平台内不许重复（否则界面上两条长得一样）。"""
    if platform not in PLATFORMS:
        raise ChannelConfigError("不认识的通道：%s" % platform)
    name = _check_name(name)
    creds_in = _clean_fields(platform, fields)
    if not creds_in:
        raise ChannelConfigError("至少填一格凭据")
    data = load()
    p = _platform(data, platform)
    for entry in (p.get("entries") or {}).values():
        if str((entry or {}).get("name") or "") == name:
            raise ChannelConfigError("已经有一条叫「%s」的了，换个名字" % name)
    if len(p.get("entries") or {}) >= MAX_PER_PLATFORM:
        raise ChannelConfigError("这个通道最多 %d 条，先删一条再建" % MAX_PER_PLATFORM)

    cid = new_id()
    now = int(time.time())
    p["entries"][cid] = {"name": name, "created_at": now, "updated_at": now}
    if not p.get("default"):
        p["default"] = cid
    creds = _read(_CREDS)
    creds[cid] = creds_in
    _write(_LEDGER, data)
    _write(_CREDS, creds)
    return {"id": cid, "name": name, "is_default": p["default"] == cid}


def update(cid: str, *, name: str | None = None, fields: dict | None = None) -> dict:
    """改一条。名字与凭据各改各的，没给的那半不动。

    `fields` 里空着的格子不改 —— 与界面上那条「留空 = 不改」同一条规矩：
    界面不回显凭据，原样送回来的空串不等于"清空"。
    """
    data = load()
    found = None
    for platform, p in (data.get("platforms") or {}).items():
        if cid in (p.get("entries") or {}):
            found = (platform, p)
            break
    if found is None:
        raise ChannelConfigError("这条配置不见了，刷新一下")
    platform, p = found

    if name is not None:
        name = _check_name(name)
        for other_id, entry in (p.get("entries") or {}).items():
            if other_id != cid and str((entry or {}).get("name") or "") == name:
                raise ChannelConfigError("已经有一条叫「%s」的了，换个名字" % name)
        p["entries"][cid]["name"] = name

    if fields:
        creds_in = _clean_fields(platform, fields)
        if creds_in:
            creds = _read(_CREDS)
            merged = dict(creds.get(cid) or {})
            merged.update(creds_in)
            creds[cid] = merged
            _write(_CREDS, creds)

    p["entries"][cid]["updated_at"] = int(time.time())
    _write(_LEDGER, data)
    return {"id": cid, "platform": platform, "name": p["entries"][cid]["name"]}


def delete(cid: str) -> bool:
    """删一条。默认那条不许删 —— 删了之后这个通道就没有能用的凭据了。"""
    data = load()
    for platform, p in (data.get("platforms") or {}).items():
        if cid not in (p.get("entries") or {}):
            continue
        if p.get("default") == cid:
            others = [k for k in (p.get("entries") or {}) if k != cid]
            raise ChannelConfigError(
                "「默认」那条不能删 —— %s" % (
                    "先把另一条设为默认，再删它。" if others
                    else "这是唯一的一条，删了这个通道就没凭据了。"))
        p["entries"].pop(cid, None)
        _write(_LEDGER, data)
        creds = _read(_CREDS)
        if creds.pop(cid, None) is not None:
            _write(_CREDS, creds)
        return True
    return False


def set_default(platform: str, cid: str) -> dict:
    data = load()
    p = _platform(data, platform)
    if cid not in (p.get("entries") or {}):
        raise ChannelConfigError("这条配置不见了，刷新一下")
    p["default"] = cid
    _write(_LEDGER, data)
    return {"platform": platform, "default": cid}


def seed_from_legacy(data: dict) -> None:
    """把升级前那套搬成一条默认条目。只在 `seeded_at == 0` 时跑一次。

    三个来源：`config.yaml` 的 `channel.<平台>` 段（`${ENV}` 已解析）、
    `data/qq_bot_credentials.json`（QQ 自己的文件）。
    一个字段都没有的平台不建空条目 —— 那会让界面凭空多出一条空壳。
    """
    legacy: dict[str, dict] = {}
    try:
        import yaml

        from core import paths as _paths
        cfg_path = _paths.config_path()
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            ch_cfg = (cfg.get("channel") or {}) if isinstance(cfg, dict) else {}
            for platform in PLATFORMS:
                section = ch_cfg.get(platform) or {}
                if not isinstance(section, dict):
                    continue
                # `config.yaml` 里凭据写的是 `${FEISHU_APP_ID}` 这种占位符，
                #   真值在环境变量 / `.env` 里。照原样搬进来的话，播种出来的
                #   那条凭据就是字符串 "$ {FEISHU_APP_ID}" —— 界面显示得像配好了，
                #   一启动就 401。所以这里必须解析；解析不出来就跳过这一格。
                # 只看凭据那几格。这一段里还躺着 `proxy` / `polling_timeout`
                #   之类与凭据无关的键，它们也可能写成 `${TELEGRAM_PROXY}` 而
                #   那个变量本来就没设 —— 把它算进来，整趟播种会永远放弃，
                #   现有配置一条都接不进来。
                values = {}
                for key in (PLATFORMS[platform].get("fields") or ()):
                    value = section.get(key)
                    if not isinstance(value, (str, int)):
                        continue
                    raw = str(value)
                    for name in _ENV_RE.findall(raw):
                        if not os.environ.get(name):
                            # 环境变量还没读进来（`.env` 由 web 启动时装载，
                            #   而这条播种可能跑在更早的通道构造里）。这时候
                            #   播种出来的是一条空凭据，而 `seeded_at` 一旦
                            #   落下去就再也不会重来 —— 用户的配置就这么静默没了。
                            #   所以整趟放弃、不落 `seeded_at`，下次再试。
                            print("[channels] 环境变量 %s 还没装载，暂不播种" % name)
                            return
                    text = _resolve_env(raw).strip()
                    if text and not _ENV_ONLY_RE.match(text):
                        values[key] = text
                if values:
                    legacy[platform] = values
    except Exception as exc:                                  # noqa: BLE001
        print("[channels] 读 config.yaml 播种失败（%s: %s）"
              % (type(exc).__name__, exc))

    qq_path = Path("data/qq_bot_credentials.json")
    if qq_path.exists() and "qq_bot" not in legacy:
        creds = _read(qq_path)
        if creds:
            legacy["qq_bot"] = creds

    creds_out = _read(_CREDS)
    for platform, values in legacy.items():
        spec = PLATFORMS.get(platform) or {}
        picked = _clean_fields(platform, {
            k: values.get(k, "") for k in (spec.get("fields") or ())
        })
        if not picked:
            continue
        p = _platform(data, platform)
        cid = new_id()
        p["entries"][cid] = {"name": "原有配置", "created_at": int(time.time()),
                             "updated_at": int(time.time())}
        p["default"] = p.get("default") or cid
        creds_out[cid] = picked

    data["seeded_at"] = int(time.time())
    data["version"] = _VERSION
    _write(_LEDGER, data)
    _write(_CREDS, creds_out)
