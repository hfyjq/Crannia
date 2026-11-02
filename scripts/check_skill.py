"""skill 兼容性体检的命令行入口。

判据全在 `core/skill_doctor.py`（纯函数、只读、不执行任何东西）。
这个脚本只是它的一个包装 —— 与本仓其它 `check_*.py` 同一个形状。

用法（先 `export PYTHONIOENCODING=utf-8`）：

    .venv/Scripts/python.exe scripts/check_skill.py            # 体检全部
    .venv/Scripts/python.exe scripts/check_skill.py --quiet    # 只报不通过的
    .venv/Scripts/python.exe scripts/check_skill.py file_parser   # 只体检一个

退出码：`0` = 全部通过 · `1` = 有不通过 · `2` = 用法错 / 目录不在。

它不 import 任何 skill，也不去 boot 注册表 —— 所以重名那一项它查不了
（会被登记进 `skipped`，不是"没问题"）。要连重名一起查，走 `/skill check`
（那条路在框架里，注册表是热的）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.skill_doctor as SD  # noqa: E402

#: 「这一项为什么没跑」—— 只对真的进了 `skipped` 的那些打印。
#: 不能无条件打这两行，否则没跑的检查那一段会说假话：
#: `handler_tool_presence` 明明跑了（没有包用 `import *`），
#: 却照样被写成"没跑"。判据：**不许把「跑了没问题」
#: 和「没跑」说成同一件事** —— 反过来也一样。
_SKIP_HINTS = {
    SD.SKIP_NO_REGISTRY:
        "需要在框架里跑（注册表是热的才查得了归属），见 `/skill check`",
    SD.SKIP_STAR_IMPORT:
        "该包 handler.py 有 `from X import *`，静态判不了",
}


def _print_one(diag: SD.Diagnosis, verbose: bool) -> None:
    print("  %-18s %s" % (diag.skill, SD.summary_line(diag)))
    if verbose or not diag.ok:
        for f in diag.findings:
            if f.severity == SD.SEV_INFO and diag.ok and not verbose:
                continue
            print("      [%s] %s" % (f.severity, f.message))


def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    verbose = "--verbose" in argv or "-v" in argv
    names = [a for a in argv[1:] if not a.startswith("-")]

    base = _ROOT / "skills"
    if not base.is_dir():
        print("[check_skill] 找不到 skills/（%s）" % base)
        return 2

    if names:
        targets = [base / n for n in names]
        missing = [str(p) for p in targets if not (p / "manifest.yaml").is_file()]
        if missing:
            print("[check_skill] 这些不是 skill 包：%s" % ", ".join(missing))
            return 2
    else:
        targets = SD.discover(base)

    if not targets:
        print("[check_skill] skills/ 下一个包都没有 —— 路径对吗？")
        return 2

    print("=" * 66)
    print("skill 兼容性体检 —— %d 个包，%s" % (len(targets), base))
    print("* 静态检查：不 import、不执行、不改任何东西")
    print("=" * 66)

    diags = [SD.examine(p) for p in targets]
    bad = [d for d in diags if not d.ok]
    skipped_all: set[str] = set()
    for d in diags:
        skipped_all |= set(d.skipped)
        if quiet and d.ok and not verbose:
            continue
        _print_one(d, verbose)

    print()
    print("=" * 66)
    if skipped_all:
        # 「没查」必须说出来（判据 12：缺失不许塌成零）
        print("[!] 这些检查**没跑**（不是「没问题」）：%s" % ", ".join(sorted(skipped_all)))
        for code in sorted(skipped_all):
            why = _SKIP_HINTS.get(code)
            print("   - %s -- %s" % (code, why or "（没写理由 —— 去补 _SKIP_HINTS）"))
    if bad:
        print("结论: FAIL -- %d/%d 个包不兼容：%s"
              % (len(bad), len(diags), ", ".join(d.skill for d in bad)))
        print()
        # 用户给的稿 —— 控制台这一份走 `ui=False`（徽章换 ASCII）：
        # 带符号那一份是给 UI/JSON 的，进 `print` 会在 GBK 控制台上崩
        # （`check_gbk_safe.py` 的基线 60 不许增长）。
        print("-" * 66)
        print(SD.compat_message(bad[0], ui=False))
        return 1
    print("结论: PASS -- %d 个包全部通过" % len(diags))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
