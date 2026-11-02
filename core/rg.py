"""`grep` 的执行层 —— 跑随包发的 `ripgrep` 二进制。

## 为什么是外部二进制

自身实现走不动极端规模：`file_tools._iter_files` 刻意返回整张列表
（判据是"截断点不许随文件系统顺序漂移"），到千万级文件就是 **GB 级内存 +
先把整棵树走完才开始匹配**。走树交给 ripgrep（并行、流式、到上限就停），
我们只做两件事：拼参数、把 `--json` 事件翻成回执。

不联网下载、不要求用户另装：二进制随包发（`vendor/ripgrep/`，见那里的
`README.md`）。找不到，报错带路线，不静默退回慢引擎（那会让"搜索慢"
变成一个没人认领的现象）。

## 三条会改变结果的参数（都在这里钉着）

| 参数 | 为什么 |
|---|---|
| `--pcre2` | 我们对模型承诺的是"Python 正则"。rg 默认引擎没有后行断言/反向引用，不加这个，模型照旧描述写的 `(?<=…)` 会当场失败 |
| `--sort path` | 确定性：同一条命令两次得同一个答案（旧的 `_iter_files` 就为这条返回列表）。代价是 rg 退成单线程 —— 大仓吞吐会低一些，但仍是 Python 逐文件读的十几倍 |
| `--no-config` | 不看 `RIPGREP_CONFIG_PATH`：用户环境里一份配置就能把结果改掉，而那不在我们任何一处记录里 |

## 编码

`--encoding none`，非 UTF-8 的行在 JSON 里以 base64 字节回来，
调用方用我们自己的 `_decode`（认 cp936 / UTF-16 / UTF-8）解 —— 这样
"非 UTF-8 文件也能命中"这条旧行为不会被 rg 悄悄改掉。

## 这个模块不做的事

不做回执排版、不做上限截断、不做跨区/根校验 —— 那些在 `file_tools.grep` 里。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__all__ = [
    "RgUnavailable", "platform_dir", "binary_path", "build_args", "RgRun",
]

#: 仓库根 / 安装后的 `resources/app` —— 两种跑法同一个相对路径。
#: `core/` 直接在它下面，所以 `parent.parent` 就是它（见 `main.cjs::backendRoot`）。
_APP_ROOT = Path(__file__).resolve().parent.parent

#: `(sys.platform, 机器架构)` → `vendor/ripgrep/` 下的目录名。
#: 今天只有 Windows x64；别的平台没有二进制 —— 那就明说，不假装能用。
_PLATFORM_DIRS = {
    ("win32", "amd64"): "win32-x64",
    ("win32", "x86_64"): "win32-x64",
}


class RgUnavailable(RuntimeError):
    """这个环境没有可用的 ripgrep 二进制。消息里必须带路线（调用方原样回给模型）。"""


def platform_dir() -> str:
    """这台机器该用 `vendor/ripgrep/` 下哪个目录。没有，`RgUnavailable`。"""
    key = (sys.platform, (os.environ.get("PROCESSOR_ARCHITECTURE") or
                          os.environ.get("HOSTTYPE") or "").lower())
    #: `platform.machine()` 在 Windows 上更稳（PROCESSOR_ARCHITECTURE 会被
    #: 32 位进程报成 x86），先问它。
    try:
        import platform as _pf
        key = (sys.platform, str(_pf.machine() or "").lower())
    except Exception:                                       # noqa: BLE001
        pass
    name = _PLATFORM_DIRS.get(key)
    if not name:
        raise RgUnavailable(
            "这个平台（%s/%s）**没有随包的 ripgrep** —— 今天只发了 Windows x64。"
            "要么换一台 Windows x64 的机器，要么用 `CRANNIA_RG` 指定一个 "
            "ripgrep 可执行文件的绝对路径。" % (sys.platform, key[1] or "?"))
    return name


def binary_path() -> Path:
    """可执行的绝对路径。顺序：`CRANNIA_RG` → 随包目录 → 报错。

    不查 PATH：系统里那个 rg 版本/特性未知（`--pcre2` 可能没编进去），
      悄悄用上就是"同一个工具两台机器行为不同"。要换就显式给 `CRANNIA_RG`。
    """
    override = str(os.environ.get("CRANNIA_RG") or "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise RgUnavailable(
            "`CRANNIA_RG` 指到了 `%s`，但那里没有文件 —— 改对或删掉这个变量。" % p)
    exe = "rg.exe" if sys.platform == "win32" else "rg"
    p = _APP_ROOT / "vendor" / "ripgrep" / platform_dir() / exe
    if p.is_file():
        return p
    raise RgUnavailable(
        "找不到随包的 ripgrep（应当是 `%s`）。\n"
        "· 安装版：这个文件应当由安装包带进来 —— 重装一次；\n"
        "· 开发树：跑 `python scripts/fetch_ripgrep.py` 取一份；\n"
        "· 急用：用 `CRANNIA_RG` 指一个 ripgrep 可执行文件的绝对路径。"
        % p)


# 参数

#: 单个文件的上限（与我们旧的 `_MAX_GREP_FILE_BYTES` 对齐；调用方传进来）。
def build_args(pattern: str, root, *, glob: str = "", context: int = 0,
               include_ignored: bool = False, skip_dirs=(), max_file_bytes=0,
               max_line_chars=0) -> list:
    """拼 argv（不含二进制本身）。

    *skip_dirs* 是 `-g '!<dir>/**'` 的排除名单 —— 调用方从
    `core.walk_scope.SCAN_SKIP_DIRS` 那一份常量生成，别在这里再写一份。
    """
    args = [
        "--json", "--stats", "--sort", "path", "--no-config",
        # 见模块头：正则方言 + 编码 + 确定性，三条都在这里定。
        "--pcre2", "--encoding", "none",
        #: rg 14+ 默认只在自己是 git 仓时才读 `.gitignore`；我们的走树规则
        #:   不管在不在仓里都读，加这一个，两边语义才对齐。
        "--no-require-git",
        #: 祖先目录的 `.gitignore` 不算数，只认搜索根以下的那几份。
        #:   不加这一条时，从工作区里搜东西会去读仓根那份 —— 而它写着
        #:   `workspace/*`（那句是"别把运行数据提交进 git"），于是 agent
        #:   自己写进工作区的文件对它自己不可见（1200 处回「没有匹配」）。
        "--no-ignore-parent",
    ]
    if max_file_bytes:
        args += ["--max-filesize", str(int(max_file_bytes))]
    if max_line_chars:
        #: `--max-columns` 数的是字节；给一点余量（多字节字符）。
        args += ["--max-columns", str(int(max_line_chars) * 4),
                 "--max-columns-preview"]
    if include_ignored:
        #: 我们的 `include_ignored=True` = 关掉 `.gitignore` 那两层，
        #: 但隐藏目录仍然跳过（前缀规则那条一直在）—— `--no-ignore`
        #: 正好只关前者，rg 默认仍然跳过隐藏项。
        args.append("--no-ignore")
    else:
        for d in skip_dirs:
            d = str(d or "").strip().strip("/")
            if d:
                #: 两种写法都要：`!/x/` 管"任意深度"，`!x/**` 管"就是根上那层"。
                args += ["-g", "!**/%s/**" % d, "-g", "!%s/**" % d]
    if context and int(context) > 0:
        args += ["-C", str(int(context))]
    if glob:
        args += ["-g", str(glob)]
    args += ["--", pattern, str(root)]
    return args


# 执行（流式）

class RgRun:
    """一次 rg 执行。流式读 `--json` 事件，随时可以提前收工。

    用法::

        with RgRun(args, timeout=60) as run:
            for ev in run.events():
                ...
                if enough:
                    break            # 退出 with，进程被杀

    `run.stats` 在跑完之后是 `--stats` 那份汇总（可能为空 —— rg 没给就没有）。
    """

    def __init__(self, args: list, *, cwd=None, timeout: float = 60.0):
        self._exe = binary_path()
        self._args = list(args)
        self._cwd = str(cwd) if cwd else str(_APP_ROOT)
        self._timeout = float(timeout)
        self._proc = None
        self.stats: dict = {}
        self.returncode: int | None = None
        self.error_text: str = ""
        #: 到过墙钟上限吗（调用方据此在回执里如实说「结果可能不全」）。
        self.timed_out = False
        #: stderr 落到临时文件：`PIPE` 而不读会在大段报错时把子进程堵死。
        self._err = tempfile.TemporaryFile()

    # 生命周期
    def __enter__(self):
        env = dict(os.environ)
        #: 不让 rg 因为控制台编码把 JSON 弄坏（它本来就输出 UTF-8）。
        env.setdefault("RIPGREP_CONFIG_PATH", "")
        self._proc = subprocess.Popen(
            [str(self._exe), *self._args],
            stdout=subprocess.PIPE, stderr=self._err,
            cwd=self._cwd, env=env, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            pass
        self.returncode = proc.returncode
        self._proc = None
        try:
            self._err.seek(0)
            self.error_text = self._err.read().decode("utf-8", "replace").strip()
        except Exception:                                   # noqa: BLE001
            self.error_text = ""

    # 读事件
    def events(self):
        """逐条产出 `--json` 事件（`dict`）。超时就收工。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        deadline = time.monotonic() + self._timeout
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("type") == "summary":
                    data = ev.get("data") or {}
                    if isinstance(data.get("stats"), dict):
                        self.stats = data["stats"]
                    continue
                yield ev
                if time.monotonic() > deadline:
                    self.timed_out = True
                    print("[rg] 超过 %.1fs —— 收工（已拿到的照常用）" % self._timeout)
                    return
        finally:
            if time.monotonic() > deadline:
                self.close()
