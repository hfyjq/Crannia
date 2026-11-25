"""缓存命中断裂检测 —— 只读观测量，不改请求。

## 它回答什么

"这一次缓存命中数掉下去，是不是我们把前缀弄坏了？"
合法下降（压缩、删消息、TTL 过期、上游抖动）不该报；真断裂该报。

## 双阈值

抄成熟客户端的口径：相对下降超过 5% 并且绝对下降超过 2000 token 才算断裂。
两个都要满足 —— 只看相对会把小命中的正常波动当事故，只看绝对会漏掉
大前缀上的 5% 滑坡（那可是几万 token）。

## 抑制规则

以下情况这次观测整条丢弃（不是记成 0），因为它们的下降是预期内的：

- 压缩 / 清理刚刚发生（调用方传 `suppressed=True`）；
- 上游没报缓存字段（`reported=False`）—— 读不到就是读不到，不是没命中；
- 上一轮没有可用基线。

抑制之后下一轮会重新建立基线，不会因为一次压缩就把后续观测全废掉。

## 不接主链

本模块只提供纯函数与可选的会话级观测器。它不打日志、不发事件、不写文件，
调用方要拿结果自己决定怎么用 —— 这一格是"能看出来"，不是"自动修"。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 相对阈值 5%，绝对阈值 2000 token。两者同时满足才报警。
RELATIVE_DROP = 0.05
ABSOLUTE_DROP = 2000


@dataclass(frozen=True)
class Sample:
    """一次观测。reported=False 时 hit 无意义（读不到，不是 0）。"""

    hit: int
    reported: bool = True
    suppressed: bool = False


@dataclass(frozen=True)
class Verdict:
    broken: bool
    reason: str
    baseline: int
    hit: int


def judge(previous: Sample | None, current: Sample) -> Verdict:
    """对比两次观测，判断是否断裂。纯函数，不保存状态。"""
    if not current.reported:
        return Verdict(False, "no_report", 0, 0)
    if current.suppressed:
        return Verdict(False, "suppressed", 0, current.hit)
    if previous is None or not previous.reported or previous.suppressed:
        return Verdict(False, "no_baseline", 0, current.hit)

    old, new = max(0, int(previous.hit)), max(0, int(current.hit))
    drop = old - new
    if drop <= 0:
        return Verdict(False, "no_drop", old, new)
    # 相对下降按上一轮算：上一轮是 0 时没有相对概念，不报。
    if old == 0:
        return Verdict(False, "zero_baseline", old, new)
    relative = drop / old
    if relative < RELATIVE_DROP:
        return Verdict(False, "below_relative", old, new)
    if drop < ABSOLUTE_DROP:
        return Verdict(False, "below_absolute", old, new)
    return Verdict(True, "broken", old, new)


class BreakWatcher:
    """按会话键记住上一轮观测。只存必要状态，不碰请求路径。

    状态是模块内的字典，登记在 `core/state_registry.py` 的 PROCESS_SHARED。
    """

    def __init__(self) -> None:
        self._last: dict[str, Sample] = {}

    def observe(self, key: str, sample: Sample) -> Verdict:
        previous = self._last.get(key)
        result = judge(previous, sample)
        if sample.reported and not sample.suppressed:
            self._last[key] = sample
        return result

    def forget(self, key: str) -> None:
        self._last.pop(key, None)
