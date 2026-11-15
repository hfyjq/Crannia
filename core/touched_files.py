"""本轮改动了哪些文件 —— UI 那个「查看改动文件」下拉框的数据源。

## 数据从哪来：一处钩子，不是一张工具名单

要问"这一轮改了哪些文件"，最直觉的写法是列一张写工具的名单
（`write_file` / `edit_file` / office 写族 / `delete_files` / `deliver_file`）——
那是一张会烂的名单（新加一个写工具，没人会想起来往这儿补一行）。

改用唯一一处：`SecurityGuard.validate_path(path, required_op)`。
它是每一个碰路径的工具都要过的那道门，而 `required_op` 已经在区分读写了
（`read` / `copy` / `write` / `delete` / `move` / `rename`）
写类的那几个 op 就是"这是要改它"的判据，不用我们另立一套。

## 判据：只算我们自己的写

常见做法有两种：只算自己的 Write/Edit（明确忽略 shell 改的）·
按 git 工作树 diff 算（唯一"算 shell"的那一种）。我们取前者。
为什么不按 git 工作树 diff 算 —— 两条理由：
  ① 我们的写不止文本工具（office 写族也是），而 **git diff 对 docx/xlsx 只报
     binary**、给不出行数；
  ② git diff 会把"不是 agent 改的"算进来 —— workspace 可能挂着 watcher
     （例如微信目录往里同步），外部同步会被算成"本轮改动"。

## 一处如实标的边界：记的是"验证过写权限"，不是"写成功了"

`validate_path` 跑在真正落盘之前，后续那一步失败（磁盘满 / 权限 / 工具自己抛）
的话，这个文件仍然在表里。这是有意的取舍：
   · 反过来的做法（等工具成功返回再记）要给每个写工具再挂一次钩子 ——
     那就是上面说的那张会烂的名单；
   · 而"验证过、但没写成"是罕见的，代价是列表里偶尔多一个文件；
   · 代价写在界面上（悬停那句），不假装它是"已确认落盘"。
回执不许比事实更自信 —— 所以措辞是「本轮改过」，
  不是「本轮写成功」。

本模块是会话级状态，去会话级状态登记表登记
（`/clear` 清；不登记 census 读者当场红）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List

#: 写类 op —— 只有这几个算"这是要改它"。
#: 名单不许从 `validate_path` 的 docstring 里抄第二份：那六个 op 的定义在
#: `core/security.py::validate_path`（`read` / `copy` / `write` / `delete` /
#: `move` / `rename`），这里只挑写类的四个。
WRITE_OPS = frozenset({"write", "delete", "move", "rename"})

#: 会话级：`session_id -> [路径, ...]`，保序 + 去重（保序 = 用户按发生顺序读）。
#:
#: 行数（界面的 `+N/-M`）不在这里记：那一格由轮末拿留档算
#: （`core.file_history.diff_for`），与点开看到的那份 diff 同源 ——
#: 各算一处的话，列表上的 `+3/-1` 与浮窗里的内容会对不上。
_touched: Dict[str, List[str]] = {}

#: 一条轮里最多记这么多（超了就不再记；不是"截断"，是不再往里加）。
#: 判据同 `context_report._MAX_SESSIONS`：进程级的表必须有界，否则会长到没人看。
_MAX_PER_TURN = 100

#: 多少个会话的表 —— 六个通道共用一个池、进程活着就一直攒。
_MAX_SESSIONS = 64

_LOCK = threading.Lock()


def note(session_id: str, path) -> bool:
    """记下"这一轮碰过这个文件"。永不抛 —— 它在 `validate_path` 的热路径上。

    返回 `True` = 这一轮第一次记到它（调用方用它决定"要不要留改前内容"：
    一个文件一轮只留一份"改前"，第二次改它就不该再留 —— 那会覆盖掉真正的改前）。

    去重是按保存的那份字符串（`str(Path(...).resolve())` 的 POSIX 形），
    不是按原样入参 —— 否则 `a.docx` 与 `./a.docx` 会算两个。
    返回值是在锁里算出来的（不是在锁外再查一次 —— 那会是一个 TOCTOU）。
    """
    try:
        key = str(session_id or "")
        try:
            shown = Path(str(path)).resolve().as_posix()
        except Exception:                       # noqa: BLE001 —— 路径怪就原样
            shown = str(path)
        with _LOCK:
            bucket = _touched.get(key)
            if bucket is None:
                if len(_touched) >= _MAX_SESSIONS:
                    _touched.pop(next(iter(_touched)))
                bucket = _touched[key] = []
            if shown in bucket:
                return False
            if len(bucket) >= _MAX_PER_TURN:
                return False
            bucket.append(shown)
            return True
    except Exception as exc:  # noqa: BLE001
        print("[touched] 记录失败（不影响写入）：%s: %s" % (type(exc).__name__, exc))
        return False


def peek(session_id: str) -> List[str]:
    """只看不取（调试 / 断言用）。"""
    with _LOCK:
        return list(_touched.get(session_id or "", ()))


def drain(session_id: str) -> List[str]:
    """取走并清空，轮末发事件时用。返回路径列表。

    取走（不是 peek）的理由：这一轮的事件只该带这一轮的文件；
    留在表里的话，下一轮会把上一轮的也算进去（报告的数把之前的也算成"这次的"）。
    """
    with _LOCK:
        return _touched.pop(session_id or "", [])


def take(session_id: str) -> List[str]:
    """旧名字，与 `drain` 同一个入口。"""
    return drain(session_id)


def forget(session_id: str) -> None:
    """`/clear` 的清入口（由会话级状态登记表的 `purge_session` 走）。"""
    with _LOCK:
        _touched.pop(session_id or "", None)


def is_downloadable(path: str) -> bool:
    """这个路径能不能给前端一个 `/api/download` 链接。

    判据点名要跟 `/api/download` 自己那条边界一致 —— 它只放
    `workspace/` 底下的文件（那是它唯一的检查，见 `web/server.py`）。
    判反了的后果是一个点开就 403 的链接 —— 那比不给链接更坏
    （用户以为坏了）。所以这里宁窄不宽：拿不准就返回 False、当纯文本显示。
    """
    try:
        ws = Path("workspace").resolve()
        p = Path(path)
        return p == ws or ws in p.parents
    except Exception:  # noqa: BLE001
        return False
