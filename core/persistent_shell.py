# -*- coding: utf-8 -*-
"""会话级持久 shell：同一会话的命令跑在同一个 bash 进程里。

一次性进程每次都从零开始，`export`、venv 激活、shell 函数活不过一条命令。
本模块只补这一层 —— 命令判定、审批、审计仍在 `core/shell.py` 里，一处不改。

## 协议

命令写进 stdin，随后写一个带序号的标记；读端按标记切分一次调用：

    <command>
    __crannia_rc=$?
    printf '\\n\\x01CRANNIA_END\\x01<seq>\\x01<rc>\\x01<hex-cwd>\\x01\\n' ...

- 序号逐条递增，命令自己打印同样的串也不会被误判成结束（除非它猜中下一个序号）。
- cwd 用十六进制回传：标记与命令输出走同一个流，路径里有中文时
  混进 UTF-8 字节会让整段被判成别的编码（`core/shell.py::_wrap` 同一处理）。
- stdout 与 stderr 合并（`stderr=STDOUT`）：标记只落在一个流上，
  合并后不需要为两个管道各配一套"命令结束了吗"的判定。回执里标明合并。

## 寿命

- 每会话一个进程、一把锁，同一会话的命令串行。
- 超时 / 进程退出 / `/stop`，杀掉并丢弃这个会话的 shell，
  下一次调用重新起一个；会话内的变量与函数随之丢失（回执里如实说）。
- `/clear` 走 `core.state_registry.purge_session` → :func:`close_session`。

## 边界

- 不是沙箱：命令与一次性进程跑在同一个用户下，判定与审批在调用方做。
- 不支持转后台：进程是会话共享的，把"一次调用"转后台等于让后续命令
  与它抢同一个 stdin。`run_command` 在入口处拒绝这个组合。
"""

from __future__ import annotations

import atexit
import shlex
import subprocess
import threading
import time
from pathlib import Path

__all__ = ["run", "close_session", "close_all", "session_alive"]

_MARK_TEXT = "\x01CRANNIA_END\x01"

#: 每会话一个 shell。会话级状态 —— 已登记到 `core/state_registry.py`。
_sessions: dict = {}
_LOCK = threading.Lock()


class _Shell:
    """一个会话的 bash 进程。调用方负责串行（`run()` 里的锁）。"""

    def __init__(self, session_id: str, work_dir: Path) -> None:
        from core.shell import bash_path, clean_env, ensure_snapshot

        bash = bash_path()
        if not bash:
            raise RuntimeError("找不到 bash，持久 shell 起不来。")
        self.session_id = session_id
        self.lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._seq = 0
        self._event = threading.Event()
        self._eof = False
        self._marker_line = ""
        self._cwd = str(work_dir)
        self._snapshot = ensure_snapshot()
        self.proc = subprocess.Popen(
            [bash, "--noprofile", "--norc", "-s"],
            cwd=str(work_dir), env=clean_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.sink = None
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    # 读端
    def _pump(self) -> None:
        from core import jobs

        try:
            while True:
                # 必须 `read1`：`read(n)` 会等满 n 字节或 EOF，而常驻进程
                #   两个都不会发生，读线程永远不返回，标记永远看不见
                #   （症状是每条命令都超时）。
                chunk = self.proc.stdout.read1(4096)
                if not chunk:
                    break
                with self._write_lock:
                    if self.sink is not None:
                        self.sink.write(chunk)
                        text = self.sink.text()
                        want = _MARK_TEXT + str(self._seq) + "\x01"
                        idx = text.rfind(want)
                        if idx >= 0:
                            end = text.find("\n", idx)
                            if end >= 0:
                                self._marker_line = text[idx:end]
                                self._event.set()
        except Exception:                                  # noqa: BLE001
            pass
        finally:
            with self._write_lock:
                self._eof = True
                self._event.set()

    @property
    def alive(self) -> bool:
        return (not self._eof) and self.proc.poll() is None

    # 一次调用
    def run(self, command: str, cwd: str, timeout_ms: int) -> dict:
        from core import jobs

        with self.lock:
            if not self.alive:
                raise RuntimeError("shell 已退出")

            with self._write_lock:
                self._seq += 1
                seq = self._seq
                self.sink = jobs._Sink()
                self._marker_line = ""
                self._event.clear()

            prologue = ""
            if self._snapshot:
                prologue += "source %s 2>/dev/null || true\n" % shlex.quote(
                    self._snapshot)
            if cwd:
                prologue += "cd %s\n" % shlex.quote(str(cwd))

            fmt = ("\\n\\x01CRANNIA_END\\x01" + str(seq)
                   + "\\x01%s\\x01%s\\x01\\n")
            payload = (prologue + command.rstrip("\n") + "\n"
                       + "__crannia_rc=$?\n"
                       + "printf '" + fmt + "' \"$__crannia_rc\" "
                       + "\"$(printf '%s' \"$(pwd -W 2>/dev/null || pwd)\" "
                       + "| od -An -v -tx1 | tr -d ' \\n')\"\n")
            try:
                self.proc.stdin.write(payload.encode("utf-8"))
                self.proc.stdin.flush()
            except Exception as exc:                       # noqa: BLE001
                self.reset()
                return {"success": False, "exit_code": -1, "stdout": "",
                        "stderr": "", "cwd": self._cwd, "shell_reset": True,
                        "error": "写入 shell 失败（%s）—— 已重置这个会话的 shell。"
                                 % type(exc).__name__}

            secs = max(1.0, (timeout_ms or 0) / 1000.0)
            if not self._event.wait(secs):
                self.reset()
                return {
                    "success": False, "timed_out": True, "exit_code": -1,
                    "stdout": "", "stderr": "", "cwd": self._cwd,
                    "shell_reset": True, "persistent": True,
                    "note": ("命令超时，**这个会话的 shell 已被重置** —— "
                             "里面 `export` 过的变量、激活过的 venv 都没了；"
                             "下一条命令会起一个新的。"),
                }

            with self._write_lock:
                text = self.sink.text() if self.sink is not None else ""
                line = self._marker_line
                eof = self._eof

            if not line:
                self.reset()
                return {
                    "success": False, "exit_code": -1, "stdout": text,
                    "stderr": "", "cwd": self._cwd,
                    "shell_reset": True, "persistent": True,
                    "note": ("shell 在执行这条命令时退出了（`exit` 或崩溃）—— "
                             "已重置；下一条命令会起一个新的。"),
                }

            parts = line.split("\x01")
            rc = self._to_int(parts[3] if len(parts) > 3 else "")
            new_cwd = ""
            if len(parts) > 4 and parts[4]:
                try:
                    from core.shell import _cwd_from_hex
                    new_cwd = _cwd_from_hex(parts[4])
                except Exception:                           # noqa: BLE001
                    new_cwd = ""
            if new_cwd:
                self._cwd = new_cwd

            idx = text.rfind(_MARK_TEXT + str(seq) + "\x01")
            out = text[:idx] if idx >= 0 else text
            if out.endswith("\n"):
                out = out[:-1]

            return {
                "success": rc == 0,
                "exit_code": rc,
                "cwd": self._cwd,
                "stdout": out,
                "stderr": "",
                "persistent": True,
                "streams_merged": True,
                "note": "持久 shell：stdout 与 stderr 合并在 `stdout` 里。",
                **({"shell_reset": True} if eof else {}),
            }

    @staticmethod
    def _to_int(raw: str) -> int:
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return -1

    # 收尾
    def reset(self) -> None:
        """杀掉进程树。幂等。"""
        try:
            from core.shell import _kill_tree
            _kill_tree(self.proc)
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.proc.stdin.close()
        except Exception:                                   # noqa: BLE001
            pass
        self._eof = True


def _get(session_id: str, work_dir: Path) -> _Shell:
    with _LOCK:
        sh = _sessions.get(session_id)
        if sh is not None and not sh.alive:
            sh.reset()
            _sessions.pop(session_id, None)
            sh = None
        if sh is None:
            sh = _Shell(session_id, work_dir)
            _sessions[session_id] = sh
        return sh


def run(session_id: str, command: str, work_dir: Path,
        timeout_ms: int = 0, cwd: str = "") -> dict:
    """在会话级 shell 里跑一条命令。返回与 `shell._run` 同形状的结果。"""
    from core.shell import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS

    ms = int(timeout_ms or 0) or DEFAULT_TIMEOUT_MS
    ms = max(1000, min(ms, MAX_TIMEOUT_MS))
    try:
        sh = _get(session_id, Path(work_dir))
    except Exception as exc:                                # noqa: BLE001
        return {"success": False, "exit_code": -1,
                "error": "持久 shell 起不来（%s: %s）" % (type(exc).__name__, exc)}
    try:
        return sh.run(command, cwd, ms)
    except RuntimeError:
        # shell 在两次调用之间死了，换一个新的再跑一次（只重试一次）。
        close_session(session_id)
        try:
            sh = _get(session_id, Path(work_dir))
            return sh.run(command, cwd, ms)
        except Exception as exc:                            # noqa: BLE001
            return {"success": False, "exit_code": -1,
                    "error": "持久 shell 起不来（%s: %s）"
                             % (type(exc).__name__, exc)}


def session_alive(session_id: str) -> bool:
    with _LOCK:
        sh = _sessions.get(session_id)
        return bool(sh is not None and sh.alive)


def close_session(session_id: str) -> None:
    """关掉一个会话的 shell（`purge_session` 的入口）。幂等。"""
    with _LOCK:
        sh = _sessions.pop(str(session_id), None)
    if sh is not None:
        sh.reset()


def close_all() -> None:
    with _LOCK:
        shells = list(_sessions.values())
        _sessions.clear()
    for sh in shells:
        sh.reset()


atexit.register(close_all)
