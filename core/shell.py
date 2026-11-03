# -*- coding: utf-8 -*-
"""执行命令的执行层 —— 全仓唯一碰 `subprocess` 的工具路径。

判定的那一半在 `core/command_policy.py`（纯函数、不执行）。这里只负责：
探测 bash · 清洗环境 · 限目录 · 限超时 · 收输出 · 落盘。

## 一、为什么是 `bash -c` + 绝对路径

shell 用 bash。本机可用的是 `D:\\ACDpl\\Git\\bin\\bash.exe`，
而 `bash` 不在 Windows PATH 上（`D:\\ACDpl\\Git\\cmd` 里只有 `git.exe`）
必须探测 + 绝对路径，不能写 `bash` 让 OS 去找。

### 没有 Git Bash 的机器：回落 PowerShell

`bash_path()` 为空时不再"整个工具不可用"—— Windows 自带 PowerShell，
而那台机器上"能跑命令"是底线而不是加分项。判据两条：

- 指引随真实 shell 走，不做语法翻译。`shell_kind()` 是那条判据的
  唯一出处，`core/prompt_assembler._runtime_line` 读它。翻译垫片有人试过
  并抛弃了（路径/编码错误的根源），所以命令文本一个字节不改。
- 失败要说"下一步"，不只说"没有"。PowerShell 的报错是本地化的
  （中文系统给中文），所以两种措辞都认；`&&` 是语法错不是"命令不存在"，
  单独给路线。不给路线的话，模型的下一条多半还是同一条。

已知取舍（都写在对应函数的 docstring 里）：PowerShell 下不支持
`persistent=true`（明确拒，不静默退化）、没有 profile 快照；
cwd 照常追踪。

## 二、 环境清洗做白名单，不做黑名单

判据是一条按名字模式 `/KEY|PASSWORD|SECRET|TOKEN/i` 的黑名单，
拿本仓 `.env` 的 7 个真实键名过一遍 —— `FEISHU_APP_ID` 不匹配。

黑名单必然漏（它假设了密钥的名字长得像密钥）。白名单反过来：
没写进去的都不继承，漏的方向是"某个变量没传进去、命令报错"，看得见。

而 `.env` 的载荷是实的：`load_dotenv(override=True)`，框架进程的
`os.environ` 里躺着全部凭据键，不清洗的话一条 `env` 就全进模型上下文。

机械读者：`tests/test_shell.py::test_env_whitelist_has_no_secret` ——
拿真的 `.env` 反查，任何一个键进了白名单就红。

## 三、 cwd 由框架记住 —— 不靠解析，靠问 bash

不要持久 shell，但 cwd 由框架记住。
我们不解析命令 —— 而这件事根本不需要解析：

    命令跑完之后，问那个 shell 自己在哪。

拼一小段尾巴（`printf ... "$PWD"`），让 bash 把它执行完时的目录打给我们。
比解析可靠（解析要处理 `cd a && cd b`、`(cd a; …)` 子 shell、`cd -` 一堆形状；
问 `$PWD` 一个都不用管）。

尾巴会让"命令自己调 `exit`"或含语法错误时不打标记 —— 那时**保留上一次的
目录**，不猜。见 `_split_cwd`。

## 四、 这不是沙箱（同 `core/command_policy.py` 第四节）

`cwd` 被限制在工作区里 —— 相对路径因此留在里面。
但一条绝对路径就能出去，能拦住它的只有"提及名单 + 询问"，而不是强制。
真正的解法是 OS 沙箱（bwrap/seatbelt/landlock），Windows 上没有对应物。
"""

from __future__ import annotations

import atexit
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from core.approval import command_subject
from core.i18n import t as tr
from core.i18n import tr_fmt as trf
from core.command_policy import (
    TIER_ALLOW, TIER_DENY, TIER_GUARDED, classify_command, linger_reason,
    no_ask_reason,
)

__all__ = [
    "run_command", "COMMAND_TOOL_SCHEMA",
    "bash_path", "powershell_path", "shell_kind", "clean_env", "ENV_WHITELIST",
    "DEFAULT_TIMEOUT_MS", "MAX_TIMEOUT_MS",
]

# 额度
#: 30s。这是"先量再改"量出来的，不是拍的：
#: 163 次 `run_command` 的耗时分布是
#: **中位 39ms · p90 2,932ms · 次慢 11,043ms · 只有 1 次撞满 120,006ms**。
#:120s 是从没派上用场的默认值，而它一旦被撞上就是白等两分钟。
#: 判据是「别让默认值替模型决定」：写脚本这类活大多几秒就有结论；
#: 真会慢的活由调用方显式给 `timeout_ms`（schema 里那一格就在），上限仍是 600s。
#: 别把它调回 120s 当"安全" —— 超时不杀进程（降级成后台作业，
#: 用同一个工具带 `job_id` 回读），调小的代价是"更早转后台"，不是"活没了"。
DEFAULT_TIMEOUT_MS = 30_000      # 30s
MAX_TIMEOUT_MS = 600_000         # 上限 600s —— 调用方给再多也压回来

#: 给模型看的输出预算。必须显著小于 `tool_result_max_chars`（50000） ——
#: 否则 `core/agent_loop._truncate_to_artifact` 会再切一次，而那一刀保的是头部，
#: 正好把我们保下来的尾部切掉（两把刀方向相反，合起来等于没保）。
#:
#: 16000 的判据是失败摘要的形状：测试/构建的失败段（断言 + 调用栈 + 尾部统计）
#: 常在 5k–15k 字符之间，4000 会让模型只看到尾巴、再去读一次落盘文件。
#: 仍然保尾部 + 落盘：超出的部分一个字节都没丢，回执里给路径。
_OUT_BUDGET = 16000

#: 审批面板里展示的命令上限。超了就截断并显式标注省略了多少 ——
#: 判据是"被审批的文本就是被同意的文本"：藏掉尾巴等于让用户
#: 同意他读不到的东西。
_PROMPT_COMMAND_LIMIT = 1200

#: 整条命令的硬上限。超过就拒绝，不是截断。
#:
#: 为什么必须有这一条（不是随手加的限制）：被审批物就是授权的键
#: （`subject` = 归一化后的命令原文），它要整个装进工具结果才送得到
#: 检测器，而工具结果有 `tool_result_max_chars` 那点预算（50000）。
#: 一条 200KB 的命令会把这个预算撑爆 —— 而撑爆的表现不是报错，
#: 是审批浮窗永远不出现，用户和模型一起干等。
#:宁可在入口处给一句人话。
#: 而且长到这种程度的单行命令几乎一定该写成脚本文件 ——
#: 报错里就带这句话（框架有 `create_document`/`add_block`，写文件是它的本职）。
_MAX_COMMAND_CHARS = 3000

#: bash 候选路径。顺序即优先级 —— 第一条是本机可用的那条。
_BASH_CANDIDATES: tuple[str, ...] = (
    r"D:\ACDpl\Git\bin\bash.exe",
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    r"D:\Program Files\Git\bin\bash.exe",
    "/bin/bash",
    "/usr/bin/bash",
    "/usr/local/bin/bash",
    r"C:\Windows\System32\bash.exe",   # WSL —— 排在最后，它的路径语义与 Git Bash 不同
)

# 环境变量白名单
#
# 一律小写比较（Windows 的环境变量名不区分大小写，而 `os.environ` 在
# Windows 上可能给出任意大小写）。
#
# 分组只为可读性，顺序无意义 —— 判据是"在不在集合里"，不是"排第几"。
ENV_WHITELIST: tuple[str, ...] = (
    # ① 进程能起来所必需（缺了 bash 或子进程会直接跑不动）
    "path", "pathext", "systemroot", "windir", "systemdrive",
    "comspec", "temp", "tmp",
    # ② `~` 与用户目录（bash 靠 HOME/USERPROFILE 展开 `~`）
    "home", "homedrive", "homepath", "userprofile",
    "appdata", "localappdata", "programdata",
    "programfiles", "programfiles(x86)",
    "commonprogramfiles", "commonprogramfiles(x86)",
    "allusersprofile", "public",
    # ③ 终端 / 编码 / 语言环境
    #    `pythonioencoding` 与 `pythonutf8` 是本仓自己的 GBK 教训的产物
    #    —— 子进程里跑 Python 时会用到。
    "lang", "language", "lc_all", "lc_ctype", "term", "colorterm",
    "pythonioencoding", "pythonutf8", "pythonutf8mode",
    # ④ 身份与宿主信息（都不是秘密：用户名、主机名读 `os.environ` 就有）
    "user", "username", "logname", "hostname", "shell",
    "number_of_processors", "processor_architecture",
    "processor_identifier", "processor_level", "processor_revision", "os",
    # ⑤ 虚拟环境标记 —— 不传的话 `python` 找不到本项目的 `.venv`
    "virtual_env", "conda_prefix", "conda_default_env",
)

_ENV_ALLOWED: frozenset[str] = frozenset(ENV_WHITELIST)


def clean_env() -> dict:
    """给子进程的环境 —— 白名单过滤后的 `os.environ`。

    不做"删除黑名单里的键"（那正是前面实际会漏掉 `FEISHU_APP_ID` 的形状）。
    也不在原 dict 上原地删 —— `os.environ` 是进程全局，改它等于改框架自己。
    """
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k.lower() in _ENV_ALLOWED:
            out[k] = v
    # 子进程一律说 UTF-8：Git Bash 默认就是，但 Python 子进程在 Windows 上
    # 会按控制台代码页走 —— 那正是本仓 `check_gbk_safe.py` 在管的那件事。
    out.setdefault("PYTHONIOENCODING", "utf-8")
    # `python` / `pip` 必须先解析到跑框架的这个解释器。
    #   不钉的话 PATH 上更早的那个会被用掉 —— 现场：`python -m pytest`
    #   报 `No module named pytest`，而它用的是另一个项目的旧虚拟环境。
    try:
        import sys as _sys
        _bindir = str(Path(_sys.executable).parent)
        _sep = ";" if os.name == "nt" else ":"

        def _norm(p: str) -> str:
            return os.path.normcase(p.rstrip("\\/"))

        #: 必须是**第一段**，不是"在 PATH 里出现过就行"：它已经在 PATH 里但排在
        #:   别处时，`python` 仍会解析到另一个项目那份（现场原样：`python -m pytest`
        #:   报 No module named pytest，而它跑的是旧虚拟环境）。
        for _k in list(out):
            if _k.lower() == "path":
                _parts = [p for p in out[_k].split(_sep)
                          if p and _norm(p) != _norm(_bindir)]
                out[_k] = _sep.join([_bindir] + _parts)
                break
        else:
            out["PATH"] = _bindir
    except Exception:                                    # noqa: BLE001
        pass
    return out


# bash 探测（只探一次，结果缓存）

_bash_cache: str | None = None
_bash_probed = False


def bash_path() -> str:
    """可用的 bash 绝对路径；一个都找不到，返回空串（不抛）。

    缓存首次结果：探测要碰磁盘（`exists()` × 8）而且每个会话都要用。
    失败结果也缓存：探测一次要几十微秒，而"装了 Git 之后不重启就生效"
    这件事用户其实做不到（`core/` 里的 import 期常量本来就要重启才生效）。

    两个格子的写序是承重的 —— `_bash_probed` 必须最后写。
    如果先落闩再填缓存，另一条会话（调度器允许并行）会在
    "闩已落下、缓存还没填"那个窗口里读到 `_bash_probed=True` +
    `_bash_cache=None`，拿到空路径，表现为「找不到 bash，命令执行工具
    不可用」—— 一次性的、看不出原因的失败。
整个探测先算完再落闩：看到闩为真，缓存一定已经写好了。
    """
    global _bash_cache, _bash_probed
    if _bash_probed:
        return _bash_cache or ""

    found = ""
    for cand in _BASH_CANDIDATES:
        try:
            if Path(cand).is_file():
                found = cand
                break
        except OSError:
            continue
    if not found:
        found = shutil.which("bash") or ""
    _bash_cache = found
    _bash_probed = True          # 最后写 —— 见 docstring 那段
    if not _bash_cache:
        print("[shell] 找不到 bash —— 命令执行工具将不可用。"
              f"已试过：{', '.join(_BASH_CANDIDATES)} 与 PATH。"
              f"装了 Git for Windows 之后重启框架即可。")
    return _bash_cache


# PowerShell 兜底
#
# 为什么要有它：`bash_path()` 找不到 Git Bash 时，`run_command` 此前整个
#   不可用 —— 而 Windows 自带 PowerShell。那台机器上"能跑命令"不是加分项，
#   是用户对"agent 能干活"的最低预期。
#
# 只做"换一个进程 + 换一套指引"，命令文本一个字节不改：语法翻译的垫片
#   正是 MSYS 路径与编码错误的来源。

#: PowerShell 候选路径。顺序即优先级 —— 7（`pwsh`）有 `&&`、默认 UTF-8，
#: 5.1 两样都没有，所以 7 优先；但 Windows 自带的是 5.1，它才是常态。
_POWERSHELL_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\PowerShell\7\pwsh.exe",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
)

_ps_cache: str | None = None
_ps_probed = False


def powershell_path() -> str:
    """可用的 PowerShell 绝对路径；一个都找不到返回空串（不抛）。

    缓存与写序判据与 `bash_path` 同一套（见那里的 docstring）。
    """
    global _ps_cache, _ps_probed
    if _ps_probed:
        return _ps_cache or ""
    found = ""
    for cand in _POWERSHELL_CANDIDATES:
        try:
            if Path(cand).is_file():
                found = cand
                break
        except OSError:
            continue
    if not found:
        found = shutil.which("pwsh") or shutil.which("powershell") or ""
    _ps_cache = found
    _ps_probed = True          # 最后写 —— 同 `bash_path`
    return _ps_cache


def shell_kind() -> str:
    """当前真的会跑哪个 shell —— `"bash"` / `"powershell"` / `""`。

    判据是 `_run` 会起哪个进程，不是 `os.name`：Windows 上装了 Git Bash
    时真的跑 bash，那时告诉模型"你在 PowerShell"比不说更坏 ——
    它会照着错的语法写，然后撞一墙。

    提示词那一侧只许读本函数，别自己再判一遍平台（一个口径一个地方）。
    """
    if bash_path():
        return "bash"
    if os.name == "nt" and powershell_path():
        return "powershell"
    return ""


# shell 快照（profile 里的函数 / 别名 / 选项 / PATH）
#
# 每条命令仍是新进程：启动时 source 一份快照，再 `eval` 命令。
# 快照在第一次执行命令时生成一次，退出时删除。
#
# 只拍四样：函数、别名、shell 选项、PATH。不拍导出变量 ——
#   用户的 `.bashrc` 里常有 `export API_KEY=...`，落进磁盘就等于把密钥
#   复制了一份。要跨命令传递变量，用命令自己的写法或写进脚本文件。
# 生成失败一律退回原状（命令照常跑），并在控制台说一句。

#: 每个进程一份。共用一个文件名时，谁先退出谁把别人的那份删掉 ——
#: 应用正在跑的时候，一个并行跑测试的进程就会覆盖并删除它的快照
#: （症状：那边下一次命令静默丢掉函数与别名）。PID 进文件名即可。
_SNAPSHOT_DEST = Path("data/runtime") / ("shell_snapshot_%d.sh" % os.getpid())
_SNAPSHOT_MAX_BYTES = 256 * 1024
_SNAPSHOT_TIMEOUT_S = 10.0

_SNAPSHOT_SCRIPT = r"""
SNAPSHOT_FILE="$1"
: > "$SNAPSHOT_FILE" || exit 1
if [ -f "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc" </dev/null >/dev/null 2>&1 || true
fi
{
  echo "# functions"
  declare -F 2>/dev/null | cut -d" " -f3 | head -n 1000 | while read -r __f; do
    declare -f "$__f" 2>/dev/null
  done
  echo "# options"
  shopt -p 2>/dev/null | head -n 1000
  set -o 2>/dev/null | awk '$2 == "on" {print "set -o " $1}' | head -n 1000
  echo "shopt -s expand_aliases"
  echo "# aliases"
  case "$OSTYPE" in
    msys*|cygwin*) alias 2>/dev/null | grep -v "='winpty " ;;
    *) alias 2>/dev/null ;;
  esac | sed "s/^alias //" | sed "s/^/alias -- /" | head -n 1000
  echo "# path"
  printf 'export PATH=%q\n' "$PATH"
} >> "$SNAPSHOT_FILE" 2>/dev/null
"""

_snapshot_cache: str = ""
_snapshot_done = False
#: 本进程实际写出的那一份（生成时定死；清理时不再重算相对路径）。
_snapshot_file: str = ""


def _write_snapshot(bash: str, dest: Path, *, home: str = "") -> tuple[bool, str]:
    """生成一份快照。返回 `(成功?, 失败原因)`。不抛。"""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "目录建不出来（%s）" % exc
    env = clean_env()
    if home:
        env["HOME"] = home
        env["USERPROFILE"] = home
    try:
        proc = subprocess.run(
            [bash, "--noprofile", "--norc", "-c", _SNAPSHOT_SCRIPT,
             "snapshot", str(dest)],
            cwd=str(Path.cwd()), env=env, timeout=_SNAPSHOT_TIMEOUT_S,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "超时 %d 秒" % int(_SNAPSHOT_TIMEOUT_S)
    except OSError as exc:
        return False, "%s" % exc
    if proc.returncode != 0:
        return False, "退出码 %d" % proc.returncode
    try:
        size = dest.stat().st_size
    except OSError:
        return False, "没写出文件"
    if size == 0:
        return False, "文件是空的"
    if size > _SNAPSHOT_MAX_BYTES:
        try:
            dest.unlink()
        except OSError:
            pass
        return False, "超过 %d 字节" % _SNAPSHOT_MAX_BYTES
    return True, ""


def _cleanup_snapshot() -> None:
    """删本进程真的写出来的那一份（路径在生成时定死）。

    不能在这里再算一次相对路径：进程的工作目录可能已经变了（测试会 chdir），
    那样删掉的是另一个目录里的同名文件 —— 真实现场就是把正在跑的那份删了。
    """
    target = _snapshot_file or str(_SNAPSHOT_DEST)
    try:
        Path(target).unlink()
    except OSError:
        pass


def _sweep_stale_snapshots(max_age_s: float = 86400.0) -> None:
    """清掉很久以前那些进程留下的快照（崩溃时才不会被 atexit 删）。

    只删过期的那批；本进程自己那份刚写，不在范围内。
    """
    try:
        cutoff = time.time() - max_age_s
        for path in _SNAPSHOT_DEST.parent.glob("shell_snapshot_*.sh"):
            if path.name == _SNAPSHOT_DEST.name:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    except OSError:
        pass


def ensure_snapshot() -> str:
    """第一次调用时生成快照，返回路径；不可用返回空串。每进程只试一次。"""
    global _snapshot_cache, _snapshot_done, _snapshot_file
    if _snapshot_done:
        return _snapshot_cache
    _snapshot_done = True
    bash = bash_path()
    if not bash:
        return ""
    _sweep_stale_snapshots()
    dest = _SNAPSHOT_DEST.resolve()
    ok, why = _write_snapshot(bash, dest)
    if not ok:
        print("[shell] 没生成 shell 快照（%s）—— 命令照常跑，只是没有 profile "
              "里的函数与别名。" % why)
        return ""
    _snapshot_file = str(dest)
    _snapshot_cache = str(dest)
    atexit.register(_cleanup_snapshot)
    return _snapshot_cache


# cwd

def _default_cwd() -> Path:
    """没有指定、也没有记住时的目录：默认工作区。

    口径走 `core.workspace.primary_output_dir()`（全仓唯一一处），
    并且保证这个目录存在 —— 这里曾经是裸的 `Path("workspace")`：
    安装版启动后 cwd 是数据根，而全新安装的数据根里没有 `workspace/`，
    于是 `_resolve_cwd` 的"回退到工作区"回退到了一个不存在的目录，
    `subprocess` 直接 `NotADirectoryError (WinError 267)` ——
    每一条 `run_command` 都起不来，而错误只说"命令起不来"。

    建不出来就退回进程 cwd（它一定存在）：宁可让相对路径落在数据根，
    也不把一个不存在的目录交给 `subprocess`。
    """
    try:
        from core.workspace import primary_output_dir
        p = Path(primary_output_dir())
    except Exception:                                          # noqa: BLE001
        p = Path("workspace")
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        if p.is_dir():
            return p
    except OSError:
        pass
    return Path.cwd()


def _resolve_cwd(cwd: str, wm) -> tuple[Path, str]:
    """把请求的 *cwd* 归一化成一个工作区内的目录。

    返回 `(路径, 说明)`；说明非空表示"被改过"，要如实告诉模型。

    限制在工作区里是刻意的：bash 不受 `validate_path` 约束，
    唯一能真的做到的那点约束就是 cwd —— 它让相对路径留在工作区。
    绝对路径不受它管（见模块 docstring 第四节，别读成"沙箱"）。
    """
    raw = (cwd or "").strip()
    if not raw:
        # 「框架记住」那一半：上一次命令跑完时的目录（调度器每轮投影进来）
        remembered = str(getattr(wm, "session_cwd", "") or "").strip()
        raw = remembered or str(_default_cwd())

    try:
        p = Path(raw).expanduser()
        p = (Path.cwd() / p).resolve() if not p.is_absolute() else p.resolve()
    except (OSError, RuntimeError, ValueError):
        return _default_cwd().resolve(), f"给的目录 `{raw}` 解析不了，已回退到工作区。"

    if not p.is_dir():
        return _default_cwd().resolve(), f"目录 `{raw}` 不存在，已回退到工作区。"

    guard = getattr(wm, "security", None)
    if guard is not None:
        try:
            _, level = guard.classify_path(str(p))
            from core.security import PathLevel
            if level != PathLevel.READ_WRITE:
                return _default_cwd().resolve(), (
                    f"目录 `{raw}` 不在工作区里，已回退到工作区。"
                    f"（命令的**工作目录**只允许在工作区内 —— 相对路径因此留在里面。"
                    f"要访问别处请用绝对路径。）"
                )
        except Exception:
            pass
    return p, ""


# 执行

#: 让 bash 把它结束时的目录打回来。用 SOH 包着，撞车的概率可以忽略。
_CWD_MARK = "\x01CRANNIA_CWD\x01"

#: Windows：让子进程自成一个进程组，超时才杀得掉整棵树
#: （`subprocess.run` 的 timeout 只杀直接子进程，孙进程会活下来）。
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _wrap(command: str, snapshot: str = "") -> str:
    """给命令拼上"回报 cwd + 保留退出码"的尾巴（见模块 docstring 第三节）。

    *snapshot* 非空时，先在同一个 shell 里 source 它，再用 `eval` 执行命令 ——
    `eval` 是必需的：`bash -c` 会把整段脚本先解析完，source 进来的别名
    在同一遍解析里不会展开，要第二遍解析才生效。

    用 `pwd -W` 而不是 `$PWD` —— 实际两者给出的不是同一种
    路径：Git Bash 的 `$PWD` 是 MSYS 写法（`/d/work/...`），
    而 Python 的 `Path("/d/work/...")` 在 Windows 上解析成 `D:\\d\\work\\...`
    —— 一个不存在的目录，于是"记住的 cwd"下一轮必然回退，功能静默失效。
    `pwd -W` 是 MSYS 给 Git Bash 加的选项，直接给 `D:/work/...`。

    `-W` 不是 POSIX 的（Linux 上没有），所以 `|| pwd` 兜底 ——
    在 Linux 上 `pwd -W` 报错并被 `2>/dev/null` 吞掉，走 `pwd`。
    于是**一个在 Windows 上却报 POSIX 路径的 bash（WSL）会落到"目录不存在"
回退工作区并出声**（`_resolve_cwd` 的说明字段）。方向是安全的。

    标记里只许出现 ASCII —— 路径先转成十六进制再打印。
    原因不是好看：这条尾巴和命令输出走同一个流，而流的编码是靠开头一小段
    判出来的。路径里一有中文（本仓的工作区就叫 `… - 副本`），那几段 UTF-8 字节
    就混进了一个可能是 GBK 的流，整段被判成 cp1252，命令的中文输出全部乱码。
    十六进制把自己压成 ASCII，判定只看命令自己的输出。
    残余：UTF-16 输出（带 BOM 的那类）仍会把这段 ASCII 尾巴解成乱码 ——
    此时 cwd 不更新（回退上一次），尾部多几个字符。
    """
    tail = ("__crannia_rc=$?\n"
            f"printf '\\n{_CWD_MARK}%s\\n' "
            "\"$(printf '%s' \"$(pwd -W 2>/dev/null || pwd)\" "
            "| od -An -v -tx1 | tr -d ' \\n')\"\n"
            "exit $__crannia_rc\n")
    body = command.rstrip("\n")
    if snapshot:
        return ("source %s 2>/dev/null || true\n" % shlex.quote(snapshot)
                + "eval %s\n" % shlex.quote(body)
                + tail)
    return body + "\n" + tail


def _wrap_ps(command: str) -> str:
    """PowerShell 版：UTF-8 输出 + 原样退出码 + cwd（十六进制）。

    ## 与 `_wrap` 的三处不同（每一处都是实际逼出来的）

    ① 控制符不进命令行。`_CWD_MARK` 里有两个 `\\x01`，经
       `CreateProcess` 的命令行解析会被吃掉/改写。所以标记在 PS 里现拼
       （`[char]1`），Python 侧仍然按同一个串找。
    ② 退出码要分两种情况。`$LASTEXITCODE` 只对外部程序有意义；
       纯 cmdlet 的成败在 `$?` 里，而那时 `$LASTEXITCODE` 是 `$null`
       （不是 0）—— 拿它当真会把"成功"报成上一条命令的退出码。
    ③ UTF-8 只覆盖运行期。语法错误发生在解析期，那两行赋值还没跑，
       报错按控制台编码出（中文 Windows = GBK）。这不是这里能修的 ——
       但 `core/jobs._Sink` 的候选编码里有 `cp936`，读端解得对，
       所以别为它加特判。

    ## 已知取舍

    PowerShell 下不支持 `persistent=true`（入口处明确拒绝，不静默退化），
    也没有 bash 那份 profile 快照。cwd 照常追踪 —— "记住目录"与
    "环境变量活多久"是两件事（别家也这么分：目录持久、变量不持久）。
    """
    # `$?` 必须先抓：`$__crannia_rc = $LASTEXITCODE` 本身就是一条命令，
    #   它成功执行之后 `$?` 变成 True —— 下一行再读 `$?` 读到的是"赋值成功了"，
    #   于是每一条 cmdlet 失败都会被报成 rc=0（`nosuchcmd_xyz`
    #   明明报 `is not recognized`，退出码却是 0）。这是把失败报成成功的形状。
    tail = (
        "$__crannia_ok = $?\n"
        "$__crannia_rc = $LASTEXITCODE\n"
        "if ($null -eq $__crannia_rc) { $__crannia_rc = if ($__crannia_ok) { 0 } else { 1 } }\n"
        "$__crannia_cwd = (Get-Location).ProviderPath\n"
        "[Console]::Out.Write([char]1 + 'CRANNIA_CWD' + [char]1)\n"
        "[Console]::Out.Write(([BitConverter]::ToString("
        "[Text.Encoding]::UTF8.GetBytes($__crannia_cwd)) -replace '-','').ToLower())\n"
        "[Console]::Out.Write(\"`n\")\n"
        "exit $__crannia_rc\n"
    )
    head = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
            "$OutputEncoding = [Text.Encoding]::UTF8\n")
    return head + command.rstrip("\n") + "\n" + tail


def _split_cwd(stdout: str, fallback: str) -> tuple[str, str]:
    """从 stdout 里摘出 cwd 标记。返回 `(去标记后的 stdout, cwd)`。

    取最后一次出现 —— 命令自己也可能打印出这个串（虽然概率极低）。
    找不到标记（命令 `exit` 了 / 语法错误），保留上一次的目录，不猜。
    """
    idx = stdout.rfind(_CWD_MARK)
    if idx < 0:
        return stdout, fallback
    tail = stdout[idx + len(_CWD_MARK):]
    hex_text = tail.split("\n", 1)[0].strip()
    # 连 `\r` 一起剥：PowerShell 的 `[Console]::Out` 写 CRLF，只剥 `\n`
    #   会在回执末尾留一个裸 `\r`。bash 那条路本来就没有 `\r`，对它一个字节不变。
    cleaned = stdout[:idx].rstrip("\r\n")
    new_cwd = _cwd_from_hex(hex_text)
    if not new_cwd:
        return cleaned, fallback
    return cleaned, new_cwd


def _cwd_from_hex(hex_text: str) -> str:
    """把标记里的十六进制还原成路径。解不出来就返回空串（不猜）。

    路径的字节由 bash 的 `pwd` 给出，编码随那个 bash 自己（Git Bash 是 UTF-8）——
    所以先按 UTF-8，再按本地代码页。`od` 不可用时这段是空的，回退上一次目录。
    """
    import locale as _locale

    text = (hex_text or "").strip()
    if not text or len(text) % 2:
        return ""
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return ""
    try:
        cands = ["utf-8", _locale.getpreferredencoding(False) or "", "cp936"]
    except Exception:                                  # noqa: BLE001
        cands = ["utf-8", "cp936"]
    for enc in cands:
        if not enc:
            continue
        try:
            return raw.decode(enc).strip()
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀掉整棵进程树（best-effort）。

    Windows 上 `proc.kill()` 只结束直接子进程；`taskkill /T` 才带子进程。
    拿不到 `taskkill` 就退回 `kill()` —— 降级要出声。
    """
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
                return
            except Exception as exc:
                print(f"[shell] taskkill 失败（{type(exc).__name__}: {exc}）"
                      f" —— 退回单进程 kill，孙进程可能残留")
        else:
            print("[shell] 找不到 taskkill —— 退回单进程 kill，孙进程可能残留")
    try:
        proc.kill()
    except Exception as exc:
        print(f"[shell] kill 也失败了（{type(exc).__name__}: {exc}）")


def _spill(payload: str) -> str:
    """把完整输出落盘，返回给模型照着敲的路径（正斜杠）。

    目录用 `core/agent_loop._resolve_tool_output_dir()`（不另抄一份路径），
    因为那个目录在 `core/security.py::DEFAULT_FRAMEWORK_READABLE_DIRS` 里是
    READ_ONLY，agent 真能读回来。写到别处就是"把丢失伪装成完整"。
    """
    from core.agent_loop import _resolve_tool_output_dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = _resolve_tool_output_dir() / f"{ts}_run_command.txt"
    out_path.write_text(payload, encoding="utf-8")
    return str(out_path).replace("\\", "/")


def _clip(text: str, budget: int) -> tuple[str, str]:
    """按预算裁剪，保尾部。

    返回 `(给模型看的, 落盘指针或空串)`。

    为什么保尾不保头：命令输出里要命的信息在结尾（报错、退出前的状态）。
      代价是长列表的开头看不见 —— 所以必须 spill，且指针要真能取回来。
    指针刻意不用 `<persistent_output>` 标签：工具结果整段会被
      `sanitize_text` 处理，而那个标签在 `RESERVED_TAGS` 里，会被转义成
      字面量，模型看到的是一串带 `&lt;` 的噪声。一句人话足够。
    """
    if len(text) <= budget:
        return text, ""
    path = _spill(text)
    head_note = f"（输出共 {len(text):,} 字符，只保留下面的结尾部分）"
    return (f"{head_note}\n{text[-budget:]}\n"
            f"（完整输出在 `{path}` —— 路径**可读**，用 `parse_txt` 取回。）"), path


#: Windows 上不存在的常见命令 → 该用什么。
#:
#: 判据来自模型连试 3 次 `python3` 才换 `python` ——
#: 每一次都是一整个 LLM 往返。
#: 修法是即时提示，不是往提示词里加一句常驻：后者每条请求都付费，
#:   而这件事几天才撞一次（税要付在付得起的地方）。
#:
#: 提示要回答"撞了怎么做"，不只说"没有"：编译/运行时缺了，
#:   模型的下一条命令通常就是"再换一个名字撞一次"。一句"要装 / 先问用户 /
#:   装不上就直说"能省掉整串往返，也能挡住"没编译过却说编译过了"。
def _build_hint(name: str) -> str:
    return ("本机没有 `%s` —— 编译或构建得先装工具链"
            "（Windows：`winget install` 装 MSYS2 / LLVM，或 VS Build Tools；"
            "Linux：`apt install build-essential`）。★ **装之前先问用户**；装不上就直说，"
            "别把没编译过的说成编译过。" % name)


def _runtime_hint(name: str) -> str:
    return ("本机没有 `%s` —— 这条语言运行时不在本机。可以先问用户装不装"
            "（`winget` / `apt` / `brew` 这类包管理器都能装，装之前要他点头），"
            "或换一条本机已有的路；★ **别假装跑过**。" % name)


_NOT_FOUND_HINTS = (
    ("python3", "本机没有 `python3` —— 用 `python`"),
    ("pip3", "本机没有 `pip3` —— 用 `python -m pip`"),
    ("pip", "本机没有 `pip` —— 用 `python -m pip`"),
    ("gcc", _build_hint("gcc")),
    ("g++", _build_hint("g++")),
    ("cc", _build_hint("cc")),
    ("clang", _build_hint("clang")),
    ("clang++", _build_hint("clang++")),
    ("make", _build_hint("make")),
    ("cmake", _build_hint("cmake")),
    ("ninja", _build_hint("ninja")),
    ("meson", _build_hint("meson")),
    ("go", _runtime_hint("go")),
    ("rustc", _runtime_hint("rustc")),
    ("cargo", _runtime_hint("cargo")),
    ("javac", _runtime_hint("javac")),
    ("java", _runtime_hint("java")),
    ("dotnet", _runtime_hint("dotnet")),
    ("ruby", _runtime_hint("ruby")),
    ("php", _runtime_hint("php")),
    ("lua", _runtime_hint("lua")),
    ("node", _runtime_hint("node")),
    ("npm", _runtime_hint("npm")),
    ("tsc", _runtime_hint("tsc")),
    ("git", "本机没有 `git` —— Windows 装 Git for Windows（它同时带上 bash）。"),
)

_HINT_BY_NAME = dict(_NOT_FOUND_HINTS)

#: 从"找不到"那句话里抠出缺的那个命令。三种壳各一条。
#: 不拿命令原文去猜：`make` 把 `gcc` 弄丢时，命令里一个字都看不到 `gcc`，
#:   而报错那行写着它 —— 报错才是证据。
_MISSING_BASH_RE = re.compile(r"(?:^|\n)[^\n:]*:\s*([A-Za-z0-9_+.-]+):\s*command not found")
_MISSING_CMD_RE = re.compile(r"'([^']+)' is not recognized as an internal")
#: PowerShell：`The term 'X' is not recognized as the name of a cmdlet`。
#: 这句是本地化的 —— 中文系统给「无法将"X"项识别为 cmdlet」，
#:   两种都认；都不中时退回"命令里的词"（老兜底）。
_MISSING_PS_RE = re.compile(
    r"The term '([^']+)' is not recognized as the name of a cmdlet")
_MISSING_PS_ZH_RE = re.compile(r"无法将[“\"]([^”\"]+)[”\"]项识别为")

#: `&&` 在 PowerShell 5.1 上是语法错误，不是"命令不存在"。
#: 这是"agent 大战 powershell"最常见的形状：模型默认按 bash 串联写，
#:   5.1 直接拒绝，而报错读起来像"命令坏了"。所以单独认、单独说。
_NOT_SEPARATOR_RE = re.compile(r"is not a valid statement separator", re.I)
_NOT_SEPARATOR_ZH = "不是此版本中的有效语句分隔符"

#: PowerShell 的其它语法错。`ParserError` 是 5.1 与 7 都给的分类名。
#: 命中的回执给一条通用出路：先查官方文档再写 ——
#: 站点写死在回执里（不是提示词里）：它只在真撞上时出现，零常驻成本，
#: 而 `learn.microsoft.com` 本来就在预批准域名表里，查它不会弹审批。
_PS_PARSE_ERROR_RE = re.compile(r"ParserError|Unexpected token", re.I)
_PS_DOCS = "learn.microsoft.com/powershell"


def _add_not_found_hint(result: dict, command: str) -> None:
    """失败且是"命令不存在"时，就地补一句"该用什么"。改的是传进来的那个 dict。

    只在真的没找到时才加 —— 判据是 `stderr` 里那两句
    （bash 的 `command not found` 与 cmd 的 `is not recognized as an internal`），
    不是"命令里出现了 `python3`"（那会把 `python3 --version` 这种成功的一起标上）。
    放独立的 `hint` 字段，不并进 `stderr` ——
    `stderr` 是命令的输出，掺进框架的话就是"被审计的不是被执行的那条"
    （同 `core/security.py::record_command` 那条判据）。
    """
    if result.get("success"):
        return
    raw_err = result.get("stderr") or ""
    err = raw_err.lower()
    if ("command not found" not in err
            and "is not recognized as an internal" not in err
            and "is not recognized as the name of a cmdlet" not in err
            and "项识别为" not in raw_err):
        return
    # 先看报错点名了谁 —— 那比"命令原文里出现过谁"准：
    #   `make` / `./configure` 把 gcc 弄丢时，命令里根本没有 gcc。
    name = ""
    for rx in (_MISSING_BASH_RE, _MISSING_CMD_RE,
               _MISSING_PS_RE, _MISSING_PS_ZH_RE):
        m = rx.search(raw_err)
        if m:
            name = m.group(1).lower()
            break
    # 报错认不出（别的壳 / 别的措辞），退回命令里的词（老行为）。
    #   按词比，不按子串：`cc` 这种短名字不能用 `in` 去碰运气。
    if not name:
        for tok in re.split(r"[\s;|&()<>]+", command or ""):
            tok = tok.strip().lower()
            if tok in _HINT_BY_NAME:
                name = tok
                break
            base = tok.split("-")[0]
            if base in _HINT_BY_NAME:
                name = base
                break
    advice = _HINT_BY_NAME.get(name)
    if advice:
        result["hint"] = advice

    # PowerShell 下再加一句路线 —— 这是"agent 大战 powershell"的解药。
    #   最常见的失败是：模型照着 bash 语法写 → 报错 → 再发一次同样的。
    #   报错回执里说清"现在是什么 shell、该往哪走"，比往提示词里加常驻句子便宜
    #   （那句话每条请求都付费，而这条只在真撞上时出现）。
    if shell_kind() == "powershell":
        line = ("这是 PowerShell 的报错：`%s` 在这里不存在。要么换成 PowerShell 的写法"
                "（`ls`→`Get-ChildItem`、`cat`→`Get-Content`、`rm`→`Remove-Item`），"
                "要么换专用工具（`read_file` / `glob` / `grep` / `write_file`）。"
                "别把同一条再发一次 —— 再发一次还是这个结果。"
                % (name or "那个命令"))
        result["hint"] = ((advice + " ") if advice else "") + line


def _add_syntax_hint(result: dict) -> None:
    """`&&` 这类"这个 shell 不认"的语法错 —— 单独说，别混进"命令不存在"。

    PowerShell 5.1 没有 `&&`（7 才有）。模型默认按 bash 串联写，撞的是
    语法错误：报错读起来像"命令坏了"，而真正的解法是换个分隔符。
    不给路线的话，下一条它多半还是 `&&`。
    """
    if result.get("success"):
        return
    err = result.get("stderr") or ""
    prev = result.get("hint") or ""

    if _NOT_SEPARATOR_RE.search(err) or _NOT_SEPARATOR_ZH in err:
        line = ("这个 shell 不认 `&&`：PowerShell 5.1 没有它（7 才有）。"
                "用 `;` 把两条命令分开，或者分两次调用。")
        result["hint"] = (prev + " " + line).strip() if prev else line
        return

    # 其它语法错：给一条通用出路。这一句同时覆盖"写法不对"与"版本差异"，
    #   而"再试一次同一种写法"没有任何可能让它变成对的。
    if shell_kind() == "powershell" and _PS_PARSE_ERROR_RE.search(err):
        line = ("这是 PowerShell 的语法错误。先确认写法是 PowerShell 的"
                "（环境变量 `$env:NAME`；5.1 没有 `&&`，用 `;`）。"
                "不确定的语法先查官方文档 `" + _PS_DOCS + "` 再写，"
                "别连着试同一种写法。")
        result["hint"] = (prev + " " + line).strip() if prev else line


def _add_shell_hint(result: dict, command: str) -> None:
    """把"这个 shell 说了什么"翻成"下一步怎么做"。

    两个判据分开写、各管各的形状：语法错（`&&`）与"命令不存在"是两件事，
    合并成一个函数会让"命中哪一个"变得靠猜。
    """
    _add_syntax_hint(result)
    _add_not_found_hint(result, command)


def _session_key_of_current_call() -> str:
    """当前这次工具调用属于哪个会话。

    `handler(**args)` 拿不到 `session_id`（`core/agent_loop.py:1042` 那条注释
    写着的、同一个理由），所以只能从请求快照那个 `ContextVar` 上取 ——
    与 `core/ice/reveal.py::reveal_handler` 用的是同一个口（一条判据一处）。

    取不到就返回空串，不登记（而不是"登记到一个空键上"）：
    那种情况下 `/stop` 掐不到这条命令，但也不会掐错别人的。
    方向是对的 —— 误差朝"少掐一次"倒，而不是"掐错一个进程"。
    """
    try:
        from core.request_snapshot import current_session_id
        return str(current_session_id() or "")
    except Exception:                             # noqa: BLE001
        return ""


def _background_receipt(job, *, by_user: bool = False, timed_out: bool = False) -> dict:
    """转后台后交给模型的一行回执：只说"已转后台 + 去哪儿看输出"，不带正文。

    模型拿不到命令输出 —— 只有 task id + 输出文件路径 + 一句说明。
    完成时的详情走另一条路（`<background_task>` 通知），不在这里塞。
    """
    from core import jobs as _jobs
    # 只有通知链真的接通了，回执才敢承诺"会收到回报"（标签不许说谎）。
    try:
        from core.tasks_notify import is_installed as _notify_on
        _promise = "完成时会收到一条后台任务回报；" if _notify_on() else ""
    except Exception:                                  # noqa: BLE001
        _promise = ""
    if timed_out:
        note = (
            f"命令超过 {job.elapsed_ms() / 1000:.0f} 秒还没结束，**已经转到后台继续跑**"
            f"（没有杀它）。用 `run_command(job_id=\"{job.id}\")` 查它，"
            f"或者先做别的事 —— 它最多活 {_jobs.JOB_MAX_LIFETIME_MS // 60000} 分钟。"
            f"⚠️ 后台任务里的 `cd` **不粘**（回读不到 cwd），"
            f"要切目录请在**同一条命令里** `cd X && ...`。")
    elif by_user:
        note = (f"命令已按你的请求转到后台继续跑（没有杀它），任务 ID：{job.id}。"
                f"{_promise}用 `task(task_id=\"{job.id}\", action=\"wait\")` 等它结束、"
                f"`action=\"cancel\"` 取消；`run_command(job_id=\"{job.id}\")` 看输出。"
                f"别在结果回来之前假定它完成了。")
    else:
        note = (f"命令已在后台运行，任务 ID：{job.id}；不会占住这一轮。"
                f"{_promise}用 `task(task_id=\"{job.id}\", action=\"wait\")` 等它结束、"
                f"`action=\"cancel\"` 取消；`run_command(job_id=\"{job.id}\")` 看输出。"
                f"别在结果回来之前假定它完成了。")
    return {
        "success": True,          # 不是失败：命令在跑，只是不在这一轮里等
        "status": "running",
        "task_id": job.id,
        "job_id": job.id,         # 兼容旧调用（模型已经会用 job_id）
        "pid": job.pid,
        "output_file": job.output_path,
        "timed_out": bool(timed_out),
        "backgrounded_by_user": bool(by_user),
        "note": note,
    }


def run_quiet(argv: list, cwd, timeout_s: float = 30.0) -> tuple[int, str]:
    """跑一条框架自己的命令：不经审批、不落审计、不做输出裁剪。

    只给框架内部的确定性操作（今天只有 `core/worktree.py` 的 git 调用）——
    参数由代码拼、不含模型文本。工具路径不许走到这里，那是 `run_command` 的活。
    """
    try:
        p = subprocess.run(argv, cwd=str(cwd), env=clean_env(),
                           capture_output=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:    # noqa: BLE001
        return -1, "%s: %s" % (type(exc).__name__, exc)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, (out + err).strip()


def _run(command: str, work_dir: Path, timeout_ms: int, *,
         background: bool = False, description: str = "",
         storage_id: str = "") -> dict:
    """真的起进程。只由 `run_command` 调用（判定已经做完了）。"""
    kind = shell_kind()
    if kind == "bash":
        argv = [bash_path(), "-c", _wrap(command, ensure_snapshot())]
    elif kind == "powershell":
        # `-NonInteractive` 让"等输入"的命令立刻失败而不是挂到超时；
        #   stdin 那边也是 DEVNULL —— 两道都堵上。
        argv = [powershell_path(), "-NoProfile", "-NoLogo",
                "-NonInteractive", "-Command", _wrap_ps(command)]
    else:
        return {
            "success": False,
            "error": ("找不到可用的 shell（bash 与 PowerShell 都没有），命令没有执行。"
                      "Windows 上装 Git for Windows，或确认系统自带的 powershell.exe 在。"),
        }

    ms = int(timeout_ms or 0) or DEFAULT_TIMEOUT_MS
    ms = max(1000, min(ms, MAX_TIMEOUT_MS))
    secs = ms / 1000.0

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(work_dir),
            env=clean_env(),
            stdin=subprocess.DEVNULL,     # 读输入的命令应当立刻拿到 EOF，不是挂着
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        return {"success": False,
                "error": f"命令起不来（{type(exc).__name__}: {exc}）"}

    # 把这条命令登记到会话上，好让 `/stop` 能真掐它
    # 只登记子进程（`run_command` 是唯一跑得久的工具，也是唯一杀得掉的
    #   —— 其他工具是进程内的同步 Python，Python 杀不掉线程，登记了也没用）。
    # 必须在 `finally` 里摘 —— 不摘的话，下一次 `/stop` 会去杀一个
    #   已经结束的 PID，而 PID 会被复用，杀错进程。
    _cancel_key = _session_key_of_current_call()
    if _cancel_key:
        from core import cancellation as _cancel
        _cancel.register_process(_cancel_key, proc, command)

    # 读线程从进程一起来就在跑
    # 为什么不用 `proc.communicate(timeout=)`：它自己有内部读线程，超时路径
    # 会把管道里的数据读掉 —— 之后再挂读线程什么都拿不到（实际症状是
    # "后台作业跑完了但输出是空的"）。`communicate()` 与手动 `read()` 不许混用。
    # 这里是"全自己读"：读线程先起，`wait(timeout=)` 只负责超时。
    from core import jobs

    # 用户 Ctrl+B 的落点：事件挂在这一个 proc 上（不是会话布尔）。
    _fg_event = jobs.note_foreground(_cancel_key, proc) if _cancel_key else None

    out_sink, err_sink = jobs._Sink(), jobs._Sink()

    def _pump(stream, sink) -> None:
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                sink.write(chunk)
        except Exception:
            pass
        finally:
            # 流结束必须显式收尾：不足 `_SNIFF_BYTES` 的尾巴还攒在
            #   `_pending` 里，不 finish 就会凭空少掉最后几个字节。
            sink.finish()

    readers = []
    for stream, sink in ((proc.stdout, out_sink), (proc.stderr, err_sink)):
        if stream is not None:
            th = threading.Thread(target=_pump, args=(stream, sink), daemon=True)
            th.start()
            readers.append(th)

    # `try/finally` 是判据的一部分，不是风格 —— 登记了就必须摘：
    #   这条路上有三个出口（超时转后台 / 正常完成 / 任意异常），
    #   逐个去写 `unregister` 就是 *a list of places* 的形状。
    try:
        timed_out = False

        def _to_background(*, by_user: bool = False, timed_out_: bool = False) -> dict:
            """把这一个 proc 挂进统一任务表（三条来源共用同一个出口）。"""
            job = jobs.start(
                proc, command, str(work_dir), out_sink, err_sink,
                session_key=_cancel_key, storage_id=storage_id or _cancel_key,
                description=description,
            )
            why = ("用户转后台" if by_user else
                   ("超时转后台" if timed_out_ else "按请求直接转后台"))
            print(f"[shell] {why}：{job.id}（上限 "
                  f"{jobs.JOB_MAX_LIFETIME_MS // 60000} 分钟）")
            # 产生后台任务的这一刻，把 `task` 工具挂进这个会话（按需发现；
            #   只追加、不改前缀，`K5`）。回执里点名它，模型不用猜。
            if _cancel_key:
                try:
                    from bus.registry import note_discovered
                    note_discovered(_cancel_key, "task")
                except Exception:                     # noqa: BLE001
                    pass
            return _background_receipt(job, by_user=by_user, timed_out=timed_out_)

        # ① 模型显式要求后台（`background=true`）
        if background:
            return _to_background()

        # ② 前台等待：0.2 秒一跳，好让用户 Ctrl+B 能插进来
        # 不能再用一次 `proc.wait(timeout=secs)`：那样用户在等待期间按 Ctrl+B
        #    要等整条超时才被看见（而"转后台"的全部意义就是立刻不等）。
        deadline = time.monotonic() + secs
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if jobs.should_background(_fg_event):
                    return _to_background(by_user=True)
                if time.monotonic() >= deadline:
                    return _to_background(timed_out_=True)

        # 正常完成：等读线程把管道排空（EOF 之后它们自然退出）
        for th in readers:
            th.join(timeout=5)
        stdout, stderr = out_sink.text(), err_sink.text()
        return_code = proc.returncode

        # cwd 回读（见模块 docstring 第三节）
        prev = str(work_dir)
        stdout, new_cwd = _split_cwd(stdout, prev)

        result: dict = {
            "success": (return_code == 0) and not timed_out,
            "exit_code": return_code,
            "cwd": new_cwd,
            "stdout": stdout,
            "stderr": stderr,
        }
        # 非 UTF-8 的输出要报出来（判据同 `jobs.Job.to_dict`）：
        #   静默换一种编码解码，会让乱码看起来像"程序本来就输出这个"。
        _encs = sorted({e for e in (out_sink.encoding, err_sink.encoding)
                        if e and e not in ("utf-8", "utf-8-sig")})
        if _encs:
            result["output_encoding"] = _encs
        # 被掐掉的那条要如实说（标签不许说谎）。
        #   判据是取消标志 + 非零退出码两个一起 —— 只看标志会把
        #   "按了停但命令刚好正常跑完"也标成被掐。
        if return_code != 0 and _cancel_key and _cancel.is_cancelled(_cancel_key):
            result["interrupted"] = True
            result["note"] = ("这条命令**被用户中断了**（进程树已被杀）。"
                              "已产生的那部分输出在 stdout/stderr 里，"
                              "但它可能只做了一半 —— ⚠️ **不要**假定它完成了。")
        # 失败时补一句"下一步怎么做"（即时，零常驻成本）：
        #   命令不存在 → 该换哪个名字；`&&` 语法错 → 该换哪个分隔符。
        _add_shell_hint(result, command)
        return result
    finally:
        if _cancel_key:
            _cancel.unregister_process(_cancel_key, proc)
            jobs.clear_foreground(_cancel_key, proc)


# 工具入口

#: 「这条命令本来属于危险档，被开关降级了」那句话。
#: 放在模块级是为了让扫描器（`scripts/i18n_scan.py` 的 `names`）能单独取它 ——
#:   `run_command` 的其余字符串全是给模型看的回执，整函数扫会搅进一堆不该翻的。
_RELAX_NOTE = (
    "⚠️ 注：这条命令本来属于**危险档**（每次都问、不能记住）。"
    "「关闭安全询问」开关开着，所以它被降级成普通档 —— "
    "批准之后可以「本次对话内不再询问此命令」。"
)


def _approval_prompt(subject: str, work_dir: Path, reason: str,
                     relaxed_from: str = "") -> str:
    """文本通道看到的正文。命令原文必须在这里 —— 用户要批的就是它。"""
    shown = subject
    if len(shown) > _PROMPT_COMMAND_LIMIT:
        omitted = len(shown) - _PROMPT_COMMAND_LIMIT
        # 千分位先格式化好再交给 `tr_fmt` —— 它的占位符只按名字填值，
        #   `{n:,}` 那种格式说明会被当成名字的一部分（`:,` 不进值）。
        note = trf("...（还有 {n} 字符没有显示 —— "
                   "⚠️ 下面同意的是**完整的那条**，不只是显示出来的这段）",
                   f"{omitted:,}")
        shown = f"{shown[:_PROMPT_COMMAND_LIMIT]}\n" + note
    lines = [tr("Crannia 想执行以下命令："), f"  {shown}",
             trf("（工作目录：{p}）", work_dir)]
    if reason:
        lines.append(f"⚠️ {reason}")
    if relaxed_from:
        lines.append(relaxed_from)
    return "\n".join(lines)


def _cmd_auto_note() -> str:
    """命令审批载荷末尾那句「自动模式为什么没替你答」。

    为什么要它：自动档下用户仍可能被要求手批，而这个弹窗上
    看不出是 (a) 手动档 / (b) 熔断退回 / (c) 判了安全却不知道怎么放行
    —— 三种的处置完全不同（(b)(c) 是自动模式自己的问题，(a) 不是）。
把 `agent_loop` 那一格带出来（同一条口径，不在这里重算一遍）。

    延迟 import：`core.shell` 与 `core.agent_loop` 本来没有边，
    模块级 import 会造一条新的（本仓被环 import 咬过不止一次）。
    拿不到，空串（读不到 != 有话说 —— 绝不编一句话出来）。
    """
    try:
        from core.agent_loop import auto_note_suffix
        return auto_note_suffix()
    except Exception:                              # noqa: BLE001
        return ""


#: 把命令拆成"可能是一个路径"的若干片段。只按标点切，不做语法分析。
_CMD_TOKEN_SPLIT = re.compile(r"""[\s"'`=,;|&<>(){}\[\]]+""")
#: 命令里 `cd <某处>` 的目标 —— 那些目录也是"相对路径可能相对谁"的候选。
_CMD_CD = re.compile(r"""(?:^|&&|;|\|\||\|)\s*cd\s+("[^"]+"|'[^']+'|[^\s;&|]+)""")
#: MSYS 写法 `/d/foo` —— Windows 上 `Path` 会把它解析成 `D:\d\foo`。
_CMD_MSYS_DRIVE = re.compile(r"^/([A-Za-z])/")


def _note_command_touches(command: str, cwd, wm) -> None:
    """命令里点名的、工作区内的文件 —— 记进「本轮改动文件」并留一份改前。

    `run_command` 不走 `validate_path`，所以这条路上一个写都不会被记；
    而模型改大文件时只能走它（`read_file` 有 60k 上限，抄不全就过不了
    「写前必读」）。不补这一笔，用户看到的是"文件改了，列表里什么都没有"，
    而且也没有快照可回滚。

    不做语法分析（本仓不写命令解析器）：把命令切成片段，凡是"解析得开、
    真的是工作区里的文件"就算 —— 方向选多报。多报的代价是列表里多一个
    文件，漏报的代价是改动对用户不可见。
    """
    try:
        from core.request_snapshot import current_session_id
        sid = str(current_session_id() or "")
    except Exception:                                    # noqa: BLE001
        sid = ""
    if not sid or wm is None or not command:
        return
    try:
        roots = []
        _primary = getattr(wm, "primary_path", "") or ""
        if _primary:
            roots.append(Path(_primary))
        for _g in (getattr(wm, "workspaces", None) or []):
            _p = getattr(_g, "path", "") or ""
            if _p:
                roots.append(Path(_p))
        roots = [r.resolve() for r in roots if r]
        if not roots:
            return

        bases = [Path(cwd)] if cwd else []
        for _m in _CMD_CD.finditer(command):
            _raw_dir = _m.group(1).strip().strip("\"'")
            if not _raw_dir:
                continue
            if _CMD_MSYS_DRIVE.match(_raw_dir):
                _raw_dir = _CMD_MSYS_DRIVE.sub(lambda mm: mm.group(1).upper() + ":/", _raw_dir)
            try:
                _p = Path(_raw_dir)
                bases.append(_p if _p.is_absolute() else (Path(cwd) / _p if cwd else _p))
            except Exception:                            # noqa: BLE001
                continue

        from core.file_history import snapshot_before
        from core.touched_files import note
        seen = set()
        for tok in _CMD_TOKEN_SPLIT.split(command):
            tok = tok.strip().strip("\"'").strip("=")
            if not tok or len(tok) > 400:
                continue
            if "/" not in tok and "\\" not in tok and "." not in tok:
                continue
            for base in bases:
                try:
                    cand = (base / tok).resolve()
                except Exception:                        # noqa: BLE001
                    continue
                if not cand.is_file():
                    continue
                if not any(cand == r or r in cand.parents for r in roots):
                    continue
                key = str(cand)
                if key in seen:
                    break
                seen.add(key)
                if note(sid, key):
                    snapshot_before(sid, key)
                break
    except Exception as exc:                              # noqa: BLE001
        # 记账失败绝不许拦住一条要跑的命令（同 `touched_files.note` 的规矩）。
        print("[touched] 命令行改动记账失败（不影响执行）：%s: %s"
              % (type(exc).__name__, exc))


def run_command(command: str = "", cwd: str = "", timeout_ms: int = 0,
                job_id: str = "", description: str = "",
                background: bool = False, persistent: bool = False) -> dict:
    """在 bash 里执行一条命令。本仓唯一能执行任意命令的工具。

    三档判定在 `core.command_policy.classify_command`（纯函数，有独立测试）：
    `deny` 直接拒（不问）、`guarded` 走危险档、`allow` 走普通档（问，但可以记住）。
    `guarded` 与 `allow` 共用同一张审批表；区别是 `guarded` 拿不到前缀规则
    （只能整条精确匹配），且提示语正文会说清为什么危险。

    工具自己不做判定之外的事 —— 它不解析命令、不改写命令。

    给了 `job_id` 就只查不跑 —— 查一条被降级到后台的作业。
    超时的命令不再被杀，而是转后台，用这个口回来看它。

    `persistent=true`，在这条会话的常驻 shell 里跑（`core/persistent_shell.py`）：
    `cd` / `export` / venv 跨调用保留。判定与审批一个字不改 —— 两种模式下
    同一条命令得到同一个结论。代价：超时会重置那个 shell（会话内状态丢失），
    且不能与 `background` 同时用。
    """
    raw = (command or "").strip()

    # 持久 shell 与后台互斥
    # 进程是全会话共享的：把一次调用转后台，后续命令会与它抢同一个 stdin。
    # 放在审批之前 —— 先问再拒，等于白弹一次浮窗。
    if persistent and background:
        return {
            "success": False,
            "error": ("`persistent` 与 `background` 不能同时用：持久 shell 的进程"
                      "是这个会话共用的，转后台会和后面的命令抢输入。"
                      "要么前台在持久 shell 里跑，要么用一次性进程转后台。"),
            "denied": True,
        }

    # PowerShell 下没有常驻 shell：明确拒，不静默退化成一个一次性进程 ——
    #   "`persistent=true` 却什么都不保留"是那种安静地不兑现的承诺。
    #   目录照常跨调用记住（`_wrap_ps` 回传 cwd），所以那句"cd 仍然有效"是真的。
    if persistent and shell_kind() == "powershell":
        return {
            "success": False,
            "error": ("`persistent` 在 PowerShell 下不支持：这台机器上没有 Git Bash，"
                      "没有常驻 shell 可以复用。要跨命令保留变量，把它和用到它的"
                      "命令写在同一条里；目录本身是跨调用记住的，`cd` 照常有效。"),
            "denied": True,
        }

    # 查后台作业（只读，不执行任何东西）
    # 放在最前面：查作业不该走命令判定/审批那一整套（它没执行任何东西）。
    if job_id:
        from core import jobs

        snap = jobs.poll(str(job_id).strip())
        if snap is None:
            return {
                "success": False,
                "error": (f"没有这个任务：{job_id!r}。"
                          f"（任务表只记**转过后台**的命令 / 子 agent；"
                          f"正常完成的命令不进表。）"),
                "known_jobs": [j["job_id"] for j in jobs.list_jobs()][:10],
            }
        return {"success": True, **snap,
                "note": ("还在跑。" if snap["status"] == "running"
                         else f"已结束（exit={snap.get('exit_code')}）。")}

    # 两个都没给，当场拒
    # 不靠 schema 的 `required`：我们的派发层不执行它（模型可能什么都不传），
    # 而"空命令"会一路走到审批，白弹一次浮窗。这里说清要什么。
    if not raw:
        return {
            "success": False,
            "error": ("要么给 `command`（要执行的命令），要么给 `job_id`（查一条"
                      "超时转后台的作业）—— 两个都没给。"),
        }


    # 长度上限（见 `_MAX_COMMAND_CHARS` 那段）。先于一切判定 ——
    # 连档都不用分：这条命令无论属于哪一档都送不出去。
    if len(raw) > _MAX_COMMAND_CHARS:
        return {
            "success": False,
            "error": (f"命令太长了（{len(raw):,} 字符，上限 "
                      f"{_MAX_COMMAND_CHARS:,}）。这一条不是性能限制 —— "
                      f"被审批物（命令原文）要整个装进审批载荷才送得到你手上，"
                      f"超了它审批就会**静默消失**。"),
            "hint": ("把这段逻辑写成一个脚本文件（`create_document` / "
                     "`add_block`），然后 `bash 那个文件`。"),
            "too_long": True,
        }

    from bus.registry import get_workspace_manager
    wm = get_workspace_manager()

    # 「关闭安全询问」开关（设置页）
    # 从 guard 上读，不自己去读 `data/settings.json`：
    # 那个文件已经有 `bus/core.py`（启动）与 `web/server.py`（读写）两个读者，
    # 再加一个"每次调用现读"的读者就是第三套口径，而它们会在什么时候生效
    # 上分叉。guard 是活的对象，设置页改它当场生效（与 `auto_approve`
    # / `cross_zone_enabled` 逐字同一条路）。
    # 拿不到 wm/guard，`False`（= 照常问）。读不到就一定朝"问"倒。
    relax = False
    try:
        relax = bool(getattr(getattr(wm, "security", None),
                             "relax_command_tier", False))
    except Exception:
        relax = False

    # 用户划的「规避目录」也要进"提及升档"
    # 走同一个活对象（`wm.security`），不自己读 `data/settings.json` ——
    #    理由逐字同上：`forbidden_dirs` 在 `classify_path` 里是活的那个表
    #    （设置页改它当场生效），再造一个"每次调用现读文件"的读者就是第三套口径。
    # 读不到，空表，只保留出厂名单，行为不受影响。
    # 拿到的是合并后的表（`DEFAULT_FORBIDDEN_DIRS` 的系统目录 + 用户追加）
    #    —— 有意如此：系统目录本身就是用户配置规避目录时最想保护的对象之一，
    #    而拆成"出厂 vs 用户"两张要 guard 存两份、多一个真相源。
    extra_protected: tuple[str, ...] = ()
    try:
        extra_protected = tuple(
            getattr(getattr(wm, "security", None), "forbidden_dirs", ()) or ())
    except Exception:
        extra_protected = ()

    # 这一档给不给执行、默认超时多长
    #
    # 工具的 handler 拿不到 session_id（签名是 `handler(**tool_args)`）
    #会话只能从请求快照上取（那台机器存在的理由之一，见
    # `core/request_snapshot.py`）。拿不到快照（有人在循环外直接调它）
    #空会话号，走到默认档（不许因为"取不到档"就不给跑）。
    #
    # 判据是档位，不是这里的第二份名单：`offers_command(spec)` 是唯一判定
    # （`command_timeout_ms == 0` 就是"这一档不给"的唯一表示，见 `core/tiers.py`）。
    from core import request_snapshot
    from core.tiers import (APPROVAL_RELAX, APPROVAL_REMEMBER, current_spec,
                            effective_approval, offers_command)

    _snap = request_snapshot.get_current()
    spec = current_spec(_snap.session_id if _snap is not None else "")
    if not offers_command(spec):
        print(f"[tier] 拒绝 run_command：档 {spec.level}（{spec.name}）不给执行命令")
        return {
            "success": False,
            "denied": True,
            "tier": "tier",
            "error": (f"**这一档不给执行命令**（当前是档 {spec.level}「{spec.name}」）—— "
                      f"要看/改文件用文件类工具；要执行命令得把投入档调到 3 及以上。"),
        }
    if not timeout_ms:
        # 调用方没给，档位给的默认。
        # `MAX_TIMEOUT_MS` 仍是全局硬上限（调用方给再多也压回来，`_run` 里那道），
        # 档位只决定默认值，不另开一个上限口径。
        timeout_ms = spec.command_timeout_ms

    # 配额（设计 §6.4）：每会话后台命令 ≤ 3 —— 拒，并给路线
    # 只对模型显式请求（`background=true`）生效：超时降级是"别白干"，
    # 不能被配额杀掉（那样等于回到"到点就杀"）。用户 Ctrl+B 也不在这里拒
    # —— 那条路是"已经在前台跑的命令"，拒了也不能让它停下来。
    if background:
        from core import jobs as _jobs
        _qkey = _snap.session_id if _snap is not None else ""
        if _jobs.running_count_for(_qkey, _jobs.KIND_COMMAND) >= 3:
            return {
                "success": False,
                "denied": True,
                "tier": "quota",
                "error": ("**后台命令已经到上限了**（这个会话已有 3 条在后台跑）—— "
                          "这一条**没有执行**。先用 `task(action=\"status\")` 看谁还在跑、"
                          "`action=\"wait\"` 等一个回来，或者直接前台跑（会等到它结束）。"),
            }

    # 档位只能收窄上面那个开关 —— `effective_approval` 是全仓唯一
    #     的收窄点（`min(用户的选择, 这一档的上限)`）。判据：
    #     调档永远不能替用户把闸门开大，反过来档压得住用户。
    #
    # 六档全是 `APPROVAL_RELAX`，这一行不改变任何行为。它存在的
    #    理由是让 `approval_ceiling` 这根旋钮真的承重。
    #
    # 只接了"能不能降级"这一半：上限的更低两档（`APPROVAL_REMEMBER` =
    #    连"别再问"都不给）没有消费者 —— 没有档用它，硬接就是给一个
    #    不可证伪的分支铺路。
    relax = effective_approval(
        APPROVAL_RELAX if relax else APPROVAL_REMEMBER, spec) >= APPROVAL_RELAX

    tier, reason = classify_command(raw, relax_guarded=relax,
                                    extra_protected=extra_protected)

    if tier == TIER_DENY:
        # 权限解禁（完全权限 + 设置里那个开关），`deny` 档也放行。
        #   代价如实记：这一轮出网地板不存在（`core/unlock.py` 的模块 docstring）。
        _unlocked = False
        try:
            from core import unlock as _unlock
            _unlocked = _unlock.bypassing()
        except Exception:                                   # noqa: BLE001
            _unlocked = False
        if _unlocked:
            print("[unlock] deny 档命令放行：%s" % raw[:120])
            tier = TIER_ALLOW
        else:
            return {
                "success": False,
                "denied": True,
                "tier": "deny",
                "error": reason,
                "hint": "要用网络请调 `web_fetch`（它走的是出网地板 + 审批闸门）。",
            }

    work_dir, cwd_note = _resolve_cwd(cwd, wm)

    # 被审批物 = 归一化后的命令原文（折叠空白）。
    # 选项 2「本次对话内不再询问此命令」认的就是它 —— 所以只归一化空白，
    # 不转小写、不删参数（大小写可能改变语义）。
    # 必须走共享函数（`core/approval.py::command_subject`）。
    #   它有三个读者（闸 / 自动模式的预授权 / 分类器的第①层），而查表是
    #   按整串精确比的，三处各写一遍就必然分叉（症状：自动模式
    #   "已预授权"了却照样弹窗）—— 见那个函数的 docstring。
    subject = command_subject(raw)

    # 审批档位只有一种。
    # 命令的 tier 仍然分三档（`deny`/`guarded`/`allow`），但 `guarded` 与
    # `allow` 走同一张审批表 —— 它只影响提示语正文（说清为什么危险），
    # 不再影响给哪几个选项。见 `core/approval.py` 关于"一格只许一个 kind 用"的段落。
    kind = "command"

    # 降级过要说出来。开关把危险档降成普通档之后，"这条本来会走
    # 危险档"那件事在 `tier` 里已经看不见了 —— 而用户/模型都该知道
    # 现在放行它的门槛比默认低。再算一次严格档（纯函数、零 IO）只为
    # 如实报告，不参与任何判定。
    relaxed_from = ""
    if relax and tier == TIER_ALLOW:
        strict, _ = classify_command(raw, extra_protected=extra_protected)
        if strict == TIER_GUARDED:
            relaxed_from = tr(_RELAX_NOTE)

    # 免问集：只读 + 简单的那一类不弹窗
    #
    # 位置承重：在 `TIER_DENY` 之后（那是确定性拒绝 —— 免问集
    #   放宽不了它，判据与 `relax_command_tier` 那条逐字相同），
    #   而只对 `TIER_ALLOW` 生效，`guarded`（`git` / 提到受保护路径 /
    #   大批删除）一个字不受影响。
    #
    # 判据是两层，缺一不可：
    #   ① 用户开着那个开关（`guard.no_ask_readonly`，设置页）—— 这是政策；
    #   ② 这条命令够格（`no_ask_reason`，纯函数）—— 这是判定。
    #   两层分开是有意的：政策可以关掉，而"够不够格"与用户无关。
    # 与 `relax` 同一条路：从 guard 这个活对象上读，不自己读
    #   `data/settings.json`（理由逐字同上面那段：再加一个"每次调用现读"的读者
    #   就是第三套口径，而它们会在什么时候生效上分叉）。
    # 拿不到，`False`（= 照常问）。读不到一定朝"问"倒。
    no_ask_on = False
    try:
        no_ask_on = bool(getattr(getattr(wm, "security", None),
                                 "no_ask_readonly", False))
    except Exception:
        no_ask_on = False
    _no_ask = no_ask_reason(raw) if (tier == TIER_ALLOW and no_ask_on) else ""

    # **开关那个裸 `*` 不再免问** —— 判据是
    #    "命中的是不是具体那一条"，而不是"有没有命中"。
    #
    #    为什么：设置页那个「执行·全部放行」写的是**裸 `*`**，而它应当
    #    「交给分类器判，不是真的全放」。分类器说安全时会发一条
    #    具体的一次性授权（`_pre_grant_for_auto`），由下面这句放行；
    #    说拦，那道闸直接拒，根本走不到这里；没答上来（手动档）
    #没有授权，弹窗（这正是"退回问人"落在实处）。
    #    用户亲手加的那一条命令 / 他刚批过的那个对象，`specific`，照旧免问。
    # 选项 2 记的是一条"规则"，而它常常是前缀
    #    （`npm run *`）。规则由那个保守的提取器给；拿不准，空串，
    #    退回整条精确匹配（行为与只用精确匹配时一致）。
    #
    #    `guarded` 档不给前缀（判据：判定序是
    #    `deny/ask → 路径约束 → allow` ——「批准一条 `git status *`」
    #    不会顺手把 `rm -rf core/` 也放行）。这一档只认整条精确匹配；
    #    `_rule` 只在 `TIER_ALLOW` 时计算，`prefix=` 也只在那里为真。
    _rule = ""
    if tier == TIER_ALLOW:
        try:
            from core.command_policy import command_prefix_rule
            _rule = command_prefix_rule(subject)
        except Exception:
            _rule = ""

    _grant = ("" if wm is None else
              wm.matched_target_grant(kind, subject,
                                      prefix=(tier == TIER_ALLOW)))
    if not _no_ask and _grant != "specific":
        from core.approval import pending_payload
        ask_index = 1
        try:
            # 「第几次被问起」是 `wm` 上的投影（真值在调度器），
            # 由 `agent_loop` 在工具返回 `pending_*` 时推进。
            ask_index = int((wm.approval_ask_counts if wm else {}).get(kind, 0)) + 1
        except Exception:
            pass
        return pending_payload(
            kind,
            body=_approval_prompt(subject, work_dir, reason, relaxed_from)
            # 自动模式为什么没替你答（`agent_loop.auto_note_suffix`）。
            #   自动档下仍被要求手批时，从这个弹窗上看不出是
            #   (a) 手动档 / (b) 熔断退回 / (c) 判了安全却不知道怎么放行。
            #   延迟 import：`core.shell` 与 `core.agent_loop` 之间本来没有边，
            #   加一条模块级 import 就是造一条新的（这个仓库被环 import 咬过不止一次）。
            + _cmd_auto_note(),
            ask_index=ask_index, footer=True,
            subject=subject, detail=reason or relaxed_from,
            cwd=str(work_dir), tier=tier, relaxed_from=relaxed_from,
            # 选项 2 会记成哪条规则（空串 = 只能整条精确匹配）。
            #    它一路带到两条通道上（文本那份进 `prompt`、浮窗那份进
            #    `build_payload` 的选项标签）—— 把规则给用户看，
            #    而不是让他猜"不再询问此命令"到底记住了什么。
            rule=_rule,
            # `status` 是工具返回的契约：`core/pipeline/stages/agent_loop.py`
            # 认的就是这个串（它按 `kind` 分派，命令只有 `command` 一种）。
            # `pending_payload` 给的默认值是跨区那套的 `pending_approval`，覆盖掉。
            status="pending_command_approval",
        )

    # 免问集生效时"没问"这件事必须看得见（"没发生"必须看得见）。
    # 用户看不到弹窗，分不出"免问集生效"与"审批坏了" ——
    # 而这两种情况的后果完全不同（标签不许说谎）。
    # 这里只 `print`（进运行日志）。界面上一行是它的自然后继 ——
    #   要动事件流（前端加一种事件 = 三处），不顺手做。
    if _no_ask:
        print(f"[command] 免问集命中，未询问：{subject[:120]} —— {_no_ask}")

    # 命令里点名的、工作区内的文件先记一笔（列表要它，留档也要它）。
    #   放在这里 = 审批过了、真的要跑了 —— 没跑的命令不该出现在"本轮改过"里。
    _note_command_touches(raw, work_dir, wm)

    _sid = _snap.session_id if _snap is not None else ""
    if persistent and _sid:
        from core import persistent_shell
        result = persistent_shell.run(
            _sid, raw, work_dir, timeout_ms,
            # 调用方显式给了 cwd 才 `cd`；没给就让 shell 保持它自己的位置
            # （那才是"持久"的含义，而回执里的 cwd 是它的实时值）。
            cwd=(str(work_dir) if cwd else ""),
        )
        if not result.get("persistent"):
            # 起不来，退回一次性进程（降级要出声）。
            _why = result.get("error") or "未知原因"
            result = _run(raw, work_dir, timeout_ms, description=description)
            result["persistent_fallback"] = _why
    else:
        result = _run(raw, work_dir, timeout_ms,
                      background=background, description=description)
        if persistent:
            result["persistent_note"] = (
                "这条没有会话身份，用一次性进程跑的（会话内状态不会保留）。")
    result["tier"] = tier

    # 审计：这是全仓唯一能执行任意命令的工具，必须留下持久记录
    # —— 控制台输出在长跑进程里会滚掉，而 `run_command` 不走
    # `validate_path`，两条路都不写盘的话，执行过的命令事后查不到。
    # 见 `SecurityGuard.record_command` 的 docstring。
    # 记的是命令原文，不截断 —— 截断审计内容等于"被审计的不是被执行的那条"。
    # 写在执行之后：这一行代表"真的跑过了"。审批与拒绝不在这里
    # （它们没执行，而且模型看得到）。
    try:
        _guard = getattr(wm, "security", None) if wm is not None else None
        if _guard is not None:
            _guard.record_command(
                raw, "timeout" if result.get("timed_out") else
                     ("ok" if result.get("success") else "fail"))
    except Exception as exc:
        # 审计失败不能吞掉：它是这条路径上唯一的留痕。
        print(f"[audit] run_command 审计写入失败（{type(exc).__name__}: {exc}）"
              f" —— 这条命令执行过但没有记录")

    # 输出裁剪（保尾 + spill）。在工具里做，不在 agent_loop 里 ——
    # 那边那把刀保的是头部，方向正好相反（见 `_OUT_BUDGET` 的注释）。
    out_show, out_path = _clip(str(result.get("stdout") or ""), _OUT_BUDGET)
    err_show, err_path = _clip(str(result.get("stderr") or ""), _OUT_BUDGET // 2)
    result["stdout"] = out_show
    result["stderr"] = err_show
    if out_path or err_path:
        result["full_output"] = [p for p in (out_path, err_path) if p]

    if cwd_note:
        result["cwd_note"] = cwd_note

    if result.get("output_encoding"):
        result["encoding_note"] = (
            "这条命令的输出**不是 UTF-8**（按 %s 解码）。如果看到乱码，"
            "让程序显式输出 UTF-8（例如先在命令里把控制台输出编码设成 UTF-8），"
            "不要据此断定程序算错了。"
            % "、".join(result["output_encoding"]))

    # 「框架记住 cwd」那一半：写投影（真值在调度器上，它每轮投影进来、
    #   轮末收回去）。工具没有会话身份，这是唯一能写的地方 —— 与
    #   `grant_target_once` 同一条规矩。
    #
    # 超时转后台的那次不记：那条命令还没结束，`$PWD` 根本没打出来，
    #    `result["cwd"]` 只是回退值 —— 记它等于把"上一次的位置"当成现状。
    if wm is not None and not result.get("timed_out"):
        try:
            wm.session_cwd = str(result.get("cwd") or "")
        except Exception:
            pass

    # 起了一个不会自己退出的东西，在回执里提醒收尾。
    #
    # 判据：危害在「没停」，不在「开始」。所以它既不拦也不问，只提醒 ——
    #   而没跑起来的东西没有收尾可言，所以这一句挂在「成功 / 超时转后台」
    #   这两支上（超时那支进程还活着，更该提醒）。
    # 落点是结果字典的一个键：`as_text()` 就是 `json.dumps`，与
    #   `cwd_note` / `encoding_note` 同一个形状，不需要另加渲染。
    # 位置是「最前面」而不是末尾：回执超预算时 `_truncate_to_artifact`
    #   保的是头部（`content[:max_chars]`），排在 `stdout` 后面的键会被切掉
    #   —— 而长输出的命令恰恰是最需要这句提醒的那些。
    if result.get("success") or result.get("timed_out"):
        _linger = linger_reason(raw)
        if _linger:
            result = {
                "cleanup_note": (
                    "%s，它不会自己退出。这一轮做完请主动把它停掉，不要留给用户；"
                    "确实要留着，就明确告诉用户它在哪个端口 / 地址、怎么关。"
                    % _linger),
                **result,
            }

    return result


#: 这一段是纯税，必须写短（"system prompt 是纯税里唯一会长胖的一格"）。
#: 砍掉重复表述（"框架不解析命令"这类用户看不到、模型也不因此改变行为的话），
#: 现在的开销见 `scripts/measure_request.py` 报的数。
#: 改这里必须重量一次。
#:
#: 最后一组 steer（工具优先级）：实际读同一张 xlsx 时，
#: `read_excel_data` 0 次、`run_command` 带 openpyxl 8 次；
#: 画图时 `plot_excel_data` 0 次、`run_command` 带 matplotlib ——
#: 工具都在，模型不知道优先级。
#: 四条里采了三条，第 ④ 条（按"替代品在不在"开关）不适用：
#: 那段话点名的五个原语全在 `_CORE_TOOLS` 里，每轮都发给它
#:静态写死永远是真话，且不动前缀（缓存无损）。
#: 反过来，若做成"按本轮发了谁"动态拼，这条 description 会**随发现集
#: 增长而改写**，从那一条起整段前缀重算。别做。
#: 办公工具（读表格 / 画图 / 改文档）不在常驻集，所以那一条只给能力
#: 与路线（`search_tools`），不点具体名字 —— 点了就违反"绝不指向一个
#: 这轮没发给它的工具"的同一条纪律。
COMMAND_TOOL_SCHEMA: dict = {
    "description": (
        "在 bash 里执行一条命令（每条独立进程；工作目录由框架记住）。"
        "出网类命令（curl/wget/ssh/scp）不执行 —— 要联网用 web_fetch。"
        "命令默认要用户批准；碰到框架源码/数据、密钥、仓库配置会升到每次都问。"
        "超过超时会**转到后台继续跑**（不杀），用同一个工具带 job_id 回来查。"
        "已知会很耗时、而你现在不需要它的结果时，`background=true` 直接转后台"
        "（用 job_id 查结果；别在结果回来之前编造它）。"
        "★ 能用专用工具就别用 shell：读文件 read_file（不是 cat/head/tail/sed）· "
        "改文件 edit_file（不是 sed/awk）· 写文件 write_file（不是重定向 / echo / python -c）· "
        "找文件 glob（不是 find/ls）· 搜内容用 grep（不是命令行的 grep/rg）。"
        "读表格 / 画图 / 改 Office 文档同样有专用工具 —— 先 search_tools 找，别用脚本重造。"
        "★ 用 shell 切窗口 / 模拟键鼠不是它的活：接管没开就先让用户开接管。"
        "**截屏可以**（需要看屏幕时直接截）。"
        "拿不准就默认走专用工具（更好读、也更好批）；bash 只留给系统命令和它们做不到的事。"
    ),
    "inputSchema": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "命令原文，原样交给 bash -c。可用管道、重定向、&&。",
            },
            "description": {
                "type": "string",
                "description": "一句话说明这条命令在做什么（UI 摘要 / 完成通知用）。",
            },
            "background": {
                "type": "boolean",
                "description": ("true = 立刻转后台，不等它跑完；用 job_id 查状态/输出。"
                                "只用于**确实耗时长**且你现在不需要结果的活。"),
            },
            "cwd": {
                "type": "string",
                "description": "工作目录（限工作区内的）。不传则用上次命令结束时的目录。",
            },
            "timeout_ms": {
                "type": "integer",
                # 默认值由档位给，文案不绑死一个数
                # （写死 `默认 {DEFAULT_TIMEOUT_MS}` 在别的档上是假话）。
                # 这一段是每请求的税，改完必须重量一次。
                "description": f"超时毫秒，默认由投入档给，"
                               f"上限 {MAX_TIMEOUT_MS}。超时会转后台，不杀。",
            },
            "job_id": {
                "type": "string",
                "description": "只用来**查**一条被转后台的作业（给了它就不执行新命令）。",
            },
            "persistent": {
                "type": "boolean",
                "description": ("true = 在本会话的常驻 shell 里跑：`cd` / `export` / "
                                "venv 跨调用保留；超时会重置它（变量丢失）。"
                                "不能与 background 同用。"),
            },
        },
        "required": [],
    },
}
