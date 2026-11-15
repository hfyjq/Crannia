"""界面语言—— 按钮 / 标签 / 通知 / 命令回话用哪一门。

## 与「回答语言」的分界（两件事，各有一条轴）

| | 管什么 | 住哪 | 谁改 |
|---|---|---|---|
| 界面语言（本模块） | 按钮 / 标签 / 通知 / 命令回话 | `data/settings.json` 的 `interface_language` | 设置页 |
| 回答语言（`core/response_language.py`） | 模型写哪种语言 | `response_language` + `/language` | 设置页 / `/language` |

两者互不影响：界面换英文不等于让模型说英文 —— 一个是给谁看，一个是说什么。

## 键是什么：中文原文就是键

`locales/en.json` 的形状是 `{ "中文原文": "English" }` —— 没有 `settings.memory.viewer`
这种键名（那个反例是界面上直接出现裸键名）。三条由此而来：

1. 缺译文回落中文，永不显示裸键：`t()` 查不到就返回原文。
   这也是给用户改文案的自由 —— 改了原文最多"这句暂时还是中文"，不会白屏。
2. 占位符由调用方填：`t()` 只换句子，不碰 `%s` / `{name}`。
`t("已停止 %d 个后台任务") % n`。别在 t() 里做格式化（那时 `%d` 已经被吃掉，
   而键里带着 `%d`，永远查不到）。
3. 语言是用户的设置，`/clear` 不许碰它 —— 它住 `data/settings.json`，
   不在会话状态里。

## 默认是跟随系统

没设过的时候按系统语言来（有包就用；认不出就先英文、再中文）。
这条从安装引导第一屏就生效：壳把 `app.getLocale()` 写成
`CRANNIA_SYSTEM_LOCALE` 传给后端，小窗那句 `?lang=` 走同一套判断 ——
否则外国人开机第一眼是中文，而那是个他从没做过的决定。

## 读不到的时候

没设置 / 坏 JSON / 包读不出来，不抛异常（`t()` 在渲染路径上，抛一次就是整页空白）。
写坏了 ≠ 没设过：认不出的值回落 `zh`，不去跟随系统 ——
用户明确写过一笔（哪怕是错字），就不该被我们的系统探测顶掉。坏值喊一次（不刷屏）。
"""

from __future__ import annotations

import os
import json
import re
import threading
from collections import defaultdict
from pathlib import Path

__all__ = [
    "SETTING_KEY", "DEFAULT_LANG", "SYSTEM", "available", "current", "configured",
    "system_language", "t", "reload", "pack_for", "tr_fmt",
]

#: `data/settings.json` 里的键。
SETTING_KEY = "interface_language"

#: 「跟随系统」的哨兵。设置里没有这个键、空串、或者字面写 `system` 都算它。
#: 存的是事实（没设过），不是值 —— 同 「别把生效值顶上去冒充设过」。
SYSTEM = "system"

#: 源码语言 —— 认不出 / 没有任何包时的最后回落。
#: 默认值不是它：没设过，跟随系统，
#:   判据是「不然外国人怎么办」—— 一个英文系统的用户第一眼就该是英文，
#:   而那是个他从没做过的决定，不该由我们默默替他选中文。
DEFAULT_LANG = "zh"

#: 给人看的名字。以后加 `ja` 时在这里加一行；没登记的包用代码当名字。
NAME_OF: dict[str, str] = {"zh": "简体中文", "en": "English"}

#: 语言包目录。开发树里是相对路径（`tests/_sandbox.py::sandbox()` 靠 chdir 生效），
#:   安装版按安装根解析（`core.paths.asset_dir`，与 providers / skills 同一条）。
#:
#: 2026-11-13 修：这里原来是写死的 `Path("locales")`，安装版的 cwd 是数据根
#:   （`%APPDATA%\Crannia`），那儿没有 locales，于是 `available()` 只报得出中文，
#:   界面上"英语"整个消失，而开发树里一切正常。同一族：`test_paths` 那句
#:   「代码资产按安装根解析，不按 cwd」—— i18n 当时漏了。
def _locales_dir() -> Path:
    """开发树走相对路径（在 `core/paths.py` 里那一条判据），安装版走安装根。"""
    from core import paths as _paths
    return Path(_paths.asset_dir("locales"))


_SETTINGS = Path("data/settings.json")

#: 进程级缓存，一格（`core/state_registry.py` 按"会被写的模块级名字"登记）：
#:   · `packs` —— `lang -> (mtime_ns, pack)`
#:   · `cur`   —— 设置文件的读数缓存（`t()` 每请求喊几百次，不能每次读盘）
#:   · `bad_noted` —— 坏值只喊一次
#: 它不是会话态：界面语言是用户设置，`/clear` 不许碰它（判据 3）。
_cache: dict = {
    "packs": {},
    "cur": {"mtime": None, "lang": DEFAULT_LANG},
    "bad_noted": False,
}
_lock = threading.Lock()


def available() -> list[tuple[str, str]]:
    """界面语言的下拉项：`zh`（源码语言）+ `locales/*.json` 里发现的包。

    自动发现（`locales/README.md` 「加一门语言 = 一行代码都不改」）：
    放一个 `locales/ja.json` 进去，下拉里就有它。
    `source.json` 是扫描产物（不是包），必须排掉 —— 否则下拉里会多一个
    `source` 选项，选了它界面就整页回落到中文且没有任何提示。
    """
    out = [("zh", NAME_OF["zh"])]
    try:
        for p in sorted(_locales_dir().glob("*.json")):
            code = p.stem
            if code == "source" or code.startswith("_") or code == "zh":
                continue
            out.append((code, NAME_OF.get(code, code)))
    except OSError:
        pass
    return out


def _pack(lang: str) -> dict:
    """读语言包（按 mtime 缓存）。读不出来，空包（`t()` 于是回落原文）。"""
    p = _locales_dir() / ("%s.json" % lang)
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return {}
    with _lock:
        hit = _cache["packs"].get(lang)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level is %s, not an object" % type(data).__name__)
        data = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
    except Exception as exc:                       # noqa: BLE001
        print("[i18n] %s 读不出来，回落中文：%s: %s"
              % (p, type(exc).__name__, exc))
        data = {}
    with _lock:
        _cache["packs"][lang] = (mtime, data)
    return data


def _os_locale() -> str:
    """这台机器的界面语言标签（尽力而为，读不到就空串，绝不猜）。

    Windows 上不用 `locale.getlocale()`：它给出的是
      `("Chinese (Simplified)_China", "936")` 这种不是标签的串，
      拿去映射只会落到"认不出"。改用 `GetUserDefaultUILanguage()` 的 LCID ——
      那正是"系统界面语言"这个概念的官方来源。
    """
    try:                                            # Windows：用户界面语言
        import ctypes
        lcid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
        return {0x04: "zh", 0x09: "en"}.get(lcid & 0x3FF, "")
    except Exception:                               # noqa: BLE001
        pass
    try:                                            # POSIX / 其它
        import locale
        for fn in ("getlocale", "getdefaultlocale"):
            try:
                got = getattr(locale, fn)()[0]
            except Exception:                       # noqa: BLE001
                continue
            if got:
                return str(got)
    except Exception:                               # noqa: BLE001
        pass
    return ""


def _map_locale(raw: str) -> str:
    """语言标签 → 我们真有包的那一门；认不出，空串。"""
    tag = str(raw or "").strip().lower().replace("_", "-")
    if not tag:
        return ""
    known = {c for c, _n in available()}
    base = tag.split("-")[0]
    for cand in (tag, base):
        if cand in known:
            return cand
    # 中文的变体很多（`zh-cn` / `zh-hans` / `zh-tw`）—— 源码语言兜到这里。
    if base == "zh" and "zh" in known:
        return "zh"
    return ""


def system_language() -> str:
    """系统语言 → 我们有的那一门。

    判据顺序：`CRANNIA_SYSTEM_LOCALE`（壳用 `app.getLocale()` 传下来）>
    `LC_ALL` / `LANG`（源码跑的机器）> 平台探测（`_os_locale`）。
    全都认不出，`en`（有包的话）> `zh` —— "看不懂"的时候英文比中文更可能是
      外国人读得懂的，而中文是我们自己的源码语言、永远在。
    """
    cached = _cache.get("sys")
    if cached:
        return str(cached)
    raw = (os.environ.get("CRANNIA_SYSTEM_LOCALE")
           or os.environ.get("LC_ALL") or os.environ.get("LANG") or "")
    if not str(raw).strip():
        raw = _os_locale()
    hit = _map_locale(str(raw))
    if not hit:
        hit = "en" if "en" in {c for c, _n in available()} else DEFAULT_LANG
    _cache["sys"] = hit
    return hit


def configured() -> str | None:
    """设置里写着的那个值；没设过 / 就是「跟随系统」，`None`（事实，不是值）。

    调用方（`GET /api/settings`）要把它原样发给界面 —— 界面据此把下拉停在
      「跟随系统」上。拿 `current()` 顶上去，那一格就变成一句假话
      （存事实，不存值）。
    """
    try:
        raw = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    val = raw.get(SETTING_KEY)
    if val is None:
        return None
    code = str(val).strip().lower()
    if not code or code == SYSTEM:
        return None
    return code


def current() -> str:
    """当前界面语言。没设过，跟随系统；坏值 / 认出但没包，`zh`。"""
    # 环境变量优先（`CRANNIA_INTERFACE_LANGUAGE`）：测试要确定性 ——
    #   否则"开发机把界面切成英文"会让一批断言中文的测试当场变红
    #   生产里没人设它。
    forced = os.environ.get("CRANNIA_INTERFACE_LANGUAGE", "").strip().lower()
    if forced and forced in {c for c, _n in available()}:
        return forced
    try:
        mtime = _SETTINGS.stat().st_mtime_ns
    except OSError:
        lang = system_language()
        _cache["cur"].update(mtime=None, lang=lang)
        return lang
    with _lock:
        if _cache["cur"]["mtime"] == mtime:
            return _cache["cur"]["lang"]
    code = configured()
    if code is None:
        lang = system_language()
    elif code in {c for c, _n in available()}:
        lang = code
    else:
        # 写坏了 ≠ 没设过：用户明确写过一笔（哪怕是错字），
        #    就不该被我们的系统探测顶掉 —— 回落源码语言，并喊一次。
        lang = DEFAULT_LANG
        if not _cache["bad_noted"]:
            _cache["bad_noted"] = True
            print("[i18n] 设置里的 %s=%r 认不出 —— 按中文处理"
                  % (SETTING_KEY, code))
    with _lock:
        _cache["cur"].update(mtime=mtime, lang=lang)
    return lang


def t(text: str) -> str:
    """把一句界面文案换成当前界面语言；查不到，原文（永不显示裸键）。

    占位符由调用方填，见模块 docstring 第 2 条。
    """
    if not text:
        return text
    lang = current()
    if lang == DEFAULT_LANG:
        return text
    return _pack(lang).get(text, text)


def pack_for(lang: str | None = None) -> dict:
    """取一份语言包（默认当前语言）—— `GET /api/i18n` 用它给前端 `t()`。

    源码语言（`zh`）返回空包：前端 `t()` 查不到就回落原文，
      与后端 `t()` 逐字同一条判据（永不显示裸键）。
    """
    code = (lang or current()).strip().lower()
    if code == DEFAULT_LANG:
        return {}
    return dict(_pack(code))


_PH = re.compile(r"\{[^{}]*\}")


def tr_fmt(template: str, *values) -> str:
    """翻译再填值 —— f-string 的替代品（接线）。

    为什么需要它：f-string 求值之后只剩结果，而键是模板
    （`已停止 {n} 个后台任务`），`t()` 永远查不到。这里先拿着模板翻，再填值。

    按占位符的名字对，不按位置：译文可以调整语序
      （`{a} 在 {b} 里`，`{b} is inside {a}`），位置法会把值塞错格。
    同一个名字出现多次，按出现顺序逐个取。
    译文里删掉某个占位符，那个值不用，不报错（译文不该因为少一个值就崩）。
    """
    names = _PH.findall(template)
    buckets: dict[str, list] = defaultdict(list)
    for nm, v in zip(names, values):
        buckets[nm].append(v)

    def _sub(m):
        lst = buckets.get(m.group(0))
        return str(lst.pop(0)) if lst else m.group(0)

    # 表里的键是 strip 过的（扫描器按行归一）==> 首尾空白留在外面，
    #   否则带前导换行的模板永远查不到。
    head = template[:len(template) - len(template.lstrip())]
    tail = template[len(template.rstrip()):]
    return head + _PH.sub(_sub, t(template.strip())) + tail


def reload() -> None:
    """清缓存（设置改了 / 包更新了）。下一次调用重新读盘。

    连系统语言那格一起清：测试会在两个用例之间改
    `CRANNIA_SYSTEM_LOCALE`，不清的话第二个用例读到的是第一个的答案。
    """
    with _lock:
        _cache["packs"].clear()
        _cache["cur"].update(mtime=None, lang=DEFAULT_LANG)
        _cache.pop("sys", None)
