"""伪名化与还原 —— 把「读来的」PII 换成句柄，再在边界换回来。

顺序是承重的（:func:`pseudonymize_text_ex`）：

    ① 入境中和（先剥掉所有 PUA）→ ② 在干净串上检测 → ③ 逐个分配句柄并替换

第 ① 步必须在最前面：检测是在"别人给我们的文本"上做的，而那段文本可以是恶意的。
先剥再测，保证「上模型的那一份里出现的每一个 PUA，都是本模块刚写的」这条不变量
不依赖任何输入方的善意。

还原只认本会话的映射。未登记的 PUA 一律丢弃 + 报告，绝不"猜"。
猜的代价是把真值注入到一个别人写的位置（"还原失败可以宽容，注入必须严谨"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from core.ice.detect import detect_pii
from core.ice.handles import (
    HANDLE_RE, Handle, iter_handles, scan_variants, strip_handles_reporting,
)
from core.ice.rules import builtin_rules, is_replaceable
from core.ice.store import SessionStore, get_store
from core.ice.types import Finding, Residual, Rule


@dataclass(frozen=True)
class PseudonymizeResult:
    """伪名化的全部读数。`text` 是上模型的那一份（真值已换成句柄）。"""

    text: str
    #: 这一轮新写进文本的句柄（同值复用，所以可能短于 findings）
    handles: Tuple[Handle, ...] = ()
    #: 全部命中（含只报不改的那些）
    findings: Tuple[Finding, ...] = ()
    #: 入境被剥掉的 PUA 总数
    neutralized: int = 0
    #: 其中恰好等于本会话已发句柄的那些码点 —— 伪造或上一轮残留的信号
    ingress_handles: Tuple[int, ...] = ()
    #: 过了闸、本该替换的条数（对比 `len(handles)` 就是"生不出句柄"的条数）
    replaceable_findings: int = 0
    #: 句柄配额耗尽，有值没换（生不出就不换）
    exhausted: bool = False

    @property
    def report(self) -> str:
        """一行可进日志的读数。只有数，没有值。"""
        return ("pseudonymized=%d findings=%d neutralized=%d ingress=%d"
                % (len(self.handles), len(self.findings), self.neutralized,
                   len(self.ingress_handles)))


@dataclass(frozen=True)
class RestoreResult:
    """还原的读数。"""

    text: str
    restored: int = 0
    #: 未登记的 PUA 被丢掉几个（它们没有变回真值）
    unknown_pua_removed: int = 0


def pseudonymize_text_ex(
    text: str,
    session_id: str,
    *,
    store: Optional[SessionStore] = None,
    rules: Optional[Sequence[Rule]] = None,
) -> PseudonymizeResult:
    """完整版：换句柄，并把这一轮发生了什么全部报出来。"""
    if not text:
        return PseudonymizeResult(text)

    if store is None:
        store = get_store()
    smap = store.session(session_id)

    # ① 入境中和 —— 见模块 docstring。`known` 是"我们自己发过的句柄串"。
    clean, neutralized, ingress = strip_handles_reporting(text, smap.known_tokens())

    # ② 在干净串上检测（下标因此与 clean 对齐）
    if rules is None:
        rules = builtin_rules()
    by_id = {r.id: r for r in rules}
    findings = detect_pii(clean, rules=rules)

    # ③ 逐个替换
    #
    # 本会话本轮放行过的那几个真值，跳过不换。
    #    这是"显式放行"唯一落地的地方 —— 放行表由 `core/ice/reveal.py` 管，
    #    只活一轮（轮首清）。放在这里而不是 `boundary`，是因为这里才是
    #    "唯一把值换成句柄的地方"（账只允许一个仪器）。
    #    空表是常态，一次 `if` 省下；`exempt` 取的是快照（下面还会改它吗？不会）。
    from core.ice import reveal as _reveal
    exempt = _reveal.exempt_values(session_id)

    out: List[str] = []
    handles: List[Handle] = []
    pos = 0
    replaceable_n = 0
    exhausted = False
    for f in findings:
        if f.value in exempt:
            continue                      # 用户点了放行，这一轮原样留着
        rule = by_id.get(f.rule_id)
        if rule is None:
            continue
        # 闸的第二道执行点（第一道在 detect 里写 Finding.replaceable）。
        #   同一个谓词，不是第二套逻辑 —— 这里是真正分配句柄的地方，
        #   所以它才是"能不能替换"的最终裁决。
        if not is_replaceable(rule, f.confidence):
            continue
        replaceable_n += 1
        h = smap.handle_for(f.value, f.rule_id)
        if h is None:
            exhausted = True
            continue                      # 生不出句柄，这个值原样留着
        out.append(clean[pos:f.start])
        out.append(h.text)
        pos = f.end
        handles.append(h)
    out.append(clean[pos:])

    return PseudonymizeResult(
        text="".join(out), handles=tuple(handles), findings=tuple(findings),
        neutralized=neutralized, ingress_handles=ingress,
        replaceable_findings=replaceable_n, exhausted=exhausted,
    )


def pseudonymize_text(
    text: str,
    session_id: str,
    *,
    store: Optional[SessionStore] = None,
    rules: Optional[Sequence[Rule]] = None,
) -> Tuple[str, List[Handle]]:
    """简版：(上模型的那一份, 这一轮写进去的句柄)。"""
    r = pseudonymize_text_ex(text, session_id, store=store, rules=rules)
    return r.text, list(r.handles)


def restore_text_ex(
    text: str,
    session_id: str,
    *,
    store: Optional[SessionStore] = None,
) -> RestoreResult:
    """完整版还原。只认本会话的映射。"""
    if not text:
        return RestoreResult(text)

    if store is None:
        store = get_store()
    smap = store.peek(session_id)
    if smap is None:
        # 没有映射，一个也不猜。所有句柄形状丢掉（用户不该看到句柄），
        # 但它们不会变成真值 —— 没有映射就没有真值可给。
        clean, removed, _ = strip_handles_reporting(text, ())
        return RestoreResult(clean, 0, removed)

    store.touch(session_id)
    out: List[str] = []
    restored = 0
    removed = 0
    pos = 0
    for m in iter_handles(text):
        token = m.group(0)
        value = smap.value_for(token)
        out.append(text[pos:m.start()])
        pos = m.end()
        if value is None:
            removed += 1        # 未登记/编造的句柄：丢弃 + 计数，绝不猜
            continue
        out.append(value)
        restored += 1
    out.append(text[pos:])
    return RestoreResult("".join(out), restored, removed)


def restore_text(
    text: str,
    session_id: str,
    *,
    store: Optional[SessionStore] = None,
) -> str:
    """简版还原。"""
    return restore_text_ex(text, session_id, store=store).text


def scan_residuals(
    text: str,
    session_id: str,
    *,
    store: Optional[SessionStore] = None,
) -> Tuple[Residual, ...]:
    """出口扫描：这段文本里还剩什么不该在的东西。

    三类：`foreign_pua`（不属于本会话的 PUA）/ `text_escape`（句柄被写成转义）/
    `replacement_char`（有损解码痕迹）。

    它是确定性的 —— 不猜、不推断，只回答"这些东西在不在"。
    """
    if not text:
        return ()

    if store is None:
        store = get_store()
    smap = store.peek(session_id)
    known_idx: Tuple[int, ...] = ()
    if smap is not None:
        known_idx = smap.known_indices()
    return scan_variants(text, known_idx)
