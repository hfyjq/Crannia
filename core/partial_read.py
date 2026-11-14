"""会话级记录：这一次会话里读过、但没读全的那一次。

挡的是这个失败形状：读了半块，拿它下结论，写进交付物。read_excel_data 当场会提示
“范围之外还有什么”（_outside_range_note），而写侧一个字都不知道。这一层把两侧接上：
把“只读了一半”从一句话升级成判据，写侧才拦得住。

判据窄，不许放宽：只有 has_more is True 才记。不拿 _outside_range_note 的命中当
判据，它四向都报，“左边那片多半是行名”这类几乎每次都命中，拿它当开关等于每份产出
都挂一句噪声，而噪声会训练人和模型忽略这一栏。

会话级、不消费，sticky 到 /clear：记的是最近一次没读全的读（覆盖写，只留一条，
要的是“有事发生”而不是账本）；写完不弹，模型写第二份时仍该被提醒；/clear 时清，
登记在会话级状态登记表里。
"""

from __future__ import annotations

# session_id 到那一句人话（哪个文件、哪张表、只读到哪、实际多大）。
_pending: dict = {}


def note(session_id: str, what: str) -> None:
    """记下这次会话里有一次没读全的读。覆盖写，只留最近一条。"""
    if not what:
        return
    _pending[session_id or ""] = str(what)


def pending(session_id: str) -> str:
    """取那一句；没有就是空串。

    空串表示没有，不是“有但为空”。调用方判 if pending(...)，两者在这里恰好等价，
    但不要把它塌成别的哨兵值。
    """
    return _pending.get(session_id or "", "")


def forget(session_id: str) -> None:
    """/clear 的清理入口，由 core/state_registry.py::purge_session 调用。"""
    _pending.pop(session_id or "", None)


# 写侧（create_document / write_excel_data）挂上去的那一句，只此一份。
_WARNING = (
    "⚠️ 本次会话里有一次 Excel 读**没读全**（%s）—— 这份产出里若有数来自那次读，"
    "**先把剩下的读完再交**（用那条回执里的 `next_start_cell` / `next_end_cell`），"
    "别对没拿到的那些行 / 列下任何结论。"
)


def warning(session_id: str) -> str:
    """给写侧用的那一句；没有没读全的读就返回空串（调用方判 if w:）。"""
    p = pending(session_id)
    return (_WARNING % p) if p else ""


def warning_now() -> str:
    """warning() 的当前请求版，写侧的调用点一律走这一个。

    写侧有两处（create_document / write_excel_data），各写一段“取会话号 + 取那一句”
    的适配就会变成两处定义，必然漂移。会话号取不到时返回空串，取舍与
    current_session_id() 相同。
    """
    from core.request_snapshot import current_session_id
    return warning(current_session_id())
