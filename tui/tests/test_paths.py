"""路径解析与解释器选择的断言。

这两件事的失败形态很特别：**错在启动第一秒，症状在十几秒后**。所以它值得被钉住，
而不是靠"跑一次看看"。下面每条都对应一个真实踩过的错。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import paths  # noqa: E402

_n = 0


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def with_env(**kv):
    """临时改环境变量。直接改 os.environ 再恢复，比 monkeypatch 省一个依赖。"""
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
    return old


def restore_env(old: dict) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def raises(fn, *args) -> str:
    try:
        fn(*args)
    except RuntimeError as exc:
        return str(exc)
    return ""


def main() -> int:
    # --- crannia_home -------------------------------------------------
    # 指到一个"有 main.py 但没有 core/web"的目录，必须拒绝。
    # 只认一个文件是不够的：任何 python 项目都有 main.py。
    tmp = HERE / "tests" / "_tmp_marker"
    tmp.mkdir(exist_ok=True)
    (tmp / "main.py").write_text("", encoding="utf-8")
    old = with_env(CRANNIA_HOME=str(tmp))
    try:
        msg = raises(paths.crannia_home)
    finally:
        restore_env(old)
        for p in tmp.iterdir():
            p.unlink()
        tmp.rmdir()
    check("未在目标位置读取到" in msg, "缺 core/web 的目录应当被拒绝")

    # 不设变量时自己找到根 —— 两种布局都算数：
    #   ① 源码在主仓 `tui/` 下（树内）；② 独立仓放在 crannia 旁边（兄弟目录）。
    # 判据是"那三个标记都在"，不是目录叫什么：主仓的名字与发布仓不同。
    old = with_env(CRANNIA_HOME=None)
    try:
        home = paths.crannia_home()
        check(all((home / m).exists() for m in ("main.py", "core", "web")),
              f"应当解析到一个 crannia 根，实际 {home}")
    finally:
        restore_env(old)

    # --- python_exe ---------------------------------------------------
    # 1) 指向不存在的文件 → 立刻报，且点明是哪个变量
    old = with_env(CRANNIA_PYTHON=str(HERE / "没有这个解释器.exe"))
    try:
        msg = raises(paths.python_exe)
    finally:
        restore_env(old)
    check("CRANNIA_PYTHON" in msg, "报错要点名 CRANNIA_PYTHON")

    # 2) 指向一个存在但跑不了 flask 的文件 → 也要立刻报。
    #    这条是补上的：原来显式指定的路径直接返回，不验证，
    #    结果"设了变量"的唯一作用是把错误推迟到后端启动之后。
    fake = HERE / "tests" / "_tmp_notpython.txt"
    fake.write_text("这不是解释器", encoding="utf-8")
    old = with_env(CRANNIA_PYTHON=str(fake))
    try:
        msg = raises(paths.python_exe)
    finally:
        restore_env(old)
        fake.unlink()
    check("import flask" in msg, "非解释器文件应当报 flask 那条")

    # 3) 不设变量、当前解释器又没有 flask → 报错里要给出下一步怎么做
    old = with_env(CRANNIA_PYTHON=None)
    try:
        try:
            found = paths.python_exe()
        except RuntimeError as exc:
            check("CRANNIA_PYTHON" in str(exc), "兜底失败时也要指出该设哪个变量")
        else:
            # 当前解释器带着 flask（共用 venv）也算正常结果
            check(Path(found).exists(), "返回的解释器必须存在")
    finally:
        restore_env(old)

    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
