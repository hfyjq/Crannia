"""测试不该碰生产数据 —— 逐文件跑一遍，看谁改了什么。

判据是 (相对路径, 大小, mtime)，不是路径集合。

只比路径会报假"干净"：`data/memory/test_*.db` 与
`logs/audit_test.log` 本来就存在，测试每跑一次改写一次，而路径集合看不出差别。
一边漏检、一边报"干净" —— 「绿了但其实没测到」。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe scripts/check_test_pollution.py            # 全目录
    .venv/Scripts/python.exe scripts/check_test_pollution.py --quiet    # 只报违例

退出码：0 = 生产目录一个字没动 · 1 = 有文件被改（逐条列出是哪个测试干的）

不要和 `run_tests.py`（或任何另一个测试驱动）同时跑。
它自己也要把 158 个文件各跑一遍 —— 两个驱动并发就是互相踩：
踩出一条假红（`test_destructive_gate` 15 通过 → 14，
而单独跑 5 次全绿），同时本次这份"干净"结论也不可信（它的快照窗口里
混进了别人的写入）。
判据同族：这类检查的分辨力只在有别人同时在看时才现形 ——
而"有别人同时在跑"恰恰是本脚本自己要排除的东西。

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 伴生文件的判据只许有一份 —— 引 `tests/_sandbox.py` 那一条，
#   不抄第二份（抄的代价不是多写三行，是两份会漂移）。
from tests._sandbox import is_sqlite_companion  # noqa: E402

#: 跑测试不该动的生产目录。加目录时想清楚：它真的是"生产数据"吗？
#: 盯的是"框架在磁盘上的全部持久状态"，不是一个地点清单。
#:
#: 只写 `["workspace", "data/memory", "logs"]` 时，`data/workdata/tool_results/`
#: （制品目录）会漏在外面：`test_tool_output_readback.py` 每跑一次就往那儿
#: 留一个 9000 字节的文件（积了 9 个），而本脚本照样报"干净"。
#:
#: 这与 `MemoryManager.new_session` 的 docstring 是同一句话：
#: 「the purge was written as a *list of places* rather than an invariant.
#: One artifact has been forgotten here per bug so far.」
#:改成 `data`（整棵树）—— 新加一个持久目录不需要有人记得来这里补一行。
WATCHED = ["workspace", "data", "logs"]

_SKIP_DIRS = {"__pycache__", ".git"}


def snapshot(root: Path | None = None) -> dict:
    """{相对路径: (大小, mtime_ns)} —— 三个目录的完整指纹。

    **SQLite 的伴生文件（`-shm`/`-wal`/`-journal`）不进指纹** ——
    它们的 mtime 由任何打开那个库的进程改（框架自己 / WebUI / 一个只读探针），
    算进来就是"只要框架开着跑就必然假红"。判据那一段在
    `tests/_sandbox.py::SQLITE_COMPANION_SUFFIXES` 上方，只此一份。

    *root* 只是为了让机械读者自己造靶子 ——
    不然这条判据只能挂在"生产目录当下恰好排掉了什么"上，而那今天可能是空的
    （没有进程开着库就没有 `-shm`），一条永远为真的断言。
    生产路径照旧走默认值。
    """
    base = Path(root) if root else PROJECT_ROOT
    out: dict = {}
    for d in WATCHED:
        sub = base / d
        if not sub.exists():
            continue
        for p in sub.rglob("*"):
            if p.is_dir() or any(part in _SKIP_DIRS for part in p.parts):
                continue
            if is_sqlite_companion(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(base))] = (st.st_size, st.st_mtime_ns)
    return out


def diff(before: dict, after: dict) -> dict:
    """返回 {路径: 'added'|'removed'|'modified'}。"""
    changes = {}
    for k, v in after.items():
        if k not in before:
            changes[k] = "added"
        elif before[k] != v:
            changes[k] = "modified"
    for k in before:
        if k not in after:
            changes[k] = "removed"
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="只报违例，不报进度")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    files = sorted(glob.glob("tests/test_*.py"))
    base = snapshot()
    if not args.quiet:
        print("=" * 66)
        print("测试污染生产目录自查 —— %d 个测试文件 / 监视 %s"
              % (len(files), ", ".join(WATCHED)))
        print("指纹 = (相对路径, 大小, mtime) —— **不是**路径集合")
        print("=" * 66)

    offenders: dict = {}
    broken: list = []
    for f in files:
        before = snapshot()
        try:
            subprocess.run([sys.executable, f], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=args.timeout)
        except subprocess.TimeoutExpired:
            broken.append(f)
        ch = diff(before, snapshot())
        if ch:
            offenders[f] = ch
            if not args.quiet:
                print("\n[POLLUTES] %s" % f)
                for path, kind in sorted(ch.items()):
                    print("    %-10s %s" % (kind, path))

    # 再确认"跑完一整轮之后，基线本身也该原样" —— 这一条抓的是
    # 跨文件累积（单个文件看不出，合起来才动的东西）
    tail = diff(base, snapshot())

    print()
    print("=" * 66)
    if not offenders and not tail:
        print("结论: PASS —— 跑完 %d 个文件，生产目录逐字节未变" % len(files))
        print("=" * 66)
        return 0

    print("结论: FAIL —— %d 个测试文件改动了生产数据" % len(offenders))
    for f, ch in sorted(offenders.items()):
        kinds = {}
        for k in ch.values():
            kinds[k] = kinds.get(k, 0) + 1
        print("    %-34s %s" % (os.path.basename(f), kinds))
    if tail:
        print("\n  跨文件累积（整轮跑完 vs 跑之前）：")
        for path, kind in sorted(tail.items()):
            print("    %-10s %s" % (kind, path))
    if broken:
        print("\n  [!] 超时未测：%r" % broken)
    print("=" * 66)
    print("判据：测试**自己**进临时目录（`TemporaryDirectory` + `chdir`），")
    print("      像 `test_clear_session` / `test_delete_approval_scope` /")
    print("      `test_office_write` 那样。**别改成「记得清」。**")
    print("=" * 66)
    return 1


if __name__ == "__main__":
    sys.exit(main())
