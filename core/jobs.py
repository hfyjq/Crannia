"""框架持有跨轮执行体 —— 命令超时/主动转后台，而不是杀掉。

它从"只装超时降级的命令"扩成了统一任务表
（命令 + 子 agent 共用一张表）。新增的字段与函数：

- `kind` / `session_key` / `storage_id` / `description` / `output_path` / `notified`
  —— 没有 session 就不知道该把完成通知投回哪条会话（旧 `Job` 连这个都没有）。
- `cancel()` / `kill_all()` / `running_for()` —— 模型与用户都能按任务取消，
  不再只能按会话杀当前前台命令。
- `_finish_hooks` —— 完成通知的挂点（批 2 接 scheduler；本批只留接口）。
- 前台登记 `note_foreground` / `request_background` —— 用户 Ctrl+B 转后台的入口：
  它只给正在跑的那条置事件，不会误伤下一条命令。


## 它解决什么

`run_command` 超时时如果直接 `_kill_tree()` 杀掉 ——
五秒钟的 `pip install`、三分钟的构建，全部白干，而模型拿到的是一句
"已被强制结束"。

降级到后台：不杀，把它挂到作业表上，让模型回来查。
这就是"框架持有一个跨越 `tool_call` 的执行体"的第三种形态
（另两种：子 agent 持有一个循环 · 一次持有多个工具调用）。

## 三条残余（别读成"堵上了"）

1. 只有超时那条路会建档 —— 正常完成的命令不进作业表（一个字节都不变）。
   这条是刻意的，`test_shell.py` 对那条路有大量断言。
2. 被降级的命令回读不到 cwd —— `cwd` 是"命令结束时问 bash"问出来的，
   而它结束时我们早就不在那一轮了。后台作业里的 `cd` 不粘（写进了它的回执）。
3. 进程真的会活过这一轮 —— 有寿限（`JOB_MAX_LIFETIME_MS`）兜底，
   但框架退出时不一定能杀干净（Windows 上尤其）。这是"持有执行体"的固有代价，
   与子 agent 一样：它买的不是安全，是"别白干"。

## 线程模型

每个作业一条读线程（stdout）+ 一条（stderr，合流到同一个缓冲区）+ 一条看门狗
（到寿限就杀树）。读线程用 `read(4096)` 循环直到 EOF —— 这是两个管道都要排空的
标准安全写法（只读一个会死锁在另一个写满时）。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

#: 后台作业的寿限 —— 到点杀树。没有它，一条 `while true` 会活到重启。
JOB_MAX_LIFETIME_MS = 30 * 60 * 1000        # 30 分钟

#: 一条作业最多留多少字符（尾部的）。与 `run_command` 的裁剪同向：
#: 保尾部（错在哪通常在最后）。上限之外的只记「丢了 N 字符」。
JOB_OUTPUT_CAP = 200_000

#: 已结束的作业留多久（秒）—— 之后从表里清掉（`/clear` 也会清）。
_JOB_KEEP_SECONDS = 60 * 60

#: 任务类别：命令、子 agent、技能后台任务。
KIND_COMMAND = "command"
KIND_SUBAGENT = "subagent"
#: 技能的后台任务，由 core/extension.py 提交。任务是一次性的，会话清理时终止。
KIND_SKILL = "skill"

#: id 前缀：c = command，s = subagent，k = skill。
_ID_PREFIX = {KIND_COMMAND: "c", KIND_SUBAGENT: "s", KIND_SKILL: "k"}


def _new_id(kind: str) -> str:
    return _ID_PREFIX.get(kind, "x") + uuid.uuid4().hex[:8]


def _safe_segment(text: str) -> str:
    """把 `storage_id` / `session_key` 变成能当目录名的片段。

    会话键里有 `:`（`workspace:private`），Windows 上不能直接当目录名。
    """
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text or ""))
    return s.strip("._")[:80] or "unknown"


def output_path_for(task_id: str, storage_id: str = "") -> str:
    """任务输出的落盘路径：每个存储段一个目录 —— 同名任务在不同段之间
    不能互相覆盖，而段是按会话切的。

    没有存储键，空串（不落盘）：生产路径上一定有会话，
    没有只可能是测试/直接调用 —— 那时候往仓库里写日志就是测试污染。
    """
    if not storage_id:
        return ""
    return str(Path("data") / "runtime" / "tasks"
               / _safe_segment(storage_id) / (task_id + ".log"))


#: 判定输出编码要看的头几个字节。短于它、又没见到换行时先攒着 ——
#: 一次 `read(4096)` 的尾巴可能正好切在多字节字符中间，拿半截去判定
#: 会把 UTF-8 误判成别的编码。
_SNIFF_BYTES = 512


def _sniff_encoding(head: bytes) -> tuple[str, int]:
    """从流的开头判断编码。返回 `(编码名, 要跳过的 BOM 字节数)`。

    顺序：BOM → UTF-8 → 本地代码页 → latin-1。
    最后那个兜底永远不会失败，且一个字节都不丢（1 字节 = 1 字符）——
    它比"替换字符"好：替换字符会让回执看起来像"内容本来就是这样"。
    """
    import codecs
    import locale

    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF32_LE, "utf-32-le"),
                     (codecs.BOM_UTF32_BE, "utf-32-be"),
                     (codecs.BOM_UTF16_LE, "utf-16-le"),
                     (codecs.BOM_UTF16_BE, "utf-16-be")):
        if head.startswith(bom):
            return enc, len(bom)

    # 无 BOM：UTF-8 允许尾部被切断（最多 3 字节），切掉的先不算。
    for cut in range(0, 4):
        try:
            head[:len(head) - cut].decode("utf-8")
            return "utf-8", 0
        except UnicodeDecodeError:
            continue

    cands: list[str] = []
    try:
        cands.append(locale.getpreferredencoding(False) or "")
    except Exception:                                  # noqa: BLE001
        pass
    cands += ["cp936", "cp1252"]
    seen = set()
    for enc in cands:
        key = (enc or "").lower()
        if not key or key in seen or key in ("utf-8", "utf8"):
            continue
        seen.add(key)
        try:
            head.decode(enc)
            return enc, 0
        except (UnicodeDecodeError, LookupError):
            continue
    return "latin-1", 0


class _Sink:
    """有上限的文本缓冲（带锁）—— 读线程往里写、作业表从里读。

    关键在于它从进程一起来就存在：`subprocess.communicate()` 的超时路径
    会把管道读掉（它自己有内部读线程），之后再挂读线程什么都拿不到，
    症状是"后台作业跑完了但输出是空的"。
判据：`communicate()` 与手动 `read()` 不许混用；要么全用它，要么全自己读。
    这里是"全自己读"：读线程先起，`wait(timeout=)` 负责超时。

    编码在流开头判一次，之后用增量解码器 —— 逐块 `decode(errors="replace")`
    会在两种情况下静默改字：① 多字节字符被块边界切开；② 输出根本不是 UTF-8
    （Windows 上的原生程序常见）。判定结果放在 :attr:`encoding`，回执要报出来。
    """

    def __init__(self, cap: int = JOB_OUTPUT_CAP):
        self._cap = cap
        self._parts: list[str] = []
        self._len = 0
        self._dropped = 0
        self._lock = threading.Lock()
        self._decoder = None
        self._pending = b""
        self._encoding = ""

    @property
    def encoding(self) -> str:
        """实际用来解码的编码名（还没判出来时是空串）。"""
        return self._encoding

    def _start(self) -> None:
        import codecs
        enc, skip = _sniff_encoding(self._pending)
        self._encoding = enc
        self._decoder = codecs.getincrementaldecoder(enc)(errors="replace")
        text = self._decoder.decode(self._pending[skip:])
        self._pending = b""
        self._append(text)

    def _append(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._parts.append(text)
            self._len += len(text)
            while self._len > self._cap and len(self._parts) > 1:
                dropped = self._parts.pop(0)
                self._len -= len(dropped)
                self._dropped += len(dropped)

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._decoder is None:
            self._pending += chunk
            # 换行与回车都算"够判了"：进度条这类输出用 `\r` 刷新、不带换行，
            #   只认 `\n` 会让一个长跑进程的进度在跑完之前一个字都看不到。
            if (len(self._pending) < _SNIFF_BYTES
                    and b"\n" not in chunk and b"\r" not in chunk):
                return
            self._start()
            return
        self._append(self._decoder.decode(chunk))

    def finish(self) -> None:
        """流结束：把攒着的头解出来、把解码器里残留的字节吐出来。幂等。"""
        if self._decoder is None:
            if self._pending:
                self._start()
            return
        self._append(self._decoder.decode(b"", final=True))

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped


@dataclass
class Job:
    id: str
    command: str
    pid: int
    started: float
    out: _Sink
    err: _Sink
    # 它不再只是"命令"，而是统一任务表的一行
    kind: str = KIND_COMMAND        # command | subagent
    session_key: str = ""          # 通道键（取消 / 通知路由 / 发现集）
    storage_id: str = ""           # 存储键（落库 / 输出目录）
    description: str = ""          # 一行摘要（UI / 完成通知）
    output_path: str = ""          # 完成时把 out/err 落盘
    status: str = "running"        # running | done | timeout | failed | killed
    exit_code: Optional[int] = None
    finished: float = 0.0
    notified: bool = False         # 完成通知只发一次（锁内 check-and-set）
    #: 这一条要不要发"后台任务回报"（收尾补丁）——
    #: 判据是创建那一刻它是不是真在后台：前台子 agent 的结论由父就地收下，
    #: 再发一条回报就是同一件事收到两遍（第二轮看起来像用户又提了一次）。
    #: 必须在创建时定：等到父拿到结果再置，会输给完成钩子那条线程
    #: （两个前台子 agent 都发了一条多余的回报）。
    #: 中途转后台（`Ctrl+B` / 超预算）时由调用方把它翻回 `True`。
    notify_on_finish: bool = True
    proc: object = None            # subprocess.Popen / None（子 agent 用 cancel_event）
    cancel_event: object = None    # threading.Event（子 agent 的协作式取消）
    done_event: object = None      # threading.Event（`task(action="wait")` 阻塞它）
    result: str = ""               # 子 agent 的最终结论

    def elapsed_ms(self) -> int:
        end = self.finished or time.monotonic()
        return int((end - self.started) * 1000)

    def snapshot(self, tail: int = 3000) -> dict:
        d = {
            "job_id": self.id,
            "task_id": self.id,
            "kind": self.kind,
            "description": self.description or self.command,
            "command": self.command,
            "status": self.status,
            "pid": self.pid,
            "elapsed_ms": self.elapsed_ms(),
            "session_key": self.session_key,
            "output_file": self.output_path,
            "notified": self.notified,
        }
        if self.status != "running":
            d["exit_code"] = self.exit_code
        if self.out.dropped:
            d["output_dropped_chars"] = self.out.dropped
        # 非 UTF-8 的输出要报出来：否则乱码看起来像"程序本来就输出这个"。
        enc = self.out.encoding
        if enc and enc not in ("utf-8", "utf-8-sig"):
            d["output_encoding"] = enc
        # 保尾部（与 `run_command` 的裁剪同向：错在哪通常在最后）
        # `tail=0` 的意思是"不要正文" —— 而 `s[-0:]` 在 Python 里是
        #    整串（不是空串）。`running_for` 与 `task(action="status")`
        #    都用 0，写错会把每条任务的完整输出塞进列表里。
        d["stdout"] = self.out.text()[-tail:] if tail else ""
        err = self.err.text()
        if tail and err.strip():
            d["stderr"] = err[-tail:]
        if self.result:
            d["result"] = self.result
        return d


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _register(job: Job) -> None:
    with _lock:
        _jobs[job.id] = job


#: 完成钩子 —— 批 2 在这里接"起一轮通知"。签名 `(job) -> None`，
#: 由 `jobs` 在看门狗线程里调用；钩子自己负责线程/事件循环。
_finish_hooks: list[Callable[[Job], None]] = []
#: 起始钩子 —— 批 5 在这里接 `task.started`（UI 的两格计数）。
#: 挂在建表这一处而不是各家调用点：命令与子 agent 分别在
#:   `core/shell.py` / `core/agent_loop.py` 建档，两处各发一次就是两条会漂的路。
_start_hooks: list[Callable[[Job], None]] = []


def on_finish(cb: Callable[[Job], None]) -> None:
    """登记一个完成钩子（幂等：同一个函数只登记一次）。"""
    if cb not in _finish_hooks:
        _finish_hooks.append(cb)


def on_start(cb: Callable[[Job], None]) -> None:
    """登记一个起始钩子（幂等）。在调用方线程里跑，要快、不许抛。"""
    if cb not in _start_hooks:
        _start_hooks.append(cb)


def _run_start_hooks(job: Job) -> None:
    for cb in list(_start_hooks):
        try:
            cb(job)
        except Exception as exc:                      # noqa: BLE001
            print("[jobs] 起始钩子失败（%s: %s）—— 任务照常跑"
                  % (type(exc).__name__, exc))


def _run_finish_hooks(job: Job) -> None:
    for cb in list(_finish_hooks):
        try:
            cb(job)
        except Exception as exc:                      # noqa: BLE001
            print("[jobs] 完成钩子失败（%s: %s）—— 任务照常收尾"
                  % (type(exc).__name__, exc))


def _dump_output(job: Job) -> None:
    """把内存里的输出落到 `output_path`（完成时一次）。"""
    # 收尾排在取正文之前：不足一次嗅探长度的尾巴还攒在缓冲里。
    try:
        job.out.finish()
        job.err.finish()
    except Exception:                                  # noqa: BLE001
        pass
    if not job.output_path:
        return
    try:
        path = Path(job.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = job.out.text()
        err = job.err.text()
        # 后台命令的输出里带着 shell 的 cwd 标记（`_split_cwd` 在前台路径上会摘掉它，
        #   而转后台时那一支没跑过），落盘前在这里摘，别把框架内部标记泄给模型。
        try:
            from core.shell import _CWD_MARK     # 懒导入：避免环
            idx = text.rfind(_CWD_MARK)
            if idx >= 0:
                text = text[:idx]
        except Exception:                        # noqa: BLE001
            pass
        if err.strip():
            text += ("\n[stderr]\n" if text else "") + err
        path.write_text(text, encoding="utf-8")
    except OSError as exc:                            # noqa: BLE001
        print("[jobs] 输出落盘失败（%s: %s）" % (type(exc).__name__, exc))


def start(proc: subprocess.Popen, command: str, work_dir: str,
          out: "_Sink", err: "_Sink", *, kind: str = KIND_COMMAND,
          session_key: str = "", storage_id: str = "",
          description: str = "") -> Job:
    """把一个已经在跑的进程接管成后台作业（登记 + 看门狗）。

    不在这里起读线程 —— 它们从进程一起来就在跑（`core/shell._run` 里），
    读写的是同一个 `_Sink`。理由见 `_Sink` 的 docstring（`communicate()` 会把管道读掉）。
这一支只做两件事：登记 + 到寿限杀树。
    """
    reap()          # 顺手收掉早就结束的条目（防表无界增长）
    task_id = _new_id(kind)
    job = Job(id=task_id, command=command,
              pid=getattr(proc, "pid", -1), started=time.monotonic(),
              out=out, err=err, kind=kind, session_key=session_key,
              storage_id=storage_id, description=description,
              output_path=output_path_for(task_id, storage_id), proc=proc,
              done_event=threading.Event())
    _register(job)

    def _watch() -> None:
        while True:
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if job.elapsed_ms() >= JOB_MAX_LIFETIME_MS:
                    job.status = "timeout"
                    try:
                        from core.shell import _kill_tree     # 懒导入：避免环
                        _kill_tree(proc)
                    except Exception:
                        pass
                    try:
                        code = proc.wait(timeout=10)
                    except Exception:
                        code = None
                    break
                continue
            break
        job.exit_code = code
        job.finished = time.monotonic()
        if job.status == "running":
            job.status = "done" if code == 0 else "failed"
        if job.done_event is not None:
            job.done_event.set()
        _dump_output(job)
        _run_finish_hooks(job)

    _run_start_hooks(job)          # 先出声再开跑：否则极快的任务会先发 finished
    threading.Thread(target=_watch, daemon=True).start()
    return job


def start_thread(run: Callable[["Job"], str], *, kind: str = KIND_SUBAGENT,
                 command: str = "", session_key: str = "",
                 storage_id: str = "", description: str = "",
                 notify: bool = True) -> Job:
    """把一个线程执行体（子 agent）接管成任务表的一行。

    *run(job)* 在新线程里跑，返回结论文本；它可以读 `job.cancel_event`
    做协作式取消，也可以把过程写进 `job.out`（落盘 / 面板用）。

    为什么子 agent 也要进这张表：用户要的两格计数里有一格就是它
      （「X 个子代理运行中」），而 `task(action="cancel")` / `/clear` 也必须够得着它。
      命令那半走 `start()`（挂的是 Popen）；这一半挂的是 Thread。

    与 `start()` 同一段收尾（`done_event` → 落盘 → 完成钩子），只有一处
      —— 两份收尾就是两条会各自漂的路。

    *notify*，跑完要不要发"后台任务回报"。前台那条传 `False`：
    它的结论由父就地收下（判据见 `Job.notify_on_finish`）。
    """
    reap()
    task_id = _new_id(kind)
    job = Job(id=task_id, command=command or description, pid=-1,
              started=time.monotonic(), out=_Sink(), err=_Sink(),
              kind=kind, session_key=session_key, storage_id=storage_id,
              description=description,
              output_path=output_path_for(task_id, storage_id),
              cancel_event=threading.Event(), done_event=threading.Event(),
              notify_on_finish=bool(notify))
    _register(job)

    def _watch() -> None:
        try:
            text = run(job)
        except Exception as exc:                      # noqa: BLE001
            job.status = "failed"
            job.result = f"{type(exc).__name__}: {exc}"
            job.out.write(("ERROR %s: %s" % (type(exc).__name__, exc)).encode("utf-8"))
        else:
            if text:
                job.out.write(str(text).encode("utf-8"))
                job.result = str(text)
                if job.status == "running":
                    job.status = "done"
        job.exit_code = 0 if job.status == "done" else None
        job.finished = time.monotonic()
        if job.done_event is not None:
            job.done_event.set()
        _dump_output(job)
        _run_finish_hooks(job)

    _run_start_hooks(job)          # 先出声再开跑：否则极快的任务会先发 finished
    threading.Thread(target=_watch, daemon=True).start()
    return job


# 前台登记 + 用户 Ctrl+B
#
# 为什么需要它：用户按 Ctrl+B 时，那条命令还没进任务表（它是前台的）。
#   直接给会话置一个布尔会误伤下一条命令（"按了没反应"之后回来一条新命令，
#   被这个陈旧标志顺手转走）。事件挂在具体的那个 proc 上。

_foreground: dict[str, list[tuple[object, threading.Event]]] = {}


def note_foreground(session_key: str, proc) -> Optional[threading.Event]:
    """登记一条正在前台等的进程；返回它的事件（调用方每 0.2s 看一次）。"""
    if not session_key or proc is None:
        return None
    ev = threading.Event()
    with _lock:
        _foreground.setdefault(str(session_key), []).append((proc, ev))
    return ev


def clear_foreground(session_key: str, proc) -> None:
    key = str(session_key or "")
    if not key or proc is None:
        return
    with _lock:
        items = [pair for pair in _foreground.get(key, ()) if pair[0] is not proc]
        if items:
            _foreground[key] = items
        else:
            _foreground.pop(key, None)


def forget_foreground(session_key: str) -> None:
    """`/clear` 用：丢掉这个会话的所有前台登记（不杀 —— 前台进程由那一轮自己收）。

    为什么不是 `clear_foreground(sid, proc)`：purge 只拿得到会话键，
    拿不到 proc。它清的是登记，而正在跑的那条会自己 `finally` 摘。
    """
    with _lock:
        _foreground.pop(str(session_key or ""), None)


def request_background(session_key: str) -> int:
    """用户点了「转后台」：给这个会话当前正在前台等的执行体置事件。

    返回置了几个（0 = 现在没有可转的前台任务 —— 界面应当说"没有可转的任务"，
    而不是假装成功）。
    """
    key = str(session_key or "")
    with _lock:
        items = list(_foreground.get(key, ()))
    for _proc, ev in items:
        ev.set()
    return len(items)


def foreground_count(session_key: str) -> int:
    """这个会话现在有几条前台执行体还在跑（命令 / 子 agent）。

    UI 那行「tips：ctrl+B …」的依据是**前端手里那条 tool.start 的
      `backgroundable`（事件驱动、零延迟）；这个函数是服务器侧的同一个口径**，
      给排障与测试用（两条不同源就会一边显示提示、一边转不动）。
    """
    with _lock:
        return len(_foreground.get(str(session_key or ""), ()))


def backgroundable_tool(name: str, args: dict | None = None) -> bool:
    """这一次工具调用现在有没有一个"前台执行体"可以转后台。

    判据（设计 §8）：

    - `run_command`：带 `job_id`，这是瞬时查询，没有执行体；
      `background=true`，它本来就后台，没有前台可转。
    - `run_subagent`：同上；另外 depth > 0（子 agent 内部）一律不可转。
    - 其余工具（进程内同步 Python）不可转 —— 它们没有能离开前台的执行体。

    审批尚未回答的调用也会为真 —— 那时活动块会随「挂起即停车」一起收掉，
       提示跟着消失（不需要在这儿再判一次，判据越少越不容易漂）。
    """
    a = args or {}
    if name == "run_command":
        return not a.get("job_id") and a.get("background") is not True
    if name == "run_subagent":
        if a.get("background") is True:
            return False
        try:
            from core.subagent import depth          # 懒导入：avoid cycle
            if depth() > 0:
                return False
        except Exception:                            # noqa: BLE001
            pass
        return True
    return False


def running_count_for(session_key: str, kind: str) -> int:
    """这个会话这一类还有几条在跑（配额用：后台命令 ≤3 · 子 agent ≤3）。"""
    with _lock:
        return sum(1 for j in _jobs.values()
                   if j.status == "running" and j.kind == kind
                   and j.session_key == str(session_key or ""))


def should_background(ev) -> bool:
    return bool(ev is not None and ev.is_set())


# 统一任务表：查 / 取消 / 按会话杀

def get(job_id: str) -> Optional[Job]:
    with _lock:
        return _jobs.get(str(job_id or ""))


def set_output_path(job_id: str, storage_id: str) -> str:
    """给一条已经在跑的任务补上落盘路径（子 agent 被转到后台那一刻）。

    为什么是"后来补"而不是"建表时统一给"：前台完成的任务不该写盘 ——
      父已经就地拿到了结论，那份文件没有任何读者，而它会往
      `data/runtime/tasks/` 里落一堆测试都清不掉的垃圾（全量跑完
      多出 `probe_sub/*.log`，被 `test_workflow` 的沙箱机检当场抓住）。
    """
    job = get(job_id)
    if job is None or job.output_path:
        return job.output_path if job is not None else ""
    path = output_path_for(job.id, storage_id)
    job.output_path = path
    return path


def running_for(session_key: str = "", kind: str = "") -> list[dict]:
    """还在跑的任务（可按会话 / 类别过滤）—— UI 的两格计数与面板用它。"""
    with _lock:
        items = [j for j in _jobs.values() if j.status == "running"]
    if session_key:
        items = [j for j in items if j.session_key == str(session_key)]
    if kind:
        items = [j for j in items if j.kind == kind]
    return [j.snapshot(tail=0) for j in items]


def mark_notified(job_id: str) -> bool:
    """原子 check-and-set `notified`。返回 True = 这次该发通知。"""
    with _lock:
        job = _jobs.get(str(job_id or ""))
        if job is None or job.notified:
            return False
        job.notified = True
        return True


def suppress_notification(job_id: str) -> None:
    """这条任务的结论已经被就地取走 —— 完成回报不再发。不抛。

    三个调用点共用这一处：`task(action="wait")`、`task(status/output)`
    看到它已经结束、`run_command(job_id=...)` 看到它已经结束。
    这三条路都是"模型自己把结果读走了"，再发一轮回报就是同一件事收到两遍，
    而用户看到的是一条空话（"…与前面的汇报一致"）。

    两半都要：

      · `mark_notified` —— 挡"之后再入队"；
      · `forget_task` —— 丢掉已经入队、还没起轮的那一条。

    只做后一半不够：完成钩子在另一条线程上，它可能刚把回报塞进去
    （先 `wait` 到结果，几秒后又来一轮）。只做前一半也不够：已经入队的
    那条不会因为标志位改变而自己消失。

    `task(status)` 只看到状态、没看正文也算"取走"：回报的头条就是"已完成"，
    模型已经知道了；细节它还能按 `output_file` 去读。
    """
    try:
        mark_notified(job_id)
    except Exception as exc:                         # noqa: BLE001
        print("[jobs] 标记已通知失败（%s: %s）" % (type(exc).__name__, exc))
    try:
        from core import tasks_notify              # 懒导入：它 import 本模块
        tasks_notify.forget_task(job_id)
    except Exception as exc:                         # noqa: BLE001
        print("[jobs] 丢掉待发回报失败（%s: %s）" % (type(exc).__name__, exc))


def cancel(job_id: str) -> bool:
    """按任务取消（模型 `task cancel` / 用户面板）。返回是否真的杀了。"""
    job = get(job_id)
    if job is None or job.status != "running":
        return False
    job.status = "killed"
    # 命令被取消不发完成通知 —— 取消不是完成，报完成会让用户以为跑完了；
    #   子 agent 相反：它的部分结论是有价值的，通知留给批 2 发。
    if job.kind == KIND_COMMAND:
        job.notified = True
    if job.proc is not None:
        try:
            from core.shell import _kill_tree     # 懒导入：避免环
            _kill_tree(job.proc)
        except Exception as exc:                  # noqa: BLE001
            print("[jobs] 取消失败（%s: %s）" % (type(exc).__name__, exc))
    elif job.cancel_event is not None:
        job.cancel_event.set()
    if job.done_event is not None:
        job.done_event.set()
    return True


def wait_for(job_id: str, timeout_s: float = 30.0) -> Optional[dict]:
    """阻塞等到任务结束（或超时）。返回快照；id 不存在，`None`。

    只有 `task(action="wait")` 用它 —— 模型明确说"我就在这等"的时候。
    """
    job = get(job_id)
    if job is None:
        return None
    ev = job.done_event
    if ev is not None and job.status == "running":
        ev.wait(timeout=max(0.0, float(timeout_s or 0)))
    return job.snapshot()


def kill_all(session_key: str = "") -> int:
    """杀掉（并按会话）所有还在跑的任务。`/clear` 用它 —— 视作必须停止。

    与 `clear()` 的分工：`kill_all` 真杀；`clear()` 只清记账。
    `/clear` 必须先 `kill_all` 再 `clear`，并把杀了几条说出来。
    """
    with _lock:
        items = [j for j in _jobs.values()
                 if j.status == "running"
                 and (not session_key or j.session_key == str(session_key))]
    n = 0
    for job in items:
        if cancel(job.id):
            n += 1
    return n


def poll(job_id: str) -> Optional[dict]:
    """查一条作业。返回 `None` 表示这个 id 不存在（≠"在跑"）。"""
    reap()
    with _lock:
        job = _jobs.get(job_id)
    return job.snapshot() if job is not None else None


def list_jobs() -> list[dict]:
    with _lock:
        return [j.snapshot(tail=200) for j in _jobs.values()]


def running_count() -> int:
    """还在跑的作业数。

    `/clear` 拿它出声：清表不等于停活 ——
    `clear()` 只清记账，进程照跑，而它的输出再没有人会来收
    （回读要 `job_id`，而那个号刚被清掉）。用户看到的是"清干净了"。
判据不是"要不要杀"，是"不许让它静默"：「清了记账、活还在」必须说出来。
    """
    with _lock:
        return sum(1 for j in _jobs.values() if j.status == "running")


def clear() -> int:
    """清空作业表（`/clear` 与收尾用）。只清表，不杀进程 ——
    进程该由寿限或系统收，不由我们"顺手"杀。"""
    with _lock:
        n = len(_jobs)
        _jobs.clear()
    return n


def reap() -> int:
    """收掉已结束且留够久的条目（防表无界增长）。"""
    now = time.monotonic()
    with _lock:
        dead = [k for k, j in _jobs.items()
                if j.status != "running" and (now - (j.finished or now)) > _JOB_KEEP_SECONDS]
        for k in dead:
            _jobs.pop(k, None)
    return len(dead)
