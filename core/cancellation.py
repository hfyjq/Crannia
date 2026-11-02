"""按会话的「中断本轮」标志。

为什么需要它
------------
主循环没有轮次上限，靠的是一组刹车：用户中断、压缩检查、断路器。
本模块提供用户中断这一道 —— `/stop` 让当前这一轮在下一个安全点停下，
而不是杀进程。

边界
-------
`provider.chat()` 是同步阻塞的（最坏 3×120s，见 `providers/openai_compat.py`
的重试循环）。因此这个标志不能打断正在飞的那次请求 —— 它只保证
下一次迭代开始前停下来。要硬杀得引入可取消的 HTTP 客户端 + 把整条链改成
async，那是另一个量级的改动。

谁能真的用它打断
----------------
* Web：✅ 唯一能用的入口。waitress `threads=12`，`POST /api/stop` 在**另一个
  线程**里跑，而 agent 循环在请求线程里跑，标志设得上、读得到。
* IM（飞书/钉钉/Telegram/微信）：❌ 打不断一轮。整轮管线是同步无 await
  的，单事件循环里第二个消息进不来，`/stop` 要等这一轮跑完才被处理。
* 终端 CLI：❌ 输入被管线阻塞，仍然是 Ctrl+C。

生命周期（清在轮末，不在轮首）
--------------------------------
* 设置：`/stop` 命令（`pre_process`）与 Web 的 `POST /api/stop`。
* 消费：`core/agent_loop.py` 每轮迭代开头 + 每个工具派发前读一次，读到就清。
* 兜底清除：`PipelineScheduler` 在这一轮的 `finally` 里清一次 ——
  覆盖"设得太晚、循环没读到"的情况。
  清在轮首是错的：标志没有"轮次身份"，下一个开局的轮次会无条件
  抹掉刚设上、还没被读到的标志，中断静默失效。
* 空闲时被设上（没有轮次在跑时点了按钮）：它会留到下一次有轮次开始时被读到 ——
  那一轮会停下并告知用户。这是安全的，但不是"精准的"。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

_cancelled: set[str] = set()
_lock = threading.Lock()

# 正在等待的子进程也登记进中断表
#
# `/stop` 若只置标志，已经在跑的工具会一路跑完（一条五分钟的 `pip install`
# 就要再等五分钟）。进程内的同步 Python 工具杀不掉线程，登记也没用；
# 只有 `run_command` 是子进程，`core/shell.py::_kill_tree` 已能整树终止。
#把正在等待的子进程登记在这里，`/stop` 时一并掐掉。
# 命令一被杀就立刻返回，循环里的检查点马上命中，不需要为工具再开线程。

#: 会话 → 正在等待的子进程 `(proc, command_head, started_at)`。
#: 只记命令前 80 字符：这张表的读者是日志与 kill，全文在 `core/jobs.py`。
#: 会话级状态，已登记在 `core/state_registry.py`。
_procs: Dict[str, set] = {}


def register_process(session_key: str, proc, command: str = "") -> None:
    """把一条正在等的子进程挂到会话上（`core/shell.py` 在 `Popen` 之后调）。

    只记前 80 个字符的命令 —— 这张表唯一的读者是日志与 kill，不需要全文
    （全文在 `core/jobs.py` 那张表里，那张是"给模型看的"）。
    """
    if not session_key or proc is None:
        return
    with _lock:
        _procs.setdefault(str(session_key), set()).add(
            (proc, str(command or "")[:80], time.time()))


def unregister_process(session_key: str, proc) -> None:
    """摘掉一条（命令结束 / 出错 / 被杀 —— 必须在 `finally` 里调）。

    不摘的后果不是"内存泄漏"那么轻：**下次 `/stop` 会去杀一个已经结束的
    PID，而 PID 是会被复用的 —— 那就是杀错进程**。这条判据与
    `core/memory_organize` 那个锁文件的"1h 陈旧线含 PID 复用护栏"是同一族。
    """
    if not session_key or proc is None:
        return
    with _lock:
        s = _procs.get(str(session_key))
        if not s:
            return
        for item in list(s):
            if item[0] is proc:
                s.discard(item)
        if not s:
            _procs.pop(str(session_key), None)


def running_processes(session_key: str) -> int:
    """这个会话现在挂着几条命令。只读（自检 / 测试 / 日志用）。"""
    with _lock:
        return len(_procs.get(str(session_key), ()))


def kill_processes(session_key: str) -> int:
    """杀掉这个会话正在等的所有子进程，返回杀了几条。永不抛。

    这是"直接掐"里唯一真能掐的那一半。调用方从另一个线程调它
    （Web 的 `POST /api/stop`），因为被掐的那条正在本线程里阻塞。

    杀完就摘（不摘的话，`is_cancelled` 之外的路径会重复杀 → PID 复用）。
    `_kill_tree` 是惰性 import 的：`core.shell` 会 import 本模块，
    顶层 import 就是环。
    """
    with _lock:
        items = list(_procs.pop(str(session_key), ()))
    if not items:
        return 0
    try:
        from core.shell import _kill_tree
    except Exception as exc:                      # noqa: BLE001
        print("[cancel] 拿不到 _kill_tree（%s: %s）—— 杀不掉，命令会继续跑"
              % (type(exc).__name__, exc))
        return 0
    killed = 0
    for proc, cmd, _t in items:
        try:
            _kill_tree(proc)
            killed += 1
            print("[cancel] 已掐掉一条命令：%s" % (cmd or "(空)"))
        except Exception as exc:                  # noqa: BLE001
            print("[cancel] 掐命令失败（%s: %s）" % (type(exc).__name__, exc))
    return killed


def purge(session_key: str) -> None:
    """`/clear` 走这里：先杀、再清表。两格一起（`state_registry` 的入口只有一个）。

    与 `core/jobs.py::clear`（只清不杀）是反的，而那是刻意的：
    作业是用户明确要求转后台的（超时降级那条路），杀掉它就等于把用户
    自己点的"继续跑"又掐了；而 `_procs` 里挂的是前台命令 —— 会话都清了，
    已经没有任何人在等它，让它继续改文件才是危险的那一侧
    （本仓那条「不许静默改坏」）。

    两次操作分开上锁：`kill_processes` 自己弹表并上锁，
      在它之后再清一次 `_interrupted` —— 不要合成一次持锁做完。
    """
    kill_processes(session_key)
    clear_processes(session_key)
    with _lock:
        _interrupted.discard(str(session_key or ""))


def clear_processes(session_key: str) -> None:
    """只清表、不杀（给"已经杀过、只想收尾"的调用方）。"""
    with _lock:
        _procs.pop(str(session_key), None)


def clear_interrupt_note(session_key: str) -> None:
    """丢掉"还没说出口的那句话"。独立一个函数，不与上面两个合并 ——
    `state_registry` 的 `purge` 适配器按函数名字符串取，合并了就说不清
    哪一格由谁清（同 `recover` 与 `clear_exempt` 分开那条判据）。"""
    with _lock:
        _interrupted.discard(str(session_key or ""))

def request_cancel(session_key: str) -> bool:
    """请求中断 *session_key* 正在跑的那一轮。返回是否新增了标志。

    顺手把正在等的那条命令掐掉。置标志只让循环在下一个检查点停下，
    而检查点之间可能夹着一条跑五分钟的命令 —— 两件事一起做，才是"立刻"。

    从 Web 的 `POST /api/stop` 调它时才有意义（那个请求在另一个线程里，
    而被掐的循环正阻塞在自己的线程里）。从 `pre_process` 的 `/stop` 命令调它时
    永远不会命中（同一个线程正卡在工具里，那条命令根本轮不到被处理）——
    这条不是遗漏，是 `cancellation.py` 老 docstring 里那条"IM 打不断"的同一件事。
    """
    if not session_key:
        return False
    added = False
    with _lock:
        # 不在这里判断"有没有轮次在跑"：空闲时设上的标志要留到下一轮被读到，
        #   这是本模块 docstring 写明的安全行为。改动它会让 `test_agent_brakes`
        #   里多条以该行为为前提的断言失效 —— 要动它，先拿到真实的失败现场。
        if session_key not in _cancelled:
            _cancelled.add(session_key)
            added = True
    # 放在锁外面：`kill_processes` 自己上锁，而且 `_kill_tree` 会
    #    起一个 `taskkill` 子进程 —— 持锁去 fork 是自找的卡顿。
    kill_processes(session_key)
    return added


# 下一轮开口时，先告诉模型上一轮被用户打断了
#
# 不在中断当下写历史（避免留下一条"上一次被打断了"的孤儿消息），
# 而是延到下一次请求前插入一句说明 —— 模型真正看到上下文时才会用到它。
# 两条差别是真实的，且延后插入更省：
#   · 用户再也没开口，历史里不留一条"上一次被打断了"的孤儿消息
#   · 而模型在下一次看到上下文时照样看得到（那才是它有用的时候）
# 所以它不能跟着 `clear()` 一起清 —— 见 `clear` 的 docstring。

#: 会话 → 上一轮是"工具执行中被用户打断"的，下一次组装用户消息时前面插一句。
_interrupted: set[str] = set()


def note_tool_interrupt(session_key: str) -> None:
    """记下"这一轮是在工具执行中被打断的"。只记不清（等下一轮取）。"""
    if not session_key:
        return
    with _lock:
        _interrupted.add(str(session_key))


def take_tool_interrupt(session_key: str) -> bool:
    """读并清那个标记。返回"要不要插那句话"。一次性（`take_` 不是 `get_`）。"""
    if not session_key:
        return False
    with _lock:
        if session_key in _interrupted:
            _interrupted.discard(session_key)
            return True
        return False


def is_cancelled(session_key: str) -> bool:
    """这一轮是否已被要求中断。只读，不清除。"""
    if not session_key:
        return False
    with _lock:
        return session_key in _cancelled


def clear(session_key: str) -> None:
    """清掉标志。由调度器在每轮开局调用。

    它只清标志，不清 `_interrupted` —— 那两个东西的寿命不一样：
    标志是"这一轮被打断了"（轮内），而 `_interrupted` 是"**下一次用户开口时
    要告诉他，上一轮被你自己打断了**"（跨轮）。合成一个清法，会把
    "还没说出口的那句话"提前抹掉 —— 而它恰好是本条的一半。
    """
    if not session_key:
        return
    with _lock:
        _cancelled.discard(session_key)


def pending() -> list[str]:
    """当前挂着中断标志的会话（自检/测试用）。"""
    with _lock:
        return sorted(_cancelled)
