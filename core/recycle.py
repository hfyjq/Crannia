"""把文件送进 Windows 系统回收站。

## 它是什么，不是什么

是什么：一个薄壳，`ctypes` 直接调 `shell32!SHFileOperationW` +
`FOF_ALLOWUNDO` —— 零依赖。用户在资源管理器里自己还原，我们零新界面。

不是什么：它不是一道闸。 `run_command` 的 `rm -rf` 照旧直接删，
「bash 的写权限不受 `validate_path` 约束」对回收站同样成立。
它管住的只有框架自己的用户文件删除（`skills/file_parser/handler.py`
的 `delete_files` 一条路）。别读成"删除安全了"。

## 三个判据（每一条都对应一个失败模式）

### ① `FOF_ALLOWUNDO` 漏了 = 永久删

这个 API 的默认是不允许撤销的，「可还原」这件事完全押在那个 bit 上。
旗子只在一处拼（`RECYCLE_FLAGS`），并且有一条断言钉着它在里面。
（别让"防什么"散在代码里靠人记。）

### ② 一次一把删是全有或全无

本机：把 `[已不存在的文件, 好文件]` 一起交给
`SHFileOperationW`，`rc=2`，而那个好文件原封不动。
要逐条调（每次一个路径），才谈得上"哪一条失败"。
逐条还有一个好处：错误归属是精确的，不用去猜整批里是谁坏。

### ③ 「进去了」与「删得回来」要分开说

超过回收站配额时，Windows 会静默地降级成永久删 —— 那是有损的。
判据是 `SHQueryRecycleBinW` 前后数条目数：删成功了而条目数没涨
那一个没进回收站，出声。
[!] 读不到就是读不到（读不到 ≠ 0）—— 查询失败一律 `None`，
而 `None` 不许被当成"涨了 0 个"（那看起来像一个结论）。
那一条路的措辞是「无法确认能不能还原」，不是「不能还原」。

## 一个故意不用的旗

`FOF_WANTNUKEWARNING` 不设。它会在"装不进回收站"时弹一个确认框，
而且压过 `FOF_NOCONFIRMATION` —— 框架是无头跑的，一个模态框会把整轮卡死
（比删错更糟：用户那边什么都看不见）。
判据：它要说的那件事（"这个没能进回收站"）我们自己算得出来（判据 ③）
拿一条会挂起的 UI 去换一个我们已经有的结论是纯亏。
降级要出声 —— 但出声的地点是工具回执，不是对话框。
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Win32 常量
FO_DELETE = 0x0003

FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400

#: 送进回收站时用的那一组旗 —— 全仓只此一处（有断言钉着 `FOF_ALLOWUNDO`）。
#:
#: [!] `FOF_ALLOWUNDO` 漏了 = 永久删（见模块 docstring 判据 ①）；
#: 其余三个都是"别弹窗"（无头框架里一个模态框 = 整轮卡死）。
RECYCLE_FLAGS = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT)


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """`SHFILEOPSTRUCTW` —— 默认对齐，没有 `#pragma pack`。

    [!] x64 上尺寸必须是 56；`fFlags` 是 `FILEOP_FLAGS`（`WORD`，16 位），
    写成 `c_uint` 会让后面每个字段整体错位 4 字节而调用照样返回 0。
有一条断言钉着 `sizeof`（本机：56）。
    """
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


class _SHQUERYRBINFO(ctypes.Structure):
    """`SHQUERYRBINFO` —— x64 上 24 字节（`DWORD` + 4 字节填充 + 两个 `int64`）。"""
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


_shell32 = None
_load_error = ""


def _lib():
    """拿 `shell32` 并装好签名；拿不到返回 `None`（原因记在 `_load_error`）。"""
    global _shell32, _load_error
    if _shell32 is not None:
        return _shell32
    if sys.platform != "win32":
        _load_error = "非 Windows 平台，没有系统回收站"
        return None
    try:
        lib = ctypes.WinDLL("shell32", use_last_error=True)
        lib.SHFileOperationW.argtypes = [ctypes.POINTER(_SHFILEOPSTRUCTW)]
        lib.SHFileOperationW.restype = ctypes.c_int
        lib.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p,
                                           ctypes.POINTER(_SHQUERYRBINFO)]
        lib.SHQueryRecycleBinW.restype = ctypes.c_long
        _shell32 = lib
        return lib
    except Exception as exc:  # noqa: BLE001
        _load_error = "%s: %s" % (type(exc).__name__, exc)
        return None


def available() -> bool:
    """这台机器上能不能用系统回收站。"""
    return _lib() is not None


def unavailable_reason() -> str:
    """为什么用不了 —— 给回执用的一句话（拿不到原因时给空串）。"""
    _lib()
    return _load_error


# 回收站条目数（判据 ③：进出前后比一比）

def bin_item_count(root: str) -> int | None:
    """*root*（盘根，如 `"C:\\\\"`）那个回收站里有多少条目。

    [!] 读不到返回 `None`，不是 `0`（读不到 ≠ 0）——
    调用方不许把 `None` 当成"一个都没进"。
    """
    lib = _lib()
    if lib is None:
        return None
    try:
        info = _SHQUERYRBINFO()
        info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
        rc = lib.SHQueryRecycleBinW(str(root), ctypes.byref(info))
        return None if rc != 0 else int(info.i64NumItems)
    except Exception:  # noqa: BLE001
        return None


# 送进去

def _send_one(lib, path: str) -> tuple[bool, str]:
    """把一个路径交给 `SHFileOperationW`。返回 (成功?, 失败原因)。

    [!] `pFrom` 要的是双 NUL 结尾的宽字符串。ctypes 从 `str` 造 `c_wchar_p`
    时会自己再补一个结尾，我们在 Python 侧多写一个 `"\\x00"`，
    内存里就正好是 `…path…\\x00\\x00`（本机可用）。
    """
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = str(path) + "\x00"
    op.pTo = None
    op.fFlags = RECYCLE_FLAGS
    op.hNameMappings = None
    op.lpszProgressTitle = None
    try:
        rc = lib.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)
    if rc != 0:
        err = ctypes.get_last_error()
        return False, "SHFileOperationW rc=%d%s" % (
            rc, (" ErrCode=%d" % err) if err else "")
    if op.fAnyOperationsAborted:
        return False, "用户/系统中止了这次操作"
    return True, ""


@dataclass
class RecycleOutcome:
    """一次"送进回收站"的结果 —— 三样都要有，缺一样就说不清。"""
    #: 走了回收站的（可能可还原 —— 见 `bin_delta`）
    moved: list = field(default_factory=list)
    #: 没成（`"路径: 原因"`）
    failed: list = field(default_factory=list)
    #: 回收站条目数的净增；`None` = 读不到（不是 0）
    bin_delta: int | None = None
    #: 路径根本没有可用的回收站（平台不支持 / shell32 拉不起来）
    unavailable: bool = False
    #: 为什么用不了（给回执）
    reason: str = ""

    @property
    def degraded(self) -> bool:
        """有没有"删了但删不回来"的 —— 这是这条能力唯一的失败形态。

        * 路径根本不可用，降级（走的是永久删）
        * 条目数读得到且小于送进去的个数，有东西没进回收站，降级
        * 条目数读不到，`True`（无法确认 —— 出声，不许默认它是好的）
        """
        if self.unavailable:
            return True
        if not self.moved:
            return False
        if self.bin_delta is None:
            return True
        return self.bin_delta < len(self.moved)


def send_to_recycle_bin(paths: Iterable[str | Path]) -> RecycleOutcome:
    """把 *paths* 逐个送进系统回收站。逐条调（见模块 docstring 判据 ②）。

    [!] 这个函数只负责送：送不成的它不动（不替调用方决定要不要硬删）。
    "送不进就退回首删"是调用方的策略，而且必须出声。
    """
    items = [str(p) for p in paths]
    out = RecycleOutcome()
    lib = _lib()
    if lib is None:
        out.unavailable = True
        out.reason = _load_error or "系统回收站不可用"
        out.failed = ["%s: %s" % (p, out.reason) for p in items]
        return out

    before = bin_item_count(_drive_root(items[0])) if items else None
    for p in items:
        ok, why = _send_one(lib, p)
        if ok:
            out.moved.append(p)
        else:
            out.failed.append("%s: %s" % (p, why))
    if out.moved:
        after = bin_item_count(_drive_root(out.moved[0]))
        if before is not None and after is not None:
            out.bin_delta = after - before
    return out


def _drive_root(path: str) -> str:
    """`C:\\a\\b` → `C:\\\\`；取不到盘符就退到进程所在盘。"""
    try:
        d = Path(path).drive
        if d:
            return d + "\\"
    except Exception:  # noqa: BLE001
        pass
    try:
        return Path.cwd().drive + "\\"
    except Exception:  # noqa: BLE001
        return ""


def explain(outcome: RecycleOutcome, *, permanent_ok: int = 0) -> str:
    """把 *outcome* 翻成一句给模型看的话 —— 出声的地方在这里，不在对话框。

    *permanent_ok* 是调用方退回永久删并且成功的个数（降级那条路）。

    [!] `permanent_ok` 一定要单独说，不许被"回收站那边看起来正常"盖掉。
    否则会出现：2 个文件里 1 个进了回收站、1 个被退回永久删，而回执只说
    「已把 1 个文件送进系统回收站（可还原）」—— 那一个永久删掉的一个字都没提。
    这正是「删了 ≠ 删得回来」要防的形状：
    有损的那一半被无声地吞掉了。两件事分开成句，各说各的。
    """
    n = len(outcome.moved)
    if outcome.unavailable:
        if permanent_ok:
            return ("[!] 这台机器上**没有可用的系统回收站**（%s）—— "
                    "%d 个文件是**永久删除**的，**还原不了**。"
                    % (outcome.reason or "原因未知", permanent_ok))
        return "[!] 系统回收站不可用（%s），什么都没删。" % (outcome.reason or "原因未知")

    parts: list[str] = []
    if n and outcome.bin_delta is None:
        parts.append("已把 %d 个文件送进系统回收站，但**读不到回收站的条目数**，"
                     "所以**无法确认**它们能不能还原。" % n)
    elif n and outcome.bin_delta < n:
        parts.append("[!] 送进去 %d 个，而回收站条目只多了 %d 个 —— "
                     "**有 %d 个没能进回收站**（多半是回收站配额满了），"
                     "那些是**永久删除**，**还原不了**。"
                     % (n, outcome.bin_delta, n - outcome.bin_delta))
    elif n:
        parts.append("已把 %d 个文件送进**系统回收站**（可从资源管理器还原）。" % n)
    else:
        parts.append("没有文件被送进回收站。")

    if permanent_ok:
        parts.append("[!] 另有 %d 个是**永久删除**的（回收站这边没能接手），"
                     "**还原不了**。" % permanent_ok)
    return "".join(parts)
