#!/usr/bin/env python3
"""全量测试的并行跑法。

## 为什么要有它

本仓每个测试文件都是自带 `__main__` 的独立脚本（条数用
`ls tests/test_*.py | wc -l` 当场数 —— 别写死一个数：它会变，而且公开副本
与开发树带的测试文件本来就不是同一批）。旧的跑法是
`for f in tests/test_*.py; do python "$f"; done` —— 串行。

本机 32 核，全量串行 128 秒，
其中绝大多数是每个进程的启动 + 导入（0.05s 启动 + 0.31s 导入；
写这段时是 153 个文件，约 55 秒是纯固定开销，文件多了按比例涨），而真正算的东西很少
（最慢的 `test_jobs.py` 9.4 秒里 `user` 只有 0.016 秒 —— 它在睡觉）。
32 核上串行跑 153 个互不依赖的进程，纯属白扔。

## 它不做三件事（都是刻意的）

1. 不改任何测试文件。它们照旧可以单独跑
   （`python tests/test_x.py`）；这个脚本只是换一种调用方式。
2. 不引入 pytest。本仓没有 pytest，判据是
   "每个测试文件自带 `__main__` 运行器"—— 这条没动。
3. 不解析"哪种汇总格式"自己一套。它 import
   `summary_parse.parse_summary`（从 `check_progress` 抽出来的同一份判据，
   公开副本也带它）——
   两个解析器会漂，而漂掉的那一支的症状是"某个文件的通过数读不出来"，
   看起来像"那个文件没汇总"（一个判定同时管多处时不抄第二份）。

## 并行安全吗

判据不是"我觉得安全"，是"跑一遍逐条比"：

    .venv/Scripts/python.exe scripts/run_tests.py --serial
    .venv/Scripts/python.exe scripts/run_tests.py            # 并行
    # 两份「文件 -> 通过数」逐条比，必须同一个集合

`--counts FILE` 把结果落成 `{文件: 通过数}` 就是为了这件事，
也是为了给 `check_progress --run` 复用（它自己再跑一遍是纯重复，见那份文档）。

为什么本该安全：每个测试文件是独立进程；需要写盘的那些各自
`chdir` 到 `tempfile.mkdtemp()` 造的目录（`tests/_sandbox.py::sandbox`），
而 `check_test_pollution.py` 本来就是为"有没有人写生产目录"立的机检 ——
跑完仍然要跑它。

用法::

    .venv/Scripts/python.exe scripts/run_tests.py                # 全量，并行
    .venv/Scripts/python.exe scripts/run_tests.py --jobs 8
    .venv/Scripts/python.exe scripts/run_tests.py --serial       # 与旧跑法同序
    .venv/Scripts/python.exe scripts/run_tests.py --counts out.json
    .venv/Scripts/python.exe scripts/run_tests.py tests/test_todos.py ...

退出码：`0` 全过 · `1` 有失败 · `2` 路径问题
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from summary_parse import parse_summary            # noqa: E402 —— 同一份判据
from summary_parse import parse_summary_total      # noqa: E402


#: 文件里写一行 `#@exclusive: <组名>`，这个文件不许与别人同时跑。
#:
#: 它治的是"共用真目录"那一类：
#: `test_skill_factory` / `test_skill_spec` 会真的往 `skills/` 里建目录
#: （`create_skill` 的输出根是写死的绝对路径），跑完自己删，净变化为零
#:端到端污染自查看不见；而 `test_skill_doctor` / `test_skill_index`
#: 会扫那个目录，并行时它看见一个别的测试的中间态，报"有包不通过"。
#:
#: 修法不是"把并行退回去"—— 那等于因为几条测试就放弃了 8 倍。
#: 而是把冲突说清楚：这一组串行，其余照旧并行。
#: 判据：别把"我没防住"伪装成"不用防"。
_EXCLUSIVE_RE = re.compile(r"^\s*#@exclusive:\s*(\S+)\s*$", re.M)


def _exclusive_group(p: Path) -> str:
    """这个文件属不属于某个"独占组"；不属于返回 `""`。只读开头 60 行。"""
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            head = "".join(f.readline() for _ in range(60))
    except OSError:
        return ""
    m = _EXCLUSIVE_RE.search(head)
    return m.group(1) if m else ""


def _files(argv) -> list:
    if argv:
        return [Path(a) if Path(a).is_absolute() else (_ROOT / a) for a in argv]
    return sorted((_ROOT / "tests").glob("test_*.py"))


def _run_one(p: Path) -> tuple:
    """跑一个文件，返回 `(相对路径, 通过数|None, 退出码, 末行, 秒数)`。

    `stdin=DEVNULL` —— 不是洁癖：有几个测试会读 stdin，
       而我们自己是并行跑的，不切断的话它们会去抢同一个终端。
    `PYTHONIOENCODING=utf-8` 由子进程环境给（不是在 shell 里
       `export`）—— 这样"忘了 export"不再是这个脚本的失败模式。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               CRANNIA_INTERFACE_LANGUAGE="zh")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, str(p)], cwd=str(_ROOT), env=env,
            stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=600)
        out, rc = (r.stdout or ""), r.returncode
    except subprocess.TimeoutExpired:
        out, rc = "", 124
    dt = time.monotonic() - t0
    tail = ""
    for line in reversed((out or "").splitlines()):
        if line.strip():
            tail = line.strip()
            break
    rel = p.relative_to(_ROOT).as_posix()
    return rel, parse_summary(out or ""), rc, tail, dt, (out or "")


def _is_bad(rc: int, passed, total) -> bool:
    """这个文件算不算失败？ 判据三条，少一条就有一种失败看不见。

    1. `rc != 0` —— 最直的那条；
    2. 读不出汇总（`passed is None`）—— 读不到 ≠ 0；
    3. 通过数 < 总数 —— 只有前两条时，一个
       自己打印 `14/15 passed` 却 `exit 0` 的文件会被报成 `[ok] 14 通过`。
       抓到过：`test_destructive_gate` 在并行下偶发少一条（真红过
       一次，而那一轮的总计仍然是「0 失败」）。第三条不是防未来的，是**已经
       发生过**。

    `total is None`（那种汇总格式里没有总数），不判：没有依据就别说话，
       那正是第 2 条的同一句（读不到 ≠ 坏）。
    """
    if rc != 0 or passed is None:
        return True
    return total is not None and passed < total


def main() -> int:
    ap = argparse.ArgumentParser(description="全量测试 · 并行跑法")
    ap.add_argument("files", nargs="*", help="只跑这几个（默认全量）")
    ap.add_argument("--jobs", type=int, default=0,
                    help="并发度（0 = 自动：核数，上限 16）")
    ap.add_argument("--serial", action="store_true", help="串行（与旧跑法一致）")
    ap.add_argument("--counts", default="", help="把 {文件: 通过数} 写到这个 JSON")
    ap.add_argument("--quiet", action="store_true", help="只报失败")
    ap.add_argument("--fail-lines", type=int, default=12,
                    help="失败时把它输出的最后 N 行打出来（默认 12）")
    ap.add_argument("--dump-failures", default="",
                    help="把每个失败文件的**完整输出**写到这个目录")
    args = ap.parse_args()

    files = _files(args.files)
    if not files:
        print("!! 一个测试文件都没找到（路径不对？）")
        return 2

    jobs = 1 if args.serial else (args.jobs or min(os.cpu_count() or 4, 16))
    print("=" * 66)
    print("全量测试 · %d 个文件 · 并发 %d%s"
          % (len(files), jobs, "（串行）" if jobs == 1 else ""))
    print("=" * 66)

    # 独占组：先并行跑其余的，再把这几个串起来单独跑
    # 顺序是承重的：必须等并行那批全跑完再进独占组，
    #   否则它们照样和"读那个真目录"的文件重叠（那正是要修的东西）。
    plain = [p for p in files if not _exclusive_group(p)]
    excl = [p for p in files if _exclusive_group(p)]
    if excl:
        groups = {}
        for p in excl:
            groups.setdefault(_exclusive_group(p), []).append(p)
        print("  独占组：%s（%d 个文件**串行**跑）"
              % (", ".join("%s x%d" % (g, len(v)) for g, v in sorted(groups.items())),
                 len(excl)))

    t0 = time.monotonic()
    if jobs == 1 or not plain:
        results = [_run_one(p) for p in plain]
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            results = list(ex.map(_run_one, plain))
    # 独占组：一个文件接一个，而且是在上面那批全部结束之后
    for _p in sorted(excl):
        results.append(_run_one(_p))
    wall = time.monotonic() - t0

    failures = []
    counts = {}
    for rel, passed, rc, tail, dt, full in sorted(results):
        if passed is not None:
            counts[rel] = passed
        # 总数现从同一份输出里取（同一个解析器）—— 判据见 `_is_bad`
        bad = _is_bad(rc, passed, parse_summary_total(full))
        if bad:
            failures.append((rel, rc, passed, tail, full))
        elif not args.quiet:
            print("  [ok]   %-46s %4d 通过  %5.1fs" % (rel, passed, dt))

    # "跑不出汇总"要单独说：它和"挂了"不是一回事
    #    （读不到 ≠ 0）。把它读成失败数是一条已知的坑。
    no_summary = [f for f in failures if f[2] is None]

    print("")
    print("-" * 66)
    print("总计 %d 个文件 · **%d 失败** · 墙钟 %.1fs"
          % (len(results), len(failures), wall))
    if no_summary:
        print("[!] 其中 %d 个**跑不出汇总**（不是失败，是没汇总 —— 别当成失败数）："
              % len(no_summary))
        for rel, rc, _n, tail, _full in no_summary:
            print("   %s  rc=%d  %s" % (rel, rc, tail[:90]))
    hard = [f for f in failures if f[2] is not None]
    if hard:
        print("FAILED:")
        for rel, rc, passed, tail, full in hard:
            print("  - %s  rc=%d  %s" % (rel, rc, tail[:100]))
            # 失败了却看不见原因 = 白跑一轮。把那一段留下来（不是整个输出）。
            # 不能按 `[FAIL]` 前缀筛 —— 本仓的测试文件没有统一的失败前缀
            # （有 `[FAIL]`、有 `FAIL `、有直接 `assert msg`），筛前缀会
            # 什么都打不出来，而"失败了看不见原因 = 白跑一轮"。
            #留最后 N 行。
            if args.dump_failures:
                d = Path(args.dump_failures)
                d.mkdir(parents=True, exist_ok=True)
                (d / (rel.replace("/", "_") + ".txt")).write_text(
                    full or "", encoding="utf-8")
            keep = [l for l in (full or "").splitlines() if l.strip()]
            for l in keep[-args.fail_lines:]:
                print("      %s" % l.strip()[:150])

    if args.counts:
        Path(args.counts).write_text(
            json.dumps(counts, ensure_ascii=False, indent=1, sort_keys=True),
            encoding="utf-8")
        print("通过数已写到 %s（%d 条）" % (args.counts, len(counts)))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
