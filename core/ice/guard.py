"""ICE 的门面对象 —— 接进 pipeline 的那个东西。

它只是一个薄壳：把规则、映射、开关三样收在一处，并把四个动词
（`detect` / `pseudonymize` / `restore` / `scan`）统一成"看开关"和"不看开关"两类。

哪几个动词看开关，是想清楚的：

    detect / pseudonymize  —— 看。保护关掉了就不该再换。
    restore / scan         —— 不看。句柄已经写出去了，它在别人手里
                              （模型正在搬、回复里正在出现）。这时候把开关关掉、
                              或者还没接上开关，都必须照常换回来 ——
                              否则用户看到的就是一串怪字符，而保护本身却没省下什么。

设置闸门在 `data/settings.json`；会话级降级见 `core.ice.reveal`；
会话级状态登记在 `core/state_registry.py`。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from core.ice.detect import detect_pii
from core.ice.handles import Handle
from core.ice.pseudonym import (
    PseudonymizeResult, RestoreResult, pseudonymize_text_ex, restore_text_ex,
    scan_residuals,
)
from core.ice.rules import builtin_rules
from core.ice.store import SessionStore, get_store
from core.ice.types import Finding, Residual, Rule


class ICEGuard:
    """信息侧的总入口。

    `enabled=False`，检测与替换都停（还原不停，见模块 docstring）。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        store: Optional[SessionStore] = None,
        rules: Optional[Sequence[Rule]] = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._store = store if store is not None else get_store()
        self._rules: Tuple[Rule, ...] = (
            tuple(rules) if rules is not None else builtin_rules()
        )

    # 读数
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def rules(self) -> Tuple[Rule, ...]:
        return self._rules

    @property
    def store(self) -> SessionStore:
        return self._store

    # 动词
    def detect(self, text: str) -> List[Finding]:
        """只看不换。开关关着，空列表。"""
        if not self._enabled:
            return []
        return detect_pii(text, rules=self._rules)

    def pseudonymize(self, text: str, session_id: str) -> Tuple[str, List[Handle]]:
        """换上模型的那一份。开关关着，原样返回。"""
        if not self._enabled:
            return text, []
        r = self.pseudonymize_ex(text, session_id)
        return r.text, list(r.handles)

    def pseudonymize_ex(self, text: str, session_id: str) -> PseudonymizeResult:
        if not self._enabled:
            return PseudonymizeResult(text)
        return pseudonymize_text_ex(text, session_id, store=self._store,
                                    rules=self._rules)

    def restore(self, text: str, session_id: str) -> str:
        """不看开关 —— 见模块 docstring。"""
        return self.restore_ex(text, session_id).text

    def restore_ex(self, text: str, session_id: str) -> RestoreResult:
        return restore_text_ex(text, session_id, store=self._store)

    def scan(self, text: str, session_id: str) -> Tuple[Residual, ...]:
        """出口扫描。也不看开关 —— 残留是既成事实，与保护开不开无关。"""
        return scan_residuals(text, session_id, store=self._store)

    def forget_session(self, session_id: str) -> None:
        self._store.forget_session(session_id)


# 进程级单例 —— 框架侧的接线点入口

_SETTINGS_PATH = Path("data/settings.json")

#: `data/settings.json` 里 `ice` 那一格认哪几个开关。唯一一份（`web/server.py`
#: 的 `/api/settings` 收哪些键，也读这里 —— 两处各写一份必然漂移）。
#:
#: `net_protection` 也在这张表里 —— 它不许成为"存得住、拨了没反应"的
#: 开关（标签不许说谎）；加进来就要渲染开关。
ICE_SWITCH_KEYS = ("enabled", "info_protection", "net_protection")

#: 「这个开关组全开」要满足哪几个键。两组，不是一组 ——
#: 总闸对所有层生效，但信息层与网络层互不依赖（关掉网络层不该把信息层带走）。
_INFO_KEYS = ("enabled", "info_protection")
_NET_KEYS = ("enabled", "net_protection")

#: 全局兜底：关。
#:
#: 默认关，不是默认开 ——
#: 理由是一条还没验的地基：PUA 句柄能不能过真上游的分词器。
#: 万一上游把不同 PUA 塌成同一个 UNK token，模型就分不清两个句柄、会抄错，
#: 而"抄错"在本模块的失败谱里是最坏那一档（写错值，不是漏保护）。
#:先接线、默认关；地经验过了把这一行翻成 `True`，一行的事。
ICE_DEFAULT_ON = False


def _switches_all_on(keys) -> bool:
    """`keys` 里那几个开关全为真吗。读不到一律回兜底（见调用方的 docstring）。"""
    try:
        if not _SETTINGS_PATH.exists():
            return ICE_DEFAULT_ON
        raw = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ICE_DEFAULT_ON
    if not isinstance(raw, dict):
        return ICE_DEFAULT_ON
    ice = raw.get("ice")
    if not isinstance(ice, dict):
        return ICE_DEFAULT_ON          # 连 `ice` 这一格都没有，兜底

    for key in keys:
        val = ice.get(key, ICE_DEFAULT_ON)
        if not isinstance(val, bool) or not val:
            return False
    return True


def _info_protection_enabled() -> bool:
    """ICE 信息层此刻生效吗 = 总闸 且 信息层（两个都为真才算）。

    判据（同 `core/tiers.py::configured_level`）：
    读不到，兜底（没有文件 / 缺键 / 坏 JSON / 非 bool 值，一律回 `ICE_DEFAULT_ON`）；
    绝不因为设置文件坏了就让一轮对话失败。
    只收真 bool —— `"true"` / `1` 都不算（`isinstance(True, int)` 是 True 那个坑）。
    """
    return _switches_all_on(_INFO_KEYS)


def net_protection_enabled() -> bool:
    """ICE 网络层此刻生效吗 = 总闸 且 网络层。

    每次现读（不像信息层那样只在构造/`reload_enabled` 时读）：网络层只在
    出站那一刻被问到（`web_fetch` / `send_email`），一天也问不了几次，
    而"用户在设置页关掉它、下一个请求就该生效"是更值钱的性质（同 `core/allowlist.py` 判据 ①）。
    """
    return _switches_all_on(_NET_KEYS)


_DEFAULT_GUARD = ICEGuard(enabled=_info_protection_enabled())


def get_guard() -> ICEGuard:
    """框架侧的唯一入口。"""
    return _DEFAULT_GUARD


def reload_enabled() -> bool:
    """重读设置，就地更新那个单例。返回新的状态。

    存在的理由：`_DEFAULT_GUARD` 是构造时读一次 —— 用户改了设置页不重启的话，
    得有个地方能把新值推进去。是不是要好，是这个函数的名字说了算（不是每次请求都读盘）。
    """
    _DEFAULT_GUARD._enabled = _info_protection_enabled()
    return _DEFAULT_GUARD.enabled
