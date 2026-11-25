"""缓存断裂检测的双阈值与抑制规则。

钉五组判据：
  1. 相对 5% 且绝对 2000 同时满足才报；
  2. 三种合法下降不误报（压缩、无上报、无基线）；
  3. 无上报不与 0 混（读不到 ≠ 没命中）；
  4. 抑制之后重新建基线，不把后续观测全废；
  5. 纯函数不改入参、不接主链。

ASCII-only 输出（GBK 控制台）。

用法：
    .venv/Scripts/python.exe tests/test_cache_break_detector.py
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.cache_break_detector import (  # noqa: E402
    ABSOLUTE_DROP, RELATIVE_DROP, BreakWatcher, Sample, judge,
)

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(desc: str, ok: bool, note: str = "") -> None:
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(desc)


def main() -> int:
    print("=" * 62)
    print("Cache break detection: dual thresholds / suppression / no-report")
    print("=" * 62)

    base = Sample(100_000)
    print("\n-- dual thresholds --")
    v = judge(base, Sample(94_000))       # -6%, -6000
    check("both thresholds exceeded -> broken", v.broken and v.reason == "broken",
          str(v))
    check("below relative -> not broken",
          not judge(base, Sample(99_000)).broken)      # -1%
    check("below absolute -> not broken",
          not judge(Sample(30_000), Sample(29_000)).broken)  # -3.3%, -1000
    check("one token over both thresholds is broken",
          judge(base, Sample(94_999)).broken)
    check("one token under the relative threshold is not",
          judge(base, Sample(95_001)).reason == "below_relative")

    print("\n-- legitimate drops --")
    check("no report is never broken",
          not judge(base, Sample(0, reported=False)).broken)
    check("no report reason is no_report",
          judge(base, Sample(0, reported=False)).reason == "no_report")
    check("compaction suppression is never broken",
          not judge(base, Sample(0, suppressed=True)).broken)
    check("no baseline is never broken", not judge(None, base).broken)
    check("previous no-report cannot be a baseline",
          not judge(Sample(0, reported=False), Sample(94_000)).broken)
    check("previous suppressed cannot be a baseline",
          not judge(Sample(0, suppressed=True), Sample(94_000)).broken)

    print("\n-- watcher state --")
    watcher = BreakWatcher()
    check("first observation establishes baseline",
          not watcher.observe("s1", base).broken)
    check("drop after baseline is detected",
          watcher.observe("s1", Sample(94_000)).broken)
    check("suppressed observation does not replace baseline",
          not watcher.observe("s1", Sample(0, suppressed=True)).broken)
    check("suppression does not overwrite the baseline",
          watcher.observe("s1", Sample(94_000)).reason == "no_drop")
    check("next real drop against the preserved baseline is detected",
          watcher.observe("s1", Sample(88_000)).broken)
    watcher.forget("s1")
    check("forget clears the baseline", not watcher.observe("s1", base).broken)

    print("\n-- constants and purity --")
    check("relative threshold is five percent", RELATIVE_DROP == 0.05)
    check("absolute threshold is 2000 tokens", ABSOLUTE_DROP == 2000)
    s = Sample(100_000)
    judge(s, Sample(90_000))
    check("samples are frozen and unchanged", s.hit == 100_000)

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
