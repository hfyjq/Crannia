"""ICE —— 本地安全模块的信息侧（PII 检测 + 可逆伪名化）。

本包是纯函数 + 单测；接线在 :mod:`core.ice.guard` 与 :mod:`core.ice.boundary`。

═══════════════════════════════════════════════════════════════════════
三条不变量（改动前先读这三条；每条都有机械读者）
═══════════════════════════════════════════════════════════════════════

① 上模型的那一份文本里出现的每一个句柄，都必须是本模块为这个会话刚写入的。

    落地：伪名化的第一步是入境中和（:func:`core.ice.handles.strip_handles_reporting`）
    —— 先把"别人给我们的文本"里的句柄形状全剥掉，再在干净串上检测。
    没有这一步，一段网页就能写一个 `【PII:1】` 进去，让模型把它当句柄搬进工具参数，
    出口还原时把真身份证填进去（这是一条真实的攻击面）。
    判据是「形状对不对」而不是「在不在映射里」—— 下标是连号、猜都不用猜，
    只剥已知的那条路等于给攻击者开一扇门。

② 落盘全是真值。

    落地：句柄只活在"上模型的那一份"。替换是变换副本，
    绝不写回 `_format_tool_result` / 会话库 / 记忆 / 笔记 / 日报。
    映射是会话级的，会话一过就失效 —— 落盘存句柄 = 留下一堆解不开的怪字符。

③ 只有高置信才替换。

    落地：:func:`core.ice.rules.is_replaceable` —— 声明了 `supports_encryption`
    且没有 `filter` 且校验位真的通过。同一个谓词在 `detect` 与
    `pseudonym` 两处调用（防"一个判定两处漂移"）。
    依据：检测可以错（代价一行噪声），打码不能错（代价用户数据被改坏）。

═══════════════════════════════════════════════════════════════════════
两条硬约束
═══════════════════════════════════════════════════════════════════════

* 不许有"模块级且被写过"的容器 —— census
  扫 `core/` 全树，那种形状当场红。缓存走 `lru_cache`，状态装在实例属性里。
* 句柄形态换过一版，GBK 那条约束因此解除了：PUA 版编不出 GBK，得处处 `chr()` 生成、
  还得额外维护一个 `display` 形态；现在句柄本身就是 【PII:N】（GBK 安全），
  句柄 == 显示形态，两个麻烦一起没了。原因见 :mod:`core.ice.handles` 的 docstring。

═══════════════════════════════════════════════════════════════════════
不做什么（是范围，不是遗漏）
═══════════════════════════════════════════════════════════════════════

* 不用 Presidio。它要拖 15+ 个包（spacy 是导入期强制的 14.5 MB 编译包），
  而它自带的中文规则是零（19 个国家/地区里没有中国），三条规则只白送"银行卡 Luhn"
  一条 —— 我们自己写只花 15 行。借它的判据，不借它的依赖。
* 不做自由文本 NER（姓名/地址那类）—— 零模型是本包的一个前提。
* 不保护用户自己打进对话的 PII（范围决定）—— 那是用户已经选择交出去的，
  且不替换才能让"帮我算算这个身份证"这类推理任务照常可用。
* 规则只做结构化，且每条要么有校验位、要么只报不改（手机号属于后者）。
"""

from __future__ import annotations

from core.ice.detect import detect_pii, detect_summary
from core.ice.guard import (
    ICE_DEFAULT_ON, ICE_SWITCH_KEYS, ICEGuard, get_guard, net_protection_enabled,
    reload_enabled,
)
from core.ice.canary import (
    CANARY_DOMAIN, CANARY_STRING, CANARY_VALUES,
    block_result, check_outbound, find_hits, note_trip,
)
from core.ice.net_heuristics import Signal, assess, note_signals
from core.ice.boundary import (
    pseudonymize_block, pseudonymize_messages, restore_json_arguments,
    restore_response, restore_tool_args, restore_tool_call_arguments,
    restore_value,
)
from core.ice.handles import (
    HANDLE_PREFIX, HANDLE_RE, HANDLE_SUFFIX, Handle, display_of, handle_text,
    is_handle, iter_handles, scan_variants, strip_handles_reporting,
)
from core.ice.pseudonym import (
    PseudonymizeResult, RestoreResult, pseudonymize_text, pseudonymize_text_ex,
    restore_text, restore_text_ex, scan_residuals,
)
from core.ice.rules import (
    CN_MOBILE_PREFIXES, IceRuleError, VALIDATORS, builtin_path, builtin_rules,
    cn_id_ok, cn_mobile_ok, is_replaceable, load_rules, luhn_ok, rules_from_toml,
    validate,
)
from core.ice.store import (
    MAX_HANDLES_PER_SESSION, MAX_SESSIONS, SessionMap, SessionStore,
    forget_all, forget_session, get_store,
)
from core.ice.types import Confidence, Finding, Residual, Rule

__all__ = [
    # types
    "Confidence", "Finding", "Residual", "Rule",
    # handles
    "Handle", "HANDLE_PREFIX", "HANDLE_SUFFIX", "HANDLE_RE",
    "handle_text", "is_handle", "iter_handles", "display_of",
    "strip_handles_reporting", "scan_variants",
    # rules
    "IceRuleError", "VALIDATORS", "CN_MOBILE_PREFIXES",
    "rules_from_toml", "load_rules", "builtin_rules", "builtin_path",
    "validate", "is_replaceable", "luhn_ok", "cn_id_ok", "cn_mobile_ok",
    # detect
    "detect_pii", "detect_summary",
    # store
    "SessionMap", "SessionStore", "get_store", "forget_session", "forget_all",
    "MAX_SESSIONS", "MAX_HANDLES_PER_SESSION",
    # pseudonym
    "pseudonymize_text", "pseudonymize_text_ex",
    "restore_text", "restore_text_ex", "scan_residuals",
    "PseudonymizeResult", "RestoreResult",
    # guard
    "ICEGuard", "ICE_SWITCH_KEYS", "ICE_DEFAULT_ON", "get_guard", "reload_enabled",
    "net_protection_enabled",
    # canary（网络侧）
    "CANARY_DOMAIN", "CANARY_STRING", "CANARY_VALUES",
    "find_hits", "check_outbound", "block_result", "note_trip",
    # net_heuristics（网络侧弱信号）—— 只报不拦（那才叫弱信号）
    "Signal", "assess", "note_signals",
    # boundary
    "pseudonymize_messages", "pseudonymize_block",
    "restore_tool_args", "restore_value", "restore_json_arguments",
    "restore_tool_call_arguments", "restore_response",
]
