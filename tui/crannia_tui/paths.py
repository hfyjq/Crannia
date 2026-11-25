"""把"crannia 在哪、用哪个解释器"收敛到一处。

为什么不写死路径：这个包要能被人 clone 下来就跑。开发机上 crannia 与
本包通常是兄弟目录，但别人的目录结构不保证一样。所以按下面顺序找：

  1. `CRANNIA_HOME` 环境变量（明确指定，最高优先）
  2. 本文件所在的那棵树本身（源码位于 crannia 根下的 `tui/` 时）
  3. 上一级目录里叫 `crannia` 的兄弟目录（独立仓的开发布局）
  4. 都没有 → 报错，并告诉调用者该设哪个变量

解释器同理：`CRANNIA_PYTHON` 优先，否则用当前解释器。后者在
"TUI 与 crannia 共用一个 venv"时是对的；分开装时前者的用途就体现出来了。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .i18n import t

#: crannia 根目录的标志文件。用 `main.py` + `core/` 一起判，
#: 单独一个文件可能在任何 python 项目里撞到。
_MARKERS = ("main.py", "core", "web")


def _looks_like_crannia(p: Path) -> bool:
    return all((p / m).exists() for m in _MARKERS)


_DOTENV_DONE = False


def load_local_env() -> None:
    """读本包目录下的 `.env`，只填还没设过的变量。

    存在的理由很窄但很硬：`CRANNIA_PYTHON` 是"每台机器都不一样"的绝对路径。
    写进启动脚本，等于把开发机的目录结构随仓库发布出去；而每次开新窗口都
    `set` 一遍又不现实。放 `.env` 里，脚本保持通用，机器相关的只有一个文件。

    不覆盖已设的变量：命令行显式 `set` 的优先级应当高于文件。
    """
    global _DOTENV_DONE
    if _DOTENV_DONE:
        return
    _DOTENV_DONE = True

    p = Path(__file__).resolve().parent.parent / ".env"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def crannia_home() -> Path:
    """返回 crannia 的根目录。找不到就抛 `RuntimeError`（不返回 None:
    调用方拿 None 之后每个地方都要判空，这个错应该在启动那一刻就炸掉）。"""
    load_local_env()
    env = os.environ.get("CRANNIA_HOME", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if not _looks_like_crannia(p):
            raise RuntimeError(t("CRANNIA_HOME 指向 %s，"
                                 "但未在目标位置读取到 main.py / core / web")
                               % p)
        return p

    # 先看"我自己在不在 crannia 树里"（源码在主仓 `tui/` 下时就是这个形状），
    # 再看兄弟目录（独立仓的开发布局）。顺序不能反：主仓里也可能有一个
    # 名字恰好叫 crannia 的子目录，先判自己更不容易认错。
    here = Path(__file__).resolve()
    for base in (here.parent.parent, here.parent.parent.parent):
        if _looks_like_crannia(base):
            return base
        cand = base / "crannia"
        if _looks_like_crannia(cand):
            return cand

    raise RuntimeError(
        t("找不到 crannia。设 CRANNIA_HOME 指向它的根目录"))


def update_root() -> Path:
    """装新版本的那个根 —— `releases/` 与 `current` 住在它下面。

    它不总是"代码根"：独立包里运行中的代码在 `releases/<版本>/`，
    而指针在启动器旁边（启动器只读它自己那一层的 `current`）。
    启动器把"我在哪"放进 `CRANNIA_LAUNCH_ROOT`；开发树与整合包里
    那个目录没有 `releases/` / `current`，落回代码根（整合包本身不自己更新）。
    """
    env = os.environ.get("CRANNIA_LAUNCH_ROOT", "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        if p.is_dir() and ((p / "releases").is_dir() or (p / "current").is_file()):
            return p
    return crannia_home()


def _has_flask(py: Path) -> bool:
    """这个解释器跑得动 crannia 吗。

    判据选 flask 不是随手挑的：crannia 的 web 入口第一行就 import 它，
    缺了它后端必然起不来。用子进程问而不是看 site-packages 目录 ——
    后者的布局（venv / conda / --user / zipapp）不止一种。
    """
    import subprocess

    try:
        r = subprocess.run([str(py), "-c", "import flask"],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def python_exe() -> Path:
    """跑后端用的解释器。

    优先级：`CRANNIA_PYTHON` → 当前解释器（如果它跑得动 crannia）→
    crannia 目录下的 `.venv`。

    第二条是有原因的：TUI 与 crannia 共用一个 venv 时，当前解释器就是对的，
    不必再让用户配一遍。而 TUI 自己独立建 venv 时（只有 textual / rich），
    当前解释器跑不了后端，这时往下走找 crannia 的 venv。

    "跑不动就报错"比"随便挑一个然后启动失败"好：后者会在十几秒后
    吐一段 traceback，而原因其实在启动的第一秒就确定了。
    """
    load_local_env()
    env = os.environ.get("CRANNIA_PYTHON", "").strip()
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            raise RuntimeError(
                t("CRANNIA_PYTHON 指向 %s，但那里没有可执行文件") % p)
        # 显式指定也要验。不验的话，这个变量的唯一作用就是"把错误推迟到
        # 十几秒后"—— 而它存在的意义本来是省掉那次排查。
        if not _has_flask(p):
            raise RuntimeError(
                t("CRANNIA_PYTHON 指向 %s，但 `import flask` 失败，"
                  "后端异常\n"
                  "它应当指向已安装 crannia 依赖的解释器"
                  "（通常是未安装 TUI 依赖的 venv），"
                  "而非本程序自己的 venv。") % p)
        return p

    import importlib.util

    me = Path(sys.executable)
    if importlib.util.find_spec("flask") is not None:
        return me

    try:
        home = crannia_home()
    except RuntimeError:
        home = None
    if home is not None:
        for rel in ("Scripts/python.exe", "bin/python3", "bin/python"):
            cand = home / ".venv" / rel
            if cand.exists() and _has_flask(cand):
                return cand

    #: 提示里那条路径按平台给。在 Linux 上写 `Scripts/python.exe`，等于让
    #: 人去找一个不存在的文件。
    rel = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    raise RuntimeError(
        t("找不到运行所需 Python：当前解释器没有 flask，"
          "crannia 目录下也没有可用的 .venv。\n"
          "设 CRANNIA_PYTHON 指向一个装了 crannia 依赖的解释器"
          "（通常是 `crannia/.venv/%s`）。") % rel)
