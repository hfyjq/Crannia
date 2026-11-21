"""在数据快照里跑 TUI，退出后还原。

为什么快照的是 `data/` 而不是 git：会变的那些东西（memory 里的会话、
runtime 的缓存、events、incidents、workdata）全在 `.gitignore` 里，git 对它们
既看不见、也还原不了。`git reset --hard` 只动那 5 个示例文件；要清干净得
`git clean -fdx`，而它会把 .venv 和 node_modules 一起删掉。

为什么不用 git worktree：那是"要改 crannia 代码"时的工具。这里代码只读，
而 worktree 里没有依赖（要重装几十个包）也没有配置，等于跑不起来。

为什么快照 `data/` 就够了：`config.yaml` 在代码根，不在数据目录里。
所以还原数据不会丢配置，备份体积也就几百 KB。

兜底：进程被强杀时 `finally` 不会执行，快照会留在盘上。下次启动会发现它，
先把现场说清楚 —— 悄悄覆盖或者悄悄丢弃，都比停在那里更糟。
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui.paths import crannia_home  # noqa: E402

DATA = crannia_home() / "data"
SNAP = crannia_home() / "data.__snapshot__"
#: 单实例锁。放在本包目录而不是 crannia 里，是为了不往根仓库多一个未跟踪文件。
LOCK = HERE / ".sandbox.lock"


def _pid_alive(pid: int) -> bool:
    """这个 PID 现在还有没有活的进程。

    不用 `os.kill(pid, 0)`：Windows 上它只支持 CTRL_C_EVENT / CTRL_BREAK，
    对普通进程没有"只探测"的语义。`OpenProcess` 拿到句柄还不够 —— 已退出的
    进程句柄仍然能打开，得再看退出码是不是 `STILL_ACTIVE`。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # PROCESS_QUERY_LIMITED_INFORMATION：权限最小，够读退出码
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259          # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                        # noqa: BLE001
        return False


def acquire() -> None:
    """抢锁。已经有实例在跑就停下来说清楚，不要两个一起写同一个 data。

    两个实例同时跑不是"慢一点"的问题：`data/memory/` 下是 sqlite，
    两个进程各持一把写锁时，轻则报 database is locked，重则把会话库写坏。
    而且两边的快照会互相覆盖 —— 先退出的那个把自己的快照换回 data，
    后退出那个换回的是别人跑过之后的状态，还原语义就没了。
    """
    if LOCK.exists():
        pid = 0
        started = "?"
        try:
            info = json.loads(LOCK.read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
            started = info.get("started") or "?"
        except Exception:                                    # noqa: BLE001
            pass
        if _pid_alive(pid):
            raise SystemExit(
                f"已经有一个实例在跑（PID {pid}，{started} 起）。\n"
                "两个实例同时写同一个 data/ 会互相锁住 sqlite，也可能把会话库写坏。\n"
                "那个窗口里按 Ctrl+C 或输入 /exit 退掉；找不到窗口就：\n"
                f"  taskkill /F /PID {pid}")
        # 锁在、进程没了 —— 上次被强杀留下的，直接接管。
        _say(f"[sandbox] 清掉上次留下的锁（PID {pid} 已不在）")
        try:
            LOCK.unlink()
        except OSError:
            pass

    LOCK.write_text(json.dumps({
        "pid": os.getpid(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False), encoding="utf-8")


def release() -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 让子进程跟着父进程一起死
# ---------------------------------------------------------------------------

#: Job 句柄必须活到本进程结束。提前 `CloseHandle` 等于立刻杀子进程 ——
#: 所以放在模块级，不关。
_JOB = None


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        # ULONG_PTR：在 64 位上是 8 字节，用 c_size_t 对齐
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kill_with_parent(pid: int) -> bool:
    """把这个 PID 挂进一个"最后一个句柄关闭时全杀"的 Job。

    解决的是这样一条路径：本进程被 TerminateProcess 打死（任务管理器、
    `timeout`、关窗口、蓝屏前的那一下），`finally` 不执行，`proc.terminate()`
    也没机会跑 —— TUI 就成了孤儿，继续写 data、继续持有 sqlite 锁，
    而下一次启动只能看到"锁里的 PID 已经死了"，于是接管并再起一个。
    两个实例同时写同一个会话库，就是这样发生的。

    Job 的语义正好对上：本进程一死，它持有的 job 句柄由内核关闭，
    job 里所有进程被一起终结。本进程活着时这行代码什么都不做。
    """
    global _JOB
    if os.name != "nt":
        return False

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _JOB = kernel32.CreateJobObjectW(None, None)
        if not _JOB:
            return False

        info = _EXTENDED_LIMIT()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = 0x2000
        # JobObjectExtendedLimitInformation = 9
        if not kernel32.SetInformationJobObject(
                _JOB, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return False

        # PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x0100 | 0x1000, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(_JOB, handle))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                        # noqa: BLE001
        return False


def _say(msg: str) -> None:
    """必须立刻可见。

    输出被重定向到文件时 stdout 是块缓冲的，不 flush 的话这几行会在进程退出时
    一次性吐出来 —— 混在 TUI 输出后面，日志看起来像是"先跑完才快照"。顺序一错，
    排查的人会怀疑快照根本没生效。
    """
    print(msg, flush=True)


def _rm(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _raise_interrupt(signum, frame) -> None:
    """把 SIGTERM 折成 KeyboardInterrupt。

    `timeout 300 …` 和任务管理器结束进程发的都是 TERM；默认行为是立即终止，
    `finally` 不执行，快照就留在盘上。折成 Ctrl-C 之后它走同一条收尾路径。
    """
    raise KeyboardInterrupt


def backup() -> None:
    if SNAP.exists():
        raise SystemExit(
            f"发现上次没还原的快照：{SNAP}\n"
            f"上一次多半是被强杀/关窗口打断的（Windows 上 `timeout` 和任务管理器\n"
            f"都是直接终止进程，finally 不执行）。先确认要保留哪一份，然后：\n"
            f"  还原（丢掉这次跑出来的 data）：  sandbox_run.py --restore\n"
            f"  丢弃（保留现在的 data）：        sandbox_run.py --discard\n"
            f"手动的话：还原 = 删掉 {DATA} 再把快照改名回 data")
    if not DATA.exists():
        _say(f"[sandbox] {DATA} 不存在，这次从空数据开始")
        DATA.mkdir(parents=True, exist_ok=True)
        return
    shutil.copytree(DATA, SNAP)
    n = sum(1 for _ in SNAP.rglob("*"))
    _say(f"[sandbox] 已快照 {DATA.name}（{n} 项）")


def restore() -> bool:
    """还原。sqlite 句柄可能还没被系统释放，所以重试几次。"""
    for _ in range(10):
        try:
            _rm(DATA)
            SNAP.rename(DATA)
            _say(f"[sandbox] 已还原 {DATA.name}")
            return True
        except OSError:
            time.sleep(0.5)
    _say(f"[sandbox] !! 还原失败。快照仍在 {SNAP}，手动改回 {DATA.name} 即可")
    return False


def main(argv: list[str]) -> int:
    # `--restore` / `--discard`：处理上一次被打断留下的快照。
    # 单独给开关而不是自动猜，是因为"data 里那份"和"快照里那份"都可能是
    # 用户想留的，替人决定等于替他丢数据。
    if "--restore" in argv or "--discard" in argv:
        # 有实例在跑时不许动数据：它正在写，这时候换目录等于把它的写入
        # 落到一个已经被移走的 inode 上 —— 跑完还看不见，比报错更糟。
        if LOCK.exists():
            try:
                info = json.loads(LOCK.read_text(encoding="utf-8"))
                pid = int(info.get("pid") or 0)
            except Exception:                                # noqa: BLE001
                pid = 0
            if _pid_alive(pid):
                raise SystemExit(
                    f"还有实例在跑（PID {pid}），先退掉它再处理快照。\n"
                    f"  taskkill /F /PID {pid}")
            # 陈旧锁顺手清掉，否则下次启动会看到一条已死的 PID。
            try:
                LOCK.unlink()
            except OSError:
                pass

        if "--restore" in argv:
            if not SNAP.exists():
                _say("[sandbox] 没有待还原的快照")
                return 0
            return 0 if restore() else 1

        if not SNAP.exists():
            _say("[sandbox] 没有待丢弃的快照")
            return 0
        _rm(SNAP)
        _say(f"[sandbox] 已丢弃 {SNAP.name}，{DATA.name} 保持现状")
        return 0

    # `--` 之后的参数原样交给 TUI
    passthrough = argv[argv.index("--") + 1:] if "--" in argv else []
    acquire()
    backup()
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _raise_interrupt)

    cmd = [sys.executable, "-m", "crannia_tui"] + passthrough
    _say(f"[sandbox] 启动：{' '.join(cmd)}")

    code = 1
    # 用 Popen 而不是 call：中断时要先把子进程收掉再还原。反过来做的话，
    # TUI 还在写的时候 data 被换走，它的写入会落到已经被替换掉的目录上。
    # 继承当前环境：CRANNIA_PYTHON 等由调用方给，这里不重新猜一遍。
    proc = subprocess.Popen(cmd, cwd=str(HERE), env=dict(os.environ))
    # 一启动就挂进 job：晚一步的话，恰好在这一步被杀就漏掉了。
    _kill_with_parent(proc.pid)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        code = 130
    finally:
        restore()
        release()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
