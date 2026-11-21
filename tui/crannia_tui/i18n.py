"""TUI 自己那些话的界面语言 —— 与后端 `core/i18n.py` 同一条判据。

## 键是什么：中文原文就是键

与后端逐字同一条：`{"中文原文": "English"}`，没有 `status.running` 这种键名。
查不到就回落原文（永不显示裸键）—— 用户改了原文最多"这句还是中文"，
不会白屏。占位符由调用方填：`t("共 %s 条") % n`（`t()` 只换句子、不碰 `%s`）。

首尾空白先摘掉再查、查完原样放回去 —— 与后端 `tr_fmt` / 前端 `i18n.js`
同一个做法，否则 `" · 接近触发"` 这种带前导空格的键永远查不到。

## 两张表：后端的做基，TUI 只补差集

- 基：crannia 根的 `locales/<lang>.json`（后端那张表）。界面语言、加载短语、
  故障文案这些两边共用的术语直接用它，术语对齐是结构性的，不是靠人记。
- 补：`crannia_tui/locales/<lang>.json`（TUI 独有的句子）。
  多语言自动发现：放一个 `ja.json` 进去就能用，一行代码都不用改。

测试钉着"补集不许重复基"：同一个键只许有一个家，重复了就是两套译文在漂。
也钉着"用到的键都得有译文"，以及占位符个数一致。

## 语言从哪来

`configure()` 由入口在启动那一刻调一次（问后端 `core.i18n.current()`，
理由同上：系统探测、"跟随系统"、坏值回落全在后端一处）。TUI 没有设置页，
改了 `interface_language` 要重开界面才生效；`CRANNIA_INTERFACE_LANGUAGE`
留给测试与临时排查（只认表里真有的一门）。

## 读不到的时候

没设置 / 包读不出来 / 表里不是字符串，一律回落原文，绝不抛 ——
这个函数在渲染路径上，抛一次就是整屏空白。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

__all__ = ["SOURCE", "available", "configure", "current", "reload", "t"]

#: 源码语言。它就是表里的键，回落也回落到它。
SOURCE = "zh"

_PACK_DIR = Path(__file__).resolve().parent / "locales"

#: 进程级两格：语言（启动时定一次）、已读的表（按语言键）。
_lang: str = SOURCE
_home: Path | None = None
_packs: dict[str, dict] = {}
_noted: set[str] = set()


def configure(lang: str | None, *, home: Path | None = None) -> None:
    """定下这一进程用哪一门界面语言。

    *lang* 是 `core.i18n.current()` 的读数（或测试给的值）；认不出 / 空串 /
    `None` 都按源码语言处理（原文照常显示，不会出现裸键）。
    *home* 是 crannia 根：基表从 `home/locales/` 读。不传就只用补集。
    """
    global _lang, _home
    _lang = str(lang or "").strip().lower() or SOURCE
    if home is not None:
        _home = Path(home)
    _packs.clear()


def current() -> str:
    """当前界面语言。"""
    return _lang


def reload() -> None:
    """清缓存（测试改语言 / 表被改写）。下一次 `t()` 重新读盘。"""
    _packs.clear()
    _noted.clear()


def available() -> list[str]:
    """真有补集的那几门（源码语言永远在）。"""
    out = [SOURCE]
    try:
        for p in sorted(_PACK_DIR.glob("*.json")):
            if p.stem not in out:
                out.append(p.stem)
    except OSError:
        pass
    return out


def _files(lang: str) -> list[tuple[Path, bool]]:
    """基表在前、补集在后（补集赢 —— 但测试不许两边同时有同一个键）。

    第二个分量是"要不要把译文也 strip 掉"。基表要：那是扫描器按源码原样
    记的，同一句话被记成 `· 接近触发` 与 ` · 接近触发` 两条，译文便各自
    带/不带前导空格；不归一的话 `t()` 补回原文的空格后会出现两个空格。
    补集不要：那里只有我们手写的条目，`Context: ` 这种尾随空格是有意的。
    """
    out: list[tuple[Path, bool]] = []
    if _home is not None:
        out.append((Path(_home) / "locales" / ("%s.json" % lang), True))
    out.append((_PACK_DIR / ("%s.json" % lang), False))
    return out


def _note(path: Path, exc: BaseException) -> None:
    """一个包读不出来只说一次，走 stderr（全屏界面里 print 会弄花画面）。"""
    key = str(path)
    if key in _noted:
        return
    _noted.add(key)
    try:
        sys.stderr.write("[i18n] %s unreadable: %s: %s\n"
                         % (path, type(exc).__name__, exc))
        sys.stderr.flush()
    except Exception:                                    # noqa: BLE001
        pass


def _pack(lang: str) -> dict:
    """读一门语言的两张表并合并。读不出来的那张跳过，不抛。"""
    hit = _packs.get(lang)
    if hit is not None:
        return hit
    data: dict[str, str] = {}
    for path, _strip_values in _files(lang):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except Exception as exc:                         # noqa: BLE001
            _note(path, exc)
            continue
        if not isinstance(raw, dict):
            _note(path, ValueError("top level is not an object"))
            continue
        for key, value in raw.items():
            if isinstance(value, str):
                # 键统一 strip：后端那张表的键是扫描器按源码原样记下的，
                # 有些带前导/尾随空格（`  （未实现）`）。`t()` 按 strip 后的
                # 原文查，这里不归一就永远查不到 —— 而症状是"英文界面里
                # 这几句仍是中文"，看着像漏翻。
                core = str(key).strip()
                if core:
                    data[core] = value.strip() if _strip_values else value
    _packs[lang] = data
    return data


def t(text: str) -> str:
    """把一句界面文案换成当前语言；查不到，原文。"""
    if not text:
        return text
    if _lang == SOURCE:
        return text
    core = text.strip()
    if not core:
        return text
    hit = _pack(_lang).get(core)
    if not isinstance(hit, str):
        return text
    at = text.index(core)
    return text[:at] + hit + text[at + len(core):]
