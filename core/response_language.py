"""回答语言—— 模型写什么语言，三层取值。

## 三层

| 层 | 谁 | 默认 | 怎么改 | 存哪 |
|---|---|---|---|---|
| ① 环境定型 | 操作系统语言 | 首次启动按环境定 | 不改 | 不存（每次现读） |
| ② 模型回答语言 | 设置项 | 未设 = 跟随① | 设置页 | `data/settings.json` |
| ③ 本会话覆盖 | `/language` | = ② | `/language` | 内存（不落盘） |

②的「未设」存的是事实（键不存在 / `null`），不是值 ——
  拿去比会撒谎（用户手改成正好等于①，系统会以为他没设过）。同框架预设的取舍。

③不落盘 —— 照 `/effort` 与 allowlist
  （"永久放行只能由用户在设置页明确做"）两条先例。

## 这一条不做成语言包

那一行是 2 条一整句、无插值（`zh` / `en` 各一句），加第三门语言 = 加一行。
别写成 `t("prompt.language")` —— 那是把一个 2 条的表伪装成基础设施。

## 代价

那一行进 system prompt（前缀），`/language` 改了会动前缀，
那一轮是一次冷启动（与 `/effort` 同一条判据）。`/language` 要当场说出来。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

__all__ = [
    "LANGS", "LANG_NAMES", "SETTING_KEY", "detect_env_language",
    "configured_language", "current", "prompt_line", "normalize",
    "session_override", "set_session", "clear_session",
]

#: 认哪两门。枚举就是这两条 —— 加第三门 = 这里加一行 + `LANG_NAMES` 加一行。
LANGS: tuple[str, ...] = ("zh", "en")

#: 给人看的名字（将来界面包做完后它该从那儿取；本模块只管"现在是哪一个"这个枚举）。
LANG_NAMES: dict[str, str] = {"zh": "简体中文", "en": "English"}

#: `data/settings.json` 里的键。
SETTING_KEY = "response_language"

#: 相对路径：`tests/_sandbox.py::sandbox()` 靠 chdir 生效。
_SETTINGS_PATH = Path("data/settings.json")

#: 会话级覆盖（③）。不落盘。`/clear` 清。
#: 会话级状态，已登记在 `core/state_registry.py`。
_session: dict[str, str] = {}
_lock = threading.Lock()

#: 坏值只喊一次（免得每条请求打一行）。
_bad_noted = False


def normalize(value) -> str | None:
    """把各种写法归一成 `"zh"` / `"en"`；认不出，`None`（不是猜一个）。"""
    if value is None:
        return None
    s = str(value).strip().lower().replace("_", "-")
    if not s:
        return None
    head = s.split("-")[0].split(".")[0]
    if head in ("zh", "cn", "chs", "chinese", "中文", "简体中文", "汉语"):
        return "zh"
    if head in ("en", "eng", "english", "英文", "英语"):
        return "en"
    return None


def _windows_locale() -> str:
    """`GetUserDefaultLocaleName` —— 本机 `'zh-CN'`。

    别用 `locale.getdefaultlocale()` —— 本机已报警告，
    Python 3.15 要删。
    """
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(85)          # LOCALE_NAME_MAX_LENGTH
        n = ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85)
        return buf.value if n > 0 else ""
    except Exception:                                    # noqa: BLE001
        return ""


def detect_env_language() -> str:
    """① 环境定型：系统语言是中文，`zh`，其余（含读不到 / `C` / `POSIX`），`en`。

    容器部署时系统语言是服务器的（多半 `C`），英文，正好对。
    """
    raw = ""
    if os.name == "nt":
        raw = _windows_locale()
    if not raw:
        for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
            raw = os.environ.get(key, "") or ""
            if raw:
                break
    lang = normalize(raw)
    return lang or "en"


def configured_language() -> str | None:
    """② 设置值。未设 / 认不出，`None`（= 跟随①）。

    认不出时喊一次（坏配置不等于"没设"，但这里不能抛 ——
    它在每条请求的提示词装配路径上）。
    """
    global _bad_noted
    try:
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return None
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or SETTING_KEY not in raw:
        return None
    val = raw.get(SETTING_KEY)
    if val is None or str(val).strip() == "":
        return None
    lang = normalize(val)
    if lang is None and not _bad_noted:
        _bad_noted = True
        print("[language] 设置里的 %s=%r 认不出 —— 按「跟随系统」处理"
              % (SETTING_KEY, val))
    return lang


def session_override(session_id: str) -> str | None:
    with _lock:
        return _session.get(str(session_id or ""))


def set_session(session_id: str, lang: str) -> None:
    """③ 本会话覆盖（`/language`）。不落盘。"""
    with _lock:
        _session[str(session_id or "")] = str(lang)


def clear_session(session_id: str) -> None:
    """`/clear` 用：回到 ②（读设置）或 ①（跟随系统）。"""
    with _lock:
        _session.pop(str(session_id or ""), None)


def current(session_id: str = "") -> str:
    """③ > ② > ①。"""
    return (session_override(session_id)
            or configured_language()
            or detect_env_language())


#: 那两句逐字（实际 +44 tokens）。
#: 加一门语言 = 加一行；别写成函数或模板。
_LINES: dict[str, str] = {
    "zh": "回答语言：简体中文。技术领域的术语与代码标识符保持原样；其余照常翻译。",
    "en": "回答语言：English. Technical terms and code identifiers stay as-is; "
          "translate everything else.",
}


def prompt_line(lang: str) -> str:
    """system prompt 里那一行（另起一行，不缝进「本机环境」那句）。"""
    return _LINES.get(lang, _LINES["zh"])
