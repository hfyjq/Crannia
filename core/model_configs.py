"""模型的「配置条目」账本 —— 用户自己建"用什么模型"。

一句话：把"用什么模型"从 `config.yaml` 的 `models:` 段 + 一份硬编码目录
（`creators/models.json`），改成用户自己建的「配置条目」——
配置名称 / 模型名 / 请求地址 / api 四样，按 12 张大类卡片分组。

动机：① 模型列表要跟着上游持续更新，硬编码那份一改就过时；
② 中转站填不进来 —— 用户没法给自己的代理地址配一个模型名。

## 这一层只做「建得出来、存得下、读得回、判得准」

它是纯数据 + 纯判据：不建 provider 实例、不联网、不进事件流。
「配置条目 → 能跑的 provider 实例」是同一模块的 `provider_for()`。

## 八条判据（每条都指得出出处）

1. 每卡至多 10 条 —— 每个大类卡片各 10 条，不是全局 10 条。
   超了拒，不静默丢（静默少一条 = 让用户以为建成了）。
2. 四样各一份 —— 一条配置只有一个地址 / 一个 api / 一个模型名。
   不支持"一条挂多个地址"。
3. 大类隐含协议 —— `claude` 卡 = Anthropic 协议，**其余（含「其他」）=
   OpenAI 兼容**，没有单独的"协议"字段。
   若某个服务两种协议都不沾，今天没有落点。
4. 凭据与配置分两个文件 —— 配置可以随便看、将来要能导出；
   凭据不行。本模块只有一个出口把明文交出去（`api_for`），
   其余读取者拿到的都是打码过的（`has_api` + 尾 4 位）。
5. 超限拒、不截断（截断改的是用户起的名）。
6. id 是铸出来的（`m` + 8 位十六进制），不是会话键 ——
   `/clear` 会复用会话键。
7. 判据全是纯函数，建/改两条路共用同一套 —— 两处各判一次就是"会各自漂"
   的形状。端点与界面都不重判。
8. 一处定义：卡片的 id / 显示名 / 对应的 provider 目录只在这张表里写一遍
   （`CARDS`），界面从 `/api/model-configs` 拿，不自己再抄一份。

`/clear` 不清它 —— 配置条目不是会话态（同 `groups`：清一段对话
不该把用户配好的模型也清掉）。登记在 `core/state_registry.py` 的
`PROCESS_SHARED` 那一列。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

# ----------------------------------------------------------------------
# 落点
# ----------------------------------------------------------------------

#: 账本。运行数据一律进 `data/runtime/`。
LEDGER_PATH = Path("data/runtime/model_configs.json")

#: 凭据单独一个文件（判据 4）。它同时要进
#: `core/security.py::DEFAULT_FORBIDDEN_PATHS` —— 否则模型读得到自己的 key
#: （兜底那一档翻成 `READ_ONLY` 之后，那份清单就是"读不到"的唯一依据）。
CREDS_PATH = Path("data/runtime/provider_credentials.json")


# ----------------------------------------------------------------------
# 12 张大类卡片（判据 8：只此一处）
# ----------------------------------------------------------------------
#
# 元组 = (卡片 id, 显示名, 对应的 provider 目录)
#
# 12 个名字：GPT · Claude · Deepseek · Qwen · GLM · Kimi · Gemini ·
#   Grok · Minimax · 火山引擎 · 腾讯混元 · 其他。
# 「其他」承接 5 个（平台/中转站，让用户自己去填）——
#   它们没有自己的卡，一律走 OpenAI 兼容那一路。
CARDS: tuple[tuple[str, str, str], ...] = (
    ("gpt",        "GPT",      "openai"),
    ("claude",     "Claude",   "anthropic"),
    ("deepseek",   "Deepseek", "deepseek"),
    ("qwen",       "Qwen",     "qwen"),
    ("glm",        "GLM",      "zhipu"),
    ("kimi",       "Kimi",     "moonshot"),
    ("gemini",     "Gemini",   "gemini"),
    ("grok",       "Grok",     "grok"),
    ("minimax",    "Minimax",  "minimax"),
    ("volcengine", "火山引擎",  "volcengine"),
    ("hunyuan",    "腾讯混元",  "hunyuan"),
    #: `ollama`（本地）。
    #:   它不是新机制：本地模型就是一个指向本机、且不要 key 的
    #:   OpenAI 兼容端点（`providers/ollama/manifest.yaml` 早就是
    #:   `openai_compat` + `http://localhost:11434/v1`）。
    #:   它等价于在普通页面里填了一个 localhost 地址 —— 没有单独的机制。
    ("ollama",     "Ollama（本地）", "ollama"),
    ("other",      "其他",      "openai"),
)

CARD_IDS: tuple[str, ...] = tuple(c[0] for c in CARDS)
CARD_LABEL: dict[str, str] = {c[0]: c[1] for c in CARDS}
CARD_PROVIDER: dict[str, str] = {c[0]: c[2] for c in CARDS}

#: 判据 1。每张卡各自一条上限（不是全局）。
MAX_PER_CARD = 10

#: 判据 5：超限拒，不截断。四个字段各一条上限。
MAX_NAME, MAX_MODEL, MAX_URL, MAX_API = 40, 120, 300, 400

#: 判据 2 的窗口那一格（「高级选项」）。填了就按它来。
#: 上下界只防手滑（`0` / `1e9`），不是产品判断。
WINDOW_MIN, WINDOW_MAX = 1_024, 10_000_000

#: 能力覆盖开关 —— 条目自己的三态。
#:
#: 由来：能力按 `(provider, model)` 查表，而表是外部维护、会错的
#: （表可能把实际能读图的模型记成 `input: ["text"]`）。用户在端配置页
#: 建的条目（中转站 / 自建模型）常常查不到，而唯一的覆盖口是改
#: `config.yaml`，它只对 `config.yaml` 里那个模型生效，条目没有自己的口。
#:
#:   auto = 不表态（查表 > 默认；与 `config.yaml` 没写这一格逐字同义）
#:   on   = 强制能（映射成 `supports_vision: True`，比表高）
#:   off  = 强制不能（映射成 `supports_vision: False`，比表高）
#:
#: `off` 是"用户自己关" —— 与 `_capability` 那条「表可以开、不可以关」
#:   不冲突：那条说的是表的否定不承重，用户自己关是另一回事。
CAP_KEYS: tuple[str, ...] = ("vision", "audio")
CAP_MODES: tuple[str, ...] = ("auto", "on", "off")
DEFAULT_CAPABILITIES: dict[str, str] = {k: "auto" for k in CAP_KEYS}
_CAP_MODEL_KEY: dict[str, str] = {"vision": "supports_vision",
                                  "audio": "supports_audio"}
_CAP_LABEL: dict[str, str] = {"vision": "图片", "audio": "音频"}

#: `providers/` 的根 —— 按模块位置算，不按 cwd 算。
#:
#: 不能用相对路径 `Path("providers")`（与 `bus/core.py` 同一个口径，
#:   生产里 cwd 就是仓根）：测试跑在沙箱里（cwd 是个临时目录），那里
#:   根本没有 `providers/`，`preflight` 恒回"装不上"，**「设为默认」
#:   在那条 HTTP 测试里恒失败**，而症状看起来像"这条配置本身有问题"。
#: 判据是两样东西的性质不一样：
#:   · `providers/` 是代码，它跟着模块走（打包后在 `resources/app/providers`，也在那儿）；
#:   · 账本（`data/runtime/…`）是用户数据，它跟着 cwd 走（沙箱要靠它隔离）。
#: 这里永远走模块位置（开发树也如此），与 `bus/core.py` 那条
#:   `_paths.asset_dir("providers")` 只在"cwd ≠ 仓根"时分叉，而两边都指向
#:   同一棵代码树：`asset_dir` 在开发树返回相对路径（测试夹具靠 chdir 隔离），
#:   这一处不能那样 —— 沙箱里 cwd 是临时目录，相对路径会让 `preflight` 恒回"装不上"。
PROVIDERS_DIR = Path(__file__).resolve().parent.parent / "providers"

#: 老配置里 `provider:` 那个字段 → 新卡片的对应表。
#:
#: 它和 `CARD_PROVIDER` 不是同一张表的反向 —— 老 `config.yaml` 里
#: `glm` / `kimi` 写的是厂商名，而 `providers/` 下的目录叫 `zhipu` / `moonshot`。
#: 两张表各有各的用途（这张只管一次性播种），所以不强行合并。
_LEGACY_PROVIDER_CARD: dict[str, str] = {
    "anthropic": "claude",
    "openai": "gpt",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "glm": "glm",
    "kimi": "kimi",
    "gemini": "gemini",
    "grok": "grok",
    "minimax": "minimax",
    "volcengine": "volcengine",
    "hunyuan": "hunyuan",
}

_LOCK = threading.Lock()

#: 账本的内存镜像。不是真相源 —— 真相是那两个文件（同 `groups._cache`）。
#: `None` = 还没装载过（与"装载了但是空的"是两件事）。
_cache: dict | None = None
_creds: dict | None = None

#: 建好的 provider 实例：`{cid: (updated_at, 实例)}`。
#:
#: 它与上面两格不是一类东西：那两格是账本的内存镜像，这一格是
#: 派生出来的机器（里面有 httpx 客户端、连接池）。缓存它的理由是**轮首每轮
#: 都问**——不缓存就是每轮一次 `import` + 构造。
#:
#: 失效判据挂在 `updated_at` 上（不是"记得清"）：用户改了那条配置
#:   （换模型 / 换地址 / 换 key），`update()` 会刷新 `updated_at`，指纹失配
#:下一轮重建。这正是「改会话模型 = 一次冷启动 + 出声」的落地 ——
#:   改完当场就该是新的那个，而不是等重启。
#: 不用 `_cache` 那条"重新装载整个文件"的路：那会让别的会话的实例
#:   一起被丢掉（一百多个会话各重建一次 = 一轮里几十次 import）。
_INSTANCES: dict[str, tuple[float, object]] = {}


# ----------------------------------------------------------------------
# 读 / 写（形状照 `core/groups.py` —— 同一族的账本，不另造一套）
# ----------------------------------------------------------------------

def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:                                    # noqa: BLE001
        # 读不动，当空。判据同 `groups._read`：宁可让用户重配一次，
        #   不可凭一个读不动的文件把界面上那几张卡渲染成半截。
        #   但不重播种 —— 重播会把用户删掉的条目复活（见 `load()`）。
        print(f"[model_configs] 账本读不动（{type(exc).__name__}: {exc}）—— 按空账本走")
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, data: dict) -> None:
    """原子写：临时文件 + `os.replace`（同 `groups._write`）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _empty() -> dict:
    """空账本的形状。 `default` 与 `configs` 同一个文件 ——
    一个写者、一把锁、一次原子写（分成两个文件就要回答"写了一半怎么办"）。"""
    return {"configs": {}, "default": "", "seeded_at": 0.0}


def _normalise(raw: dict) -> dict:
    """把读回来的东西收成账本该有的形状（坏一格不许放倒整本）。

    只丢"当不成一行"的东西（不是字典 / 没有 `card` 这一格）——
      不认识的卡片 id 一律原样保留。判据是"不许静默销毁用户手写的行"：
      将来卡片表若改名（或某张卡暂时摘掉），丢掉 = 用户配的东西无声消失；
      留着，那张卡回来时它自己就回来了。
代价是账本里可能有"渲染不出来"的行，所以 `cards_payload()` 会数出来
      （`orphan_count`）而不是装作没有 —— 这就是本仓那条「不许静默」。
    """
    out = _empty()
    configs = raw.get("configs")
    if isinstance(configs, dict):
        for cid, entry in configs.items():
            if isinstance(entry, dict) and isinstance(entry.get("card"), str):
                one = dict(entry)
                one["id"] = str(cid)
                out["configs"][str(cid)] = one
    default = raw.get("default")
    out["default"] = str(default) if isinstance(default, str) else ""
    if out["default"] not in out["configs"]:
        # 默认那条没了（被手删/文件改坏），清掉指针，而不是留一个悬空 id。
        #   下游据此走"退回 config.yaml"那条路（fail-open，老行为不变）。
        out["default"] = ""
    try:
        out["seeded_at"] = float(raw.get("seeded_at") or 0.0)
    except (TypeError, ValueError):
        out["seeded_at"] = 0.0
    return out


def _ledger_now() -> dict:
    """当前账本的一份可改副本（调用方在锁里改它）。"""
    if _cache is None:
        return _normalise(_read(LEDGER_PATH))
    return {"configs": dict(_cache["configs"]), "default": _cache["default"],
            "seeded_at": _cache["seeded_at"]}


def load(*, seed: bool = False) -> dict:
    """装载账本（启动时调一次）。返回副本。

    播种只在 `seed=True` 那次跑（`bus/core.py::init_bus` 调）。

    为什么不把播种挂在"文件不存在"上：
      · 动作顺序依赖 —— 先点「新建」的进程会带着"只有那一条"落盘，文件从此
        存在，老配置永不出现；先 GET 一下才有。那是顺序依赖，不是不变量。
      · `config.yaml` 一时读不动（YAML 坏了 / 被挪走），会写下一本
        空账，迁移永久失效，而界面上是"12 张空卡"，看不出发生过什么。
      · `${VAR}` 的解不开（没跑 `load_dotenv` 的调用者），会把
        9 条配置连空凭据一起落盘，也同样永不重来。
判据换成两个：`seeded_at == 0`（"从没播过"是一条记下来的事实，
      不是从"文件在不在"往回推 —— 来源必须跟着值走）
      且 `_seed_from_config()` 这次真认出了东西（`None` = 没认出，不写不标）。
    """
    global _cache, _creds
    with _LOCK:
        exists = LEDGER_PATH.exists()
        raw = _read(LEDGER_PATH)
        #: 文件在、却读不出账本的形状（坏了 / 被别的东西占了），不播种。
        #:   否则"读坏了"会被当成"从没播过"，把用户删掉的条目全复活。
        #:   `_read` 对"不存在"与"读不动"都回 `{}`（它俩在它那层长得一样），
        #:   所以这里必须另外用 `exists` 把两者分开。
        readable = (not exists) or isinstance(raw.get("configs"), dict)
        if seed and readable and not _normalise(raw)["seeded_at"]:
            seeded = _seed_from_config()
            if seeded is not None:
                # 并进已有条目（用户可能已经先建过几条）—— 不是覆盖。
                merged = _normalise(raw)
                merged["configs"].update(seeded["configs"])
                if not merged["default"]:
                    merged["default"] = seeded["default"]
                merged["seeded_at"] = seeded["seeded_at"]
                try:
                    _write(LEDGER_PATH, merged)
                except Exception as exc:                        # noqa: BLE001
                    print(f"[model_configs] 播种写不进去（{type(exc).__name__}: {exc}）"
                          f" —— 本次运行**仍然有**这些配置")
                raw = merged
        _cache = _normalise(raw)
        _creds = _read(CREDS_PATH)
        if not isinstance(_creds, dict):
            _creds = {}
    return {"configs": dict(_cache["configs"]), "default": _cache["default"],
            "seeded_at": _cache["seeded_at"]}


def all_entries() -> dict:
    """当前全部配置条目（懒加载）。返回副本。"""
    led = _snapshot()
    return {"configs": dict(led["configs"]), "default": led["default"],
            "seeded_at": led["seeded_at"]}


def merge_ledger(entries: dict, default: str = "") -> dict:
    """把另一个账本的条目并进来（「导入设置包」用）。返回计数。

    判据：按 id 并、不删任何本地条目（导入是"加上"，不是"替换"）——
      与 `NEVER_RESTORE` 同一条：别人的包不许把你本机的东西抹掉。
    每条都过 `create` 那套校验（card / name / model / base_url / window /
      capabilities）—— 包是外来的数据，不做形状校验就等于让它往账本里写任意字段。
    每卡上限（`MAX_PER_CARD`）照旧；超了跳过并报数，不静默。
    id 只收形状安全的（`[A-Za-z][A-Za-z0-9_-]{0,63}`），否则另铸一个 ——
      它是字典键，会被拼进 `data/providers/<dir>` 那类路径。

    返回 `{added, updated, skipped, bad, default}`；`bad` 是没通过校验的那几条
    （带一句为什么），调用方要把它们说给用户听。
    """
    global _cache
    added = updated = skipped = 0
    bad: list = []
    changed = False
    import re as _re
    _id_re = _re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    with _LOCK:
        ledger = _ledger_now()
        for cid, raw in (entries or {}).items():
            if not isinstance(raw, dict):
                bad.append("%s（不是对象）" % (cid,))
                continue
            try:
                card = card_of(raw.get("card"))
                clean = {
                    "card": card,
                    "name": _text(raw.get("name"), MAX_NAME, "配置名称")
                            or CARD_LABEL[card],
                    "model": _text(raw.get("model"), MAX_MODEL, "模型名"),
                    "base_url": base_url_of(raw.get("base_url", "")),
                    "window": window_of(raw.get("window")),
                    "capabilities": capabilities_of(raw.get("capabilities")),
                }
                if not clean["model"]:
                    raise ModelConfigError("模型名是空的")
            except (ModelConfigError, KeyError, TypeError, ValueError) as exc:
                bad.append("%s（%s）" % (cid, exc))
                continue
            key = cid if isinstance(cid, str) and _id_re.match(cid) else new_id()
            cur = ledger["configs"].get(key)
            if cur is not None:
                entry = dict(cur)
                entry.update(clean)
                entry["updated_at"] = time.time()
                ledger["configs"][key] = entry
                updated += 1
                changed = True
                continue
            same = sum(1 for e in ledger["configs"].values()
                       if e.get("card") == card)
            if same >= MAX_PER_CARD:
                skipped += 1
                continue
            ledger["configs"][key] = {"id": key, **clean,
                                      "created_at": time.time(),
                                      "updated_at": time.time()}
            added += 1
            changed = True
        if not ledger["default"] and default and default in ledger["configs"]:
            ledger["default"] = default
            changed = True
        if changed:
            _cache = ledger
            _persist(ledger, _creds or {})
        out_default = ledger["default"]
    return {"added": added, "updated": updated, "skipped": skipped,
            "bad": bad, "default": out_default}


def _snapshot() -> dict:
    if _cache is None:
        return load()
    return _cache


def get(cid: str) -> dict | None:
    """取一条。不存在回 `None`（不是空字典 —— 两者在调用方长得一样，
    而"没有这条"与"这条是空的"是两件事）。

    副本要够深：`capabilities` 是嵌套字典，`dict(entry)` 只拷一层
调用方一句 `entry["capabilities"]["vision"] = ...` 会把内存里的真账本
      改掉，而且没有任何东西会红（只改了一个本地副本，下一次 `cards_payload()`
      却看到了那个值）。
      判据：回给调用方的副本，改了不许影响真账本。
    """
    entry = _snapshot()["configs"].get(str(cid or "").strip())
    return _copy_entry(entry) if entry else None


def _copy_entry(entry: dict) -> dict:
    """一行的够深副本（`capabilities` 是嵌套字典 —— 见 `get`）。"""
    out = dict(entry)
    if isinstance(out.get("capabilities"), dict):
        out["capabilities"] = dict(out["capabilities"])
    return out


def default_id() -> str:
    """「默认启用」的那条 id。没有默认回 `""` —— 那是合法状态
    （此时下游退回 `config.yaml` 的 `model:`，老行为不变）。"""
    return str(_snapshot().get("default") or "")


def api_for(cid: str) -> str:
    """取一条的 api 明文。这是全模块唯一一个明文出口（判据 4）。

    谁能调它：建 provider 实例那条路（`provider_for`）。端点与界面一律不许——
      它们走 :func:`masked`。
    """
    if _creds is None:
        load()
    return str((_creds or {}).get(str(cid or "").strip()) or "")


def masked(cid: str) -> dict:
    """打码过的凭据摘要 —— 端点与界面只许拿这一份。

    回 `{"has_api": bool, "api_tail": "abcd"}`。尾 4 位是为了让用户能认出
    自己填的是哪一把（多把 key 时这是唯一的区分手段），而不是为了泄密。
    """
    api = api_for(cid)
    return {"has_api": bool(api), "api_tail": api[-4:] if len(api) >= 4 else ""}


# ----------------------------------------------------------------------
# 判据（纯函数 —— 建/改两条路共用同一套，判据 7）
# ----------------------------------------------------------------------

class ModelConfigError(ValueError):
    """入参不合法。消息说得出是哪一条判据（够用户改）。

    继承 `ValueError`：端点那边 `except ValueError` 就能回 400，
    不必再 import 这个类（少一处耦合，同 `groups.GroupError`）。
    """


def _text(raw, limit: int, what: str) -> str:
    """取一段文本。超限拒，不截断（判据 5）。"""
    s = str(raw if raw is not None else "").strip()
    if len(s) > limit:
        raise ModelConfigError("%s太长（%d 字 > 上限 %d 字）" % (what, len(s), limit))
    return s


def card_of(raw) -> str:
    """卡片 id。不在那 12 张里，拒（带路线：把合法的列出来）。"""
    cid = str(raw or "").strip()
    if cid not in CARD_PROVIDER:
        raise ModelConfigError("没有这张卡片：%r（合法的 12 张：%s）"
                               % (raw, "、".join(CARD_IDS)))
    return cid


def provider_dir_of(card: str) -> str:
    """卡片 → `providers/` 下的目录名（判据 3：大类隐含协议）。"""
    return CARD_PROVIDER[card_of(card)]


def base_url_of(raw) -> str:
    """请求地址。允许留空 —— 留空 = 用那家 provider 自带的地址
    （老 `config.yaml` 里 anthropic 那条就没有 `base_url`）。
    填了就必须是 http(s)。"""
    s = _text(raw, MAX_URL, "请求地址")
    if s and not (s.startswith("http://") or s.startswith("https://")):
        raise ModelConfigError("请求地址要以 http:// 或 https:// 开头（收到 %r）" % s)
    return s


def window_of(raw) -> int | None:
    """上下文容量（「高级选项」）。留空 = `None` = 交给"查表/实际"那几层去定。

    `bool` 不算整数（`isinstance(True, int)` 是 `True` ——
    「`effort_level: true` 会被当成档 1」是同一个坑）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            raw = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ModelConfigError("上下文容量要填整数（收到 %r）" % (raw,))
    if not WINDOW_MIN <= raw <= WINDOW_MAX:
        raise ModelConfigError("上下文容量超出合理范围：%d（只许 %d ~ %d）"
                               % (raw, WINDOW_MIN, WINDOW_MAX))
    return raw


def _api_of(raw) -> str:
    """api。允许留空（本地 provider 有的不要 key）。"""
    return _text(raw, MAX_API, "api")


def capability_modes(entry) -> dict:
    """读那一侧：条目里的三态能力覆盖 → 规范化字典。

    坏值不抛 —— 账本可以被手改、也可能被老版本写过。一条坏值不许把
      整条配置变成"装不上"（那会让用户连改都改不了）。
不认识的值落回 `auto`，并出声（不静默）。
    """
    raw = (entry or {}).get("capabilities")
    out = dict(DEFAULT_CAPABILITIES)
    if not isinstance(raw, dict):
        return out
    for key in CAP_KEYS:
        value = raw.get(key)
        if value is None:
            continue
        mode = str(value).strip().lower()
        if mode in CAP_MODES:
            out[key] = mode
        else:
            print(f"[model_configs] capabilities.{key}={value!r} 不认识"
                  f"—— 按 auto 处理（只接受 auto / on / off）")
    return out


def capabilities_of(raw, *, base=None) -> dict:
    """写那一侧：三态覆盖 → 规范化字典。不合规，`ModelConfigError`。

    `raw is None` = 这一格什么都没说，原样返回 `base`（更新时"不动"）。
      `create` 那边传 `None` 得到的就是全 `auto`。
    只认那三个字符串 —— 坏值拒，不静默夹取。
       `True` / `1` 也拒：`str(True).lower()` 是 `"true"`，不在那三个里。
    """
    out = capability_modes({"capabilities": base}) if base is not None \
        else dict(DEFAULT_CAPABILITIES)
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ModelConfigError("capabilities 要是对象（收到 %s）"
                               % type(raw).__name__)
    unknown = [str(k) for k in raw if k not in CAP_KEYS]
    if unknown:
        raise ModelConfigError("capabilities 没有这几格：%s（只有 %s）"
                               % ("、".join(unknown), " / ".join(CAP_KEYS)))
    for key in CAP_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:                       # 显式 null = 清回 auto
            out[key] = "auto"
            continue
        mode = str(value).strip().lower()
        if mode not in CAP_MODES:
            raise ModelConfigError("「%s」能力只能是 %s（收到 %r）"
                                   % (_CAP_LABEL[key], " / ".join(CAP_MODES), value))
        out[key] = mode
    return out


# ----------------------------------------------------------------------
# 写
# ----------------------------------------------------------------------

def _persist(ledger: dict, creds: dict) -> None:
    """两个文件各写一次。写不进去照样算成功（本次运行内是真的）——
    同 `groups.create` 那条；但要说出来。"""
    try:
        _write(LEDGER_PATH, ledger)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 账本写不进去（{type(exc).__name__}: {exc}）"
              f" —— 本次运行**改动仍然生效**")
    try:
        _write(CREDS_PATH, creds)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 凭据写不进去（{type(exc).__name__}: {exc}）"
              f" —— 本次运行**改动仍然生效**")


def create(*, card, name, model, base_url="", api="", window=None,
           capabilities=None) -> dict:
    """建一条配置。不合规，`ModelConfigError`（一个字节都不落盘 ——
    同 `skill_factory` 那条"先体检、后落盘"）。"""
    card = card_of(card)
    clean = {
        "card": card,
        "name": _text(name, MAX_NAME, "配置名称") or CARD_LABEL[card],
        "model": _text(model, MAX_MODEL, "模型名"),
        "base_url": base_url_of(base_url),
        "window": window_of(window),
        #: 缺省 = 两格都 `auto`（= 老条目/老调用方的行为，一个字节不变）。
        "capabilities": capabilities_of(capabilities),
    }
    if not clean["model"]:
        raise ModelConfigError("模型名不能空（它就是发到上游的那个串）")
    api = _api_of(api)

    global _cache, _creds
    cid = new_id()
    now = time.time()
    entry = {"id": cid, **clean, "created_at": now, "updated_at": now}
    with _LOCK:
        ledger = _ledger_now()
        #: 判据 1 的上限在锁里判。数在锁外面的话，两次并发的新建会各自
        #:   看到 9 条、各自通过 —— 而这里确实是并发的（WebUI 是 `waitress
        #:   threads=12`）。这是本模块唯一一处必须在临界区里判的判据
        #:   （别的都只看入参，与并发无关）。同 `groups.create` 那条。
        same = sum(1 for e in ledger["configs"].values() if e.get("card") == card)
        if same >= MAX_PER_CARD:
            raise ModelConfigError("「%s」这张卡最多 %d 条配置（已经有 %d 条）—— "
                                   "先删一条，或者改用别的大类。"
                                   % (CARD_LABEL[card], MAX_PER_CARD, same))
        ledger["configs"][cid] = entry
        creds = dict(_creds or {})
        if api:
            creds[cid] = api
        # 第一条配置自动成为默认（判据：没有默认时下游要退回 config.yaml，
        #   而用户刚建的第一条显然就是他想要的）—— 之后改默认只能走 `set_default`。
        if not ledger["default"]:
            ledger["default"] = cid
        _cache, _creds = ledger, creds
        _persist(ledger, creds)
    return _copy_entry(entry)


#: `update` 的"这一格没传"哨兵。与 `None` 必须分开：`window=None` 是
#: 「把容量清空，交给查表」，而"没传"是「别动它」。
_UNSET = object()


def update(cid: str, *, name=_UNSET, model=_UNSET, base_url=_UNSET,
           api=_UNSET, window=_UNSET, capabilities=_UNSET) -> dict:
    """改一条。只改传上来的那几格（缺席 = 不动它）。`KeyError` = 没有这条。

    别写成"没传就当空"（那会把配置名清成卡片名，而用户只是改了个地址）。
    `capabilities` 缺席 = 不动；传了就按格合并（只给了 `vision` 时
      `audio` 保持原值 —— 不是把没提的那格重置成 auto）。
    """
    cid = str(cid or "").strip()
    global _cache, _creds
    with _LOCK:
        ledger = _ledger_now()
        if cid not in ledger["configs"]:
            raise KeyError(cid)
        entry = dict(ledger["configs"][cid])
        if name is not _UNSET:
            entry["name"] = _text(name, MAX_NAME, "配置名称") or CARD_LABEL[entry["card"]]
        if model is not _UNSET:
            m = _text(model, MAX_MODEL, "模型名")
            if not m:
                raise ModelConfigError("模型名不能空")
            entry["model"] = m
        if base_url is not _UNSET:
            entry["base_url"] = base_url_of(base_url)
        if window is not _UNSET:
            entry["window"] = window_of(window)
        if capabilities is not _UNSET:
            # 按格合并（`base` 是现值）—— 只给了 `vision` 时 `audio` 不动。
            entry["capabilities"] = capabilities_of(
                capabilities, base=entry.get("capabilities"))
        entry["updated_at"] = time.time()
        ledger["configs"][cid] = entry
        creds = dict(_creds or {})
        if api is not _UNSET:
            a = _api_of(api)
            # 传空串 = 清掉（用户按了"清空"），而不是"不动"。
            #   两种意图靠传不传这一格区分，不靠值。
            if a:
                creds[cid] = a
            else:
                creds.pop(cid, None)
        _cache, _creds = ledger, creds
        _persist(ledger, creds)
    return _copy_entry(entry)


def set_default(cid: str) -> dict:
    """把某一条设为「默认启用」。`KeyError` = 没有这条。"""
    cid = str(cid or "").strip()
    global _cache
    with _LOCK:
        ledger = _ledger_now()
        if cid not in ledger["configs"]:
            raise KeyError(cid)
        ledger["default"] = cid
        _cache = ledger
        _persist(ledger, dict(_creds or {}))
    return _copy_entry(ledger["configs"][cid])


def delete(cid: str) -> bool:
    """删一条。返回是不是真的删掉了一条。

    默认那条不许删 —— 删了之后"用什么模型"会静默退回 `config.yaml`，
      而用户在界面上做的是一个明确的删除动作，不该顺带换掉模型。
拒，且带路线（"先指定另一个默认"）。
    """
    cid = str(cid or "").strip()
    global _cache, _creds
    with _LOCK:
        ledger = _ledger_now()
        if cid not in ledger["configs"]:
            return False
        if ledger["default"] == cid:
            others = [e for e in ledger["configs"].values() if e["id"] != cid]
            route = ("先把另一条设为默认，再删它。" if others else
                     "这是唯一的一条配置，删了就没有模型可用了。")
            raise ModelConfigError("「默认启用」的那条不能删 —— %s" % route)
        ledger["configs"].pop(cid, None)
        creds = dict(_creds or {})
        creds.pop(cid, None)
        _cache, _creds = ledger, creds
        _persist(ledger, creds)
    return True


def new_id() -> str:
    """铸一个配置 id。判据 6：不是会话键、也不是用户的输入。"""
    return "m" + uuid.uuid4().hex[:8]


# ----------------------------------------------------------------------
# 一次性播种：把 `config.yaml` 的 `models:` 段搬进来（用户口径：我搬，不给按钮）
# ----------------------------------------------------------------------

def _raw_legacy_hints() -> tuple[int, int]:
    """从原始 `config.yaml` 文本里数两件事：`${VAR}` 的条数、带多把 key 的条数。

    为什么不复用 `load_config` 的结果：它已经解析过 `${VAR}`（解不开的
      变空串），在那上面判"有没有占位符"恒假。这两件事问的是文件里写了什么，
      不是跑起来是什么，所以必须读原文。
    """
    try:
        import yaml
        raw = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    except Exception:                                           # noqa: BLE001
        return 0, 0
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        return 0, 0
    placeholders = pools = 0
    for row in models.values():
        if not isinstance(row, dict):
            continue
        if "${" in str(row.get("api_key") or ""):
            placeholders += 1
        pool = row.get("api_keys")
        if isinstance(pool, list) and len(pool) > 1:
            pools += 1
    return placeholders, pools


def _seed_from_config() -> dict | None:
    """把老 `config.yaml` 的 `models:` 段搬成配置条目。

    回 `None` = "这次没搬成，别记账、下次再说" —— 三种情形：
      · `config.yaml` 读不动（YAML 坏了 / 被挪走 / 沙箱里根本没有）；
      · 它没有 `models:` 段（本来就没有可搬的）；
      · `${VAR}` 解不开 —— 这次跑在一个没先 `load_dotenv` 的调用者里，
        `os.environ` 是空的。这一条是最危险的：搬下去就等于把 N 条配置
        连同空凭据一起落盘，而 `seeded_at` 一写就永不重来
用户回来看到的是"N 条都在、每条 `has_api: false`"。

    搬什么、不搬什么：
      · ✅ 老 `models:` 的每一条 —— 名字照抄老键，`api` 用展开后的真值；
      · ❌ 不搬它们的窗口 —— 那几个 65536/131072 是死数
        （整段 `models:` 在运行期零读者）；
      · ✅ 唯一例外：与生效中那条匹配的条目，带上顶层声明的
        `context_window`。理由是「**声明是用户自己写的，最高，不许被悄悄
        改掉**」—— 不带就等于让压缩闸门静默移动。
        搬的是声明值，不是"实际值"（当下两者恰好相等）。
    """
    try:
        from bus.core import load_config
        cfg = load_config("config.yaml")
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 播种：config.yaml 读不动"
              f"（{type(exc).__name__}: {exc}）—— 这次不搬，下次装载再试")
        return None

    if not isinstance(cfg, dict):
        return None

    top = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    active_provider = str(top.get("provider") or "")
    active_model = str(top.get("model_name") or "")
    top_window = top.get("context_window")

    old = cfg.get("models")
    if not isinstance(old, dict) or not old:
        return None

    led = _empty()
    creds: dict[str, str] = {}
    chosen = ""
    now = time.time()
    #: `${VAR}` 的条数只能从原始文本里数 —— `load_config` 已经把它们
    #:   解析过了（解不开的会变成空串），照解析后的值去数，`"${" in ...` 恒假
    #:那道"环境没就绪就别搬"的闸会恒真地放行（本模块最危险的那条路）。
    placeholders, extra_keys = _raw_legacy_hints()
    cut_names: list[str] = []
    for key, row in old.items():
        if not isinstance(row, dict):
            continue
        card = _LEGACY_PROVIDER_CARD.get(str(row.get("provider") or ""), "other")
        model = str(row.get("model_name") or "")
        if not model:
            continue
        raw_key = str(row.get("api_key") or "")
        cid = new_id()
        clean_name = str(key)
        if len(clean_name) > MAX_NAME:
            cut_names.append(clean_name)
            clean_name = clean_name[:MAX_NAME]
        # 默认那条 = 当前真正在跑的那条（按是事实记，不是拿三项去比出来）。
        is_active = (str(row.get("provider") or "") == active_provider
                     and model == active_model)
        window = None
        if is_active:
            try:
                window = window_of(top_window)
            except ModelConfigError:
                window = None
        led["configs"][cid] = {
            "id": cid, "card": card, "name": clean_name,
            "model": model, "base_url": str(row.get("base_url") or ""),
            "window": window, "created_at": now, "updated_at": now,
        }
        api = raw_key.strip()
        if api and "${" not in api:
            creds[cid] = api
        if is_active and not chosen:
            chosen = cid

    # 一条都没认出来（老配置的形状变了），不记账，让它走 fail-open。
    if not led["configs"]:
        return None

    # 环境没就绪就不搬：老配置里有 `${VAR}`，而一条都没解出来
    #这次跑的进程里 `os.environ` 是空的（没先 `load_dotenv`）。
    if placeholders and not creds:
        print(f"[model_configs] 播种：老配置里 {placeholders} 条写的是 `${{VAR}}`，"
              f"而这次一条都没解开 —— **像**是环境变量还没就绪（没先 load_dotenv）。"
              f"这次不搬，下次装载再试")
        return None

    led["default"] = chosen or sorted(led["configs"])[0]
    led["seeded_at"] = now
    # 播种不改 `config.yaml`（它今天仍然是"退回时那条路"的真相源）。
    try:
        _write(CREDS_PATH, creds)
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 播种：凭据写不进去（{type(exc).__name__}: {exc}）")
    # 报数要连"其中几条带凭据"一起报 —— `M < N` 正是那条会静默发生的事故
    #   （跑了 / 有变化 / 缺了什么，三件事都要说）。
    print(f"[model_configs] 播种：从 config.yaml 搬进 {len(led['configs'])} 条配置"
          f"，其中 {len(creds)} 条带凭据")
    if extra_keys:
        print(f"[model_configs] 播种：有 {extra_keys} 条老配置带**多把 key**（轮换池）——"
              f"只搬了第一把，其余没搬（要轮换的话请在新页面上建多条）")
    if cut_names:
        print(f"[model_configs] 播种：{len(cut_names)} 条老配置的名字超 {MAX_NAME} 字，"
              f"已截短：{cut_names[:3]}")
    return led


# ----------------------------------------------------------------------
# 给界面/端点的那一份（打码，判据 4）
# ----------------------------------------------------------------------

def cards_payload() -> dict:
    """`GET /api/model-configs` 的载荷。

    绝不回 api 明文 —— 每条只带 `has_api` + `api_tail`。
    卡片表照发全部 12 张（空卡也发）—— 界面靠它渲染窗格，
      而"这张卡还没有配置"是要看得见的状态（没读数 ≠ 可以清空）。
    """
    led = _snapshot()
    by_card: dict[str, list] = {c: [] for c in CARD_IDS}
    for entry in led["configs"].values():
        card = entry.get("card")
        if card not in by_card:
            # 卡片表里没有这张卡，渲染不出来，但不能装作没有（`_normalise`
            #   刻意保留了它）。数出来交给界面出声，见下面的 `orphan_count`。
            continue
        row = {
            "id": entry.get("id"),
            "card": card,
            "name": entry.get("name") or CARD_LABEL.get(card, card),
            "model": entry.get("model") or "",
            "base_url": entry.get("base_url") or "",
            "window": entry.get("window"),
            "is_default": entry.get("id") == led["default"],
            "created_at": entry.get("created_at") or 0,
            "updated_at": entry.get("updated_at") or 0,
        }
        row.update(masked(entry.get("id")))          # has_api / api_tail
        # 能力跟模型走。端配置页换一条配置时，
        #   用户要看得见"这个模型收不收图 / 收不收音频、这个答案是谁给的"。
        #   这里算的是行为值（与 `build_provider` 同一条 `_capability`），
        #   不是原始表：表可以开、不可以关（见那条的 docstring）。
        try:
            from bus.loaders.provider_loader import _capability
            dirname = CARD_PROVIDER.get(card, "")
            model = str(entry.get("model") or "").strip()
            #: 把用户自己勾的覆盖口喂进同一处判定（别在这里另算一套）。
            modes = capability_modes(entry)
            declared = {_CAP_MODEL_KEY[k]: modes[k] == "on"
                        for k in CAP_KEYS if modes[k] != "auto"}
            if declared:
                declared["capability_source"] = "声明(本配置)"
            vision, vsrc = _capability(declared, "supports_vision", "image", True,
                                       dirname, model)
            audio, asrc = _capability(declared, "supports_audio", "audio", False,
                                      dirname, model)
            row["capabilities"] = {
                "vision": vision, "audio": audio,
                "vision_source": vsrc, "audio_source": asrc,
            }
            #: 三态原值 —— 编辑表单要凭它回填（行为值反推不出"用户选了哪个"）。
            row["capability_modes"] = modes
        except Exception as exc:                                # noqa: BLE001
            # 能力读不出来不该让整页读不出来；但也不许装作"纯文本"
            # （读不到 ≠ 0），这一格不出现，界面就不显示 chips。
            print("[model_configs] 能力计算失败（%s: %s）—— 这条不显示能力"
                  % (type(exc).__name__, exc))
        by_card[card].append(row)
    cards = []
    for cid, label, provider_dir in CARDS:
        rows = sorted(by_card[cid], key=lambda r: r.get("created_at") or 0)
        for i, r in enumerate(rows):                 # 卡内序号，界面用
            r["index"] = i
        cards.append({
            "id": cid, "label": label, "provider": provider_dir,
            "configs": rows, "count": len(rows),
            "is_default_card": any(r["is_default"] for r in rows),
        })
    return {
        "cards": cards,
        "default": led["default"],
        "max_per_card": MAX_PER_CARD,
        #: 三条上限由后端给 —— 前端不自己写死（写死就是第二份真相源）。
        "max_name": MAX_NAME, "max_model": MAX_MODEL, "max_url": MAX_URL,
        "window_min": WINDOW_MIN, "window_max": WINDOW_MAX,
        "seeded_at": led["seeded_at"],
        #: 卡片表里认不出来、因而渲染不出来的行数。不是 0 就该出声
        #:   （跑了/坏了都得说；这里同理 —— "有几条看不见"是有用的事实）。
        "orphan_count": _orphan_count(led),
    }


def _orphan_count(led: dict) -> int:
    """账本里渲染不出来的行数 —— 卡片表里认不出那张卡的那些。

    它不是 0 就该出声（跑过了要说、有东西看不见也要说）。
    """
    return sum(1 for e in (led.get("configs") or {}).values()
               if e.get("card") not in CARD_PROVIDER)


# ----------------------------------------------------------------------
# 生效链：配置条目 -> 一个能跑的 provider 实例
# ----------------------------------------------------------------------
#
# 落点只有一处（`PipelineScheduler.initialize()` —— 它自己的 docstring
#    写着「这里是"框架启动了"的唯一一处」）：
#    两个入口（`main.py` / `web/server.py`）与 `/api/restart` 全都只调它
#写两处 = 迟早漏一处。

def _model_cfg_of(entry: dict, cid: str) -> dict:
    """条目 → `build_provider` 的那个 `config["model"]` 段（一处定义）。

    留空 = 不写这一格（不是写空串）—— 写了空串会盖掉 manifest 里
    那家自带的地址（`build_provider` 是 `model_cfg.get("base_url", 兜底)`，
    键在就轮不到兜底）。老 `config.yaml` 的 anthropic 那条正是留空。
    """
    model_cfg: dict = {"model_name": str(entry.get("model") or "").strip(),
                       "api_key": api_for(cid)}
    if entry.get("base_url"):
        model_cfg["base_url"] = entry["base_url"]
    if entry.get("window"):
        model_cfg["context_window"] = entry["window"]
    #: 三态覆盖 → `build_provider` 认得的那两个键。
    #:   `auto` 不写（= 让查表那一层说话，与没写这一格逐字同义）；
    #:   `on`/`off` 映射成真布尔，比表高（`_capability` 的 ① 层）。
    modes = capability_modes(entry)
    declared = {_CAP_MODEL_KEY[k]: modes[k] == "on"
                for k in CAP_KEYS if modes[k] != "auto"}
    if declared:
        model_cfg.update(declared)
        #: 来源标签要跟着值走 —— 否则界面上会写"声明(config.yaml)"，
        #:   而用户是在端配置页勾的。
        model_cfg["capability_source"] = "声明(本配置)"
    return model_cfg


def _dirname_model(cid: str):
    """条目 → `(目录名, 模型名)`；缺任一，`(None, 原因)`。纯函数。"""
    entry = get(cid)
    if not entry:
        return None, "没有这条配置"
    dirname = CARD_PROVIDER.get(str(entry.get("card") or ""))
    if not dirname:
        return None, "这张卡不认识（%r）" % entry.get("card")
    if not str(entry.get("model") or "").strip():
        return None, "这条没填模型名"
    return dirname, None


def instance_for(cid: str, *, fallback=None, say: bool = False):
    """某一段对话要用的那个 provider 实例。

    与 :func:`provider_for` 的差别只有它注册什么：
      · `provider_for` = 「默认启用那条」的路（开机时接一次）：它还要
        登记目录名那份元数据、并把目录名别名挂上（`ensure_provider`
        按目录名解析，见 `core/group_dispatch.py`）。
      · `instance_for` = 「这一段对话冻的那条」的路（每轮都可能问到）：
        只注册在 `cid` 下。

    它绝不能挂目录名别名，也不能写目录元数据 —— 那是"谁赢看运气"：
      `gpt` 与「其他」两张卡都映射到 `openai` 目录（见 `CARDS`），
      而"这一段对话用的那条"本来就可能不是默认那条，让它去改
      `_read_models` 与 `/api/status` 读的那一份，就是把「当前默认」说成一个
      别的会话正在用的东西（标签在说谎）。
别名与元数据只有一个主人：默认那条（`provider_for` 与
      `scheduler._apply_configured_model`）。

    进程内缓存（`_INSTANCES`，按 `updated_at` 失效）—— 轮首每轮都问，
      不缓存就是每轮一次 `import` + 构造。

    *say* 只在用户显式改这一段对话的模型时打开（那时该出声，
    同改档那条：改档 = 一次冷启动且要出声）；轮首那条路不开，
    否则每一轮都打一行。
    """
    entry = get(cid)
    if not entry:
        return fallback
    dirname, why = _dirname_model(cid)
    if not dirname:
        if say:
            print(f"[model_configs] 配置条目 '{cid}' 用不了：{why}")
        return fallback

    stamp = float(entry.get("updated_at") or 0)
    hit = _INSTANCES.get(cid)
    if hit and hit[0] == stamp:
        return hit[1]

    try:
        from bus.loaders.provider_loader import build_provider
        instance, derived = build_provider(PROVIDERS_DIR / dirname,
                                           {"model": _model_cfg_of(entry, cid)})
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 配置条目 '{cid}' 装不上"
              f"（{type(exc).__name__}: {exc}）—— 这一段对话沿用原模型")
        return fallback

    from bus.registry import register_provider
    register_provider(cid, instance)
    _INSTANCES[cid] = (stamp, instance)
    if say:
        print(f"[model_configs] 这一段对话改用：『{entry.get('name')}』"
              f"（{dirname} / {entry.get('model')} / 窗口 "
              f"{derived.get('window_tokens')} 来源 {derived.get('window_source')}）")
    return instance


def find_by_ref(text: str) -> dict | None:
    """按用户看得见的东西找一条配置 —— 配置名 或 id。找不到 / 撞名，`None`。

    用途：`/model <名字>` 与界面那一族（用户手上只有配置名称，
      `m3f8a1c2` 这种 id 他记不住）。
    撞名一律不算数（回 `None`）—— 每张卡各允许 10 条，而名字是用户
      自己起的，两张卡各有一条「我的中转站」是完全现实的。
      此时"挑第一条"就是静默指错（两个身份塌成一个）。
调用方拿 `None` 去报错并列出候选（拒绝要带路线）。
    匹配是宽松的（去空白 + 忽略大小写）：用户打的是名字，不是主键。
    """
    q = str(text or "").strip()
    if not q:
        return None
    led = _snapshot()
    hit = led["configs"].get(q)
    if hit:
        return dict(hit)
    low = q.lower()
    same = [dict(e) for e in led["configs"].values()
            if str(e.get("name") or "").strip().lower() == low]
    return same[0] if len(same) == 1 else None


def name_clashes(text: str) -> list:
    """这个引用撞了几条（`find_by_ref` 回 `None` 时用它把候选列出来）。

    分开一个函数是因为两种"找不到"要给两句不同的话：
      "没这个名字"，列全部；"撞名了"，列撞的那几条（让用户改掉其中一个）。
    """
    low = str(text or "").strip().lower()
    if not low:
        return []
    return [dict(e) for e in _snapshot()["configs"].values()
            if str(e.get("name") or "").strip().lower() == low]


def list_names() -> list:
    """`[(配置名, id, 卡片显示名, 模型名)]` —— `/model` 与设置页那两个下拉用它。

    它不是"全部条目"的另一个真相源：就是上面那个账本的一张视图，
      排序确定（卡序 + 卡内建序），界面/命令两处都不许自己再排一遍。
    """
    led = _snapshot()
    rows = []
    for card, label, _dir in CARDS:
        for e in sorted((e for e in led["configs"].values()
                         if e.get("card") == card),
                        key=lambda e: e.get("created_at") or 0):
            rows.append((e.get("name") or "", e.get("id") or "", label,
                         e.get("model") or ""))
    return rows


def names_payload() -> list:
    """`list_names()` 的字典视图 —— 界面那一族下拉框唯一该用的形状。

    回 `[{id, name, card_label, model}]`（不是一串裸 id）：界面上要显示
      "人认得的名字"，只给 id 的话用户看到的是 `m3f8a1c2`。
    只有这一份映射：设置页的子 agent 下拉、群成员的模型下拉都走它
      （两处各写一遍 `for name, cid, label, model in ...` 就是会各自漂）。
    """
    return [{"id": cid, "name": name or cid, "card_label": label, "model": model}
            for name, cid, label, model in list_names()]


#: 配置条目 id 的形状（`new_id()` 铸的就是它）。只此一处认这个形状 ——
#: `group_dispatch` 等处不许自己写正则（换形状时改一处）。
_ID_RE = re.compile(r"^m[0-9a-f]{8}$")


def is_id(value) -> bool:
    """这个值长得像一条配置条目的 id 吗（不保证它存在）。"""
    return bool(_ID_RE.match(str(value or "").strip()))


def provider_for(cid: str, *, fallback=None):
    """把「默认启用」那条配置接成这一进程在用的 provider。

    失败一律回 `fallback`（fail-open）—— 三条"失败"的路
      （没有这条 / 卡片不认识 / 装不上）合成一条，因为下游只有一种反应：
      退回 `config.yaml` 那个模型。分成三种回法就是三处要各自维护的分支。

    注册两个名字：
      · `cid`（`m`+8 位）—— 框架按它取实例（`get_provider(cid)`）；
      · 目录名 —— 只在它是「默认启用」那条时挂（`ensure_provider` 按目录名
        解析，见 `core/group_dispatch.py`；不挂别名的话，群成员 / 子 agent 会
        静默退回老 `config.yaml` 的模型，而用户看不出来）。
        必须是"只有默认那条" —— `gpt` 与「其他」两张卡**都映射到
        `openai` 目录**（`CARDS` 那张表），两条都挂别名的话"谁赢"就是
        最后一次注册的，那是运气不是判据。

    元数据登记在目录名下（不是 cid）—— `web/server.py::_read_models`
       按它列模型目录，多出 cid 键会让老页面凭空长出几张假卡片。
       登记的是条目生效后的值（窗口 / 模型名），这正是
       「显示的与使用的必须同源」。

    构造那一段与 `instance_for` 共用 `_model_cfg_of`
      （两处各拼一次 `model_cfg` 就是那个"加了 base_url 忘了加 window"的形状）。
      差别仍然只在注册什么，理由见 `instance_for` 的 docstring。
    """
    entry = get(cid)
    if not entry:
        return fallback
    dirname = CARD_PROVIDER.get(str(entry.get("card") or ""))
    model = str(entry.get("model") or "").strip()
    if not dirname or not model:
        print(f"[model_configs] 配置条目 '{cid}' 缺卡片或模型名 —— 退回原模型")
        return fallback

    try:
        from bus.loaders.provider_loader import build_provider
        instance, derived = build_provider(PROVIDERS_DIR / dirname,
                                           {"model": _model_cfg_of(entry, cid)})
    except Exception as exc:                                    # noqa: BLE001
        print(f"[model_configs] 配置条目 '{cid}' 装不上"
              f"（{type(exc).__name__}: {exc}）—— 退回 config.yaml 那个模型")
        return fallback

    from bus.registry import register_provider, register_provider_metadata
    register_provider(cid, instance)
    register_provider_metadata(dirname, _metadata_of(entry, derived))
    if default_id() == cid:
        register_provider(dirname, instance)
    _INSTANCES[cid] = (float(entry.get("updated_at") or 0), instance)
    print(f"[model_configs] 生效：『{entry.get('name')}』"
          f"（{dirname} / {model} / 窗口 {derived.get('window_tokens')}"
          f" 来源 {derived.get('window_source')}）")
    return instance


def preflight(cid: str) -> str:
    """只试装、不注册：这条配置今天装得上吗？回 `""` = 装得上，否则回一句人话。

    为什么非要有它：`设为默认` 原来只写账本，装不上时（比如 Anthropic
      那一侧的 SDK 没装、或者卡片的目录被删了）界面上照样回一句成功，
      而失败只躺在日志里，用户看到的是"设了没反应"（标签在说谎）。
先试装、装得上才落盘（同 `skill_factory` 那条"先体检、后落盘"）。

    它不注册任何东西（区别于 `provider_for`）—— 探路不该留下脚印。
    它不联网：`build_provider` 只 import + 构造。
    """
    entry = get(cid)
    if not entry:
        return "没有这条配置"
    dirname, why = _dirname_model(cid)
    if not dirname:
        return why
    cfg = _model_cfg_of(entry, cid)
    try:
        from bus.loaders.provider_loader import build_provider
        build_provider(PROVIDERS_DIR / dirname, {"model": cfg})
    except Exception as exc:                                    # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, exc)
    return ""


def _metadata_of(entry: dict, derived: dict) -> dict:
    """给注册表的那份元数据（形状与 `load_provider` 那一份对齐）。

    加了三格 `model_config*` / `model` —— 老页面与 `/api/status` 靠它们
    说清"现在用的是哪一条配置"，而不是只有 provider 目录名。
    """
    mf = derived.get("manifest_cfg") or {}
    man = derived.get("manifest") or {}
    return {
        "name": derived.get("name") or "",
        "api_mode": mf.get("api_mode", "openai_compat"),
        "description": man.get("description", ""),
        "version": man.get("version", "0.1.0"),
        "default_model": mf.get("default_model", ""),
        "base_url": entry.get("base_url") or mf.get("base_url", ""),
        "context_window": derived.get("window_tokens"),
        "window_source": derived.get("window_source"),
        "supports_function_calling": mf.get("supports_function_calling", True),
        "supports_streaming": mf.get("supports_streaming", True),
        # 端配置页换模型时能力跟着模型走 ——
        #   只有 `supports_vision` 且 `_model_cfg_of` 不传能力键时，它永远是
        #   默认 True；`supports_audio` 则根本进不了元数据。
        #   `build_provider` 会按 `(provider_dir, model_name)` 查能力表。
        "supports_vision": derived.get("supports_vision"),
        "supports_vision_source": derived.get("supports_vision_source"),
        "supports_audio": derived.get("supports_audio"),
        "supports_audio_source": derived.get("supports_audio_source"),
        "supports_thinking": mf.get("supports_thinking", False),
        "max_output_tokens": derived.get("max_output_tokens"),
        # 这三格让界面能显示"正在用哪条配置"
        "model_config": entry.get("id"),
        "model_config_name": entry.get("name"),
        "model": derived.get("model_name"),
    }


def active_summary() -> dict:
    """现在生效的那条配置 —— 给 `/api/status` 那一族显示用。

    没有默认条目，回 `{}`，调用方照旧显示 `config.yaml` 那两个值
      （读不到不等于 0，这里"没有"与"有但是空的"分得开）。
    """
    cid = default_id()
    entry = get(cid)
    if not entry:
        return {}
    return {
        "id": cid,
        "name": entry.get("name") or "",
        "model": entry.get("model") or "",
        "provider": CARD_PROVIDER.get(str(entry.get("card") or ""), ""),
        "card": entry.get("card") or "",
        "window": entry.get("window"),
    }


def reset_for_tests() -> None:
    """清内存缓存。不碰磁盘、也不播种（同 `groups.reset_for_tests`）。

    置成空账本而不是 `None` —— 置 `None` 会让下一次 `_snapshot()` 去
    `load()`，而 `load()` 在真实 cwd 下会播种，于是测试读到开发机上的
    `config.yaml`（测试不许继承开发机的状态）。
    """
    global _cache, _creds
    with _LOCK:
        _cache = _empty()
        _creds = {}
        # 实例缓存也要清：它是按 `updated_at` 失效的，而测试里
        #   两条用例可能铸出同一个 cid（同一个 `Math.random` 种子不会，
        #   但 `reset_for_tests` 之后重播的同一份夹具会），不清就会拿到
        #   上一条用例那个装着别的 key 的实例，而它看起来是对的。
        _INSTANCES.clear()
