"""测试输出的汇总行 —— 三种格式，一份判据。

跑全量的那个脚本（`run_tests.py`）与状态锚点机检（`check_progress.py`）
读的是同一份输出，必须用同一把尺子：两处各写一份正则，迟早会给出两个数，
而两个数都不报错。

## 三种格式

| 形状 | 例子 |
|---|---|
| `N/M passed` | `12/12 passed` —— M 是总数，不是失败数 |
| `Results: N passed, M failed` | 没有总数那一格 |
| `N tests: N passed, M failed` | 总数在最前 |

## 总数不能丢

判「这个文件坏没坏」时只有 `rc` 与通过数是不够的：一个自己打印
`14/15 passed` 却 `exit 0` 的文件会被报成通过。总数也一起出来，
判据才多得出第三条。

行首可能有缩进，正则一律 `^\\s*`。
"""

from __future__ import annotations

import re

#: 按顺序试。三种汇总格式并存，行首有缩进。
SUMMARY_PATTERNS = (
    r"^\s*(\d+)/(\d+) passed",          # `N/M passed`（M 是总数，不是失败数）
    r"Results:\s*(\d+) passed",
    r"^\s*(\d+) tests:\s*(\d+) passed",  # 总数在最前
)


def match_summary(out: str):
    """从测试输出里读出 (通过数, 总数 | None)；读不出返回 `None`。"""
    for pat in SUMMARY_PATTERNS:
        m = re.search(pat, out, re.M)
        if m:
            total = int(m.group(2)) if m.lastindex and m.lastindex >= 2 else None
            return int(m.group(1)), total
    return None


def parse_summary(out: str):
    """从测试输出里读出通过数；读不出返回 `None`（`None` ≠ 0）。"""
    got = match_summary(out)
    return got[0] if got else None


def parse_summary_total(out: str):
    """同一个汇总行里的总数；那个格式里没有总数就返回 `None`。"""
    got = match_summary(out)
    return got[1] if got else None
