# -*- coding: utf-8 -*-
"""长跑循环的健康度 —— 「闪了一下」与「死了九小时」必须长得不一样。

为什么有这个文件
================
一次挂机一晚的导出

| 观测量 | 实际 |
|---|---|
| 总行数 | 883 |
| `[wechat] Poll error` | 881（99.8%） |
| 去重后的字面 | 2 种 |
| 时间戳 | 0 个 |
| 启动横幅 | 不在（导出是从会话中段开始的） |

结论推翻了直觉：1 行 / 2 秒 = 0.5 行/秒，那不是"重试风暴"。
真正的缺陷是 「无界 + 无形状」：

  · 无形状，881 行里没有一行能回答「它已经这样多久了」。
    「闪了一下」与「死了九小时」在盘上留下的痕迹完全一样。
  · 无界，挂 10 小时 ≈ 18,000 行，把别的日志淹掉。

所以这个模块解的不是"重试太快"，是**「让日志的体量随停顿时长的对数增长，
并让每一行都带着时长」**：

    前 3 次失败   → 每次都报（用户要能立刻看到「它开始不对了」）
    此后          → 只在 1m / 2m / 4m / 8m … 的里程碑报
    每次报        → 都带「已连续失败多久」+「下次等多久」
    恢复          → 报一行（否则"好了"这件事无人知道）

一次 10 小时停机只留 ~13 行，而每一行都说得清它在哪一段。

两个消费者共用这一份判定（`channels/wechat_channel/handler.py` 的
轮询循环 · `core/workspace.py` 的 `_BaseWatcher`）——
一个判定同时管多处，不抄第二份（抄了就会漂移，而漂移了没有症状）。

它不是"重试框架" —— 这里没有任何 I/O、没有 `asyncio`、不 sleep。
它只回答两个问题：这次要不要出声、该等多久。调用方自己 sleep。

用法::

    h = LoopHealth("wechat")
    while running:
        try:
            updates = await get_updates()
        except TimeoutError:
            if (line := h.ok()) is not None:
                print(line)                 # 恢复那一行
            continue
        except Exception as exc:
            wait, line = h.fail(exc)
            if line is not None:
                print(line)
            await asyncio.sleep(wait)
"""

from __future__ import annotations

#: 退避的封顶（秒）。与 `telegram_channel` 逐字相同是有意的 ——
#: 同一个问题不该有两个答案。
MAX_BACKOFF_S = 30.0

#: 指数被夹到几 —— `2 ** 5 = 32 > 30`，所以夹到 5 之后再乘方结果不变。
#:
#: 必须先夹指数、再乘方。写成 `min(2.0 ** retries, 30)` 的话，
#: Python 先算乘方，`2.0 ** 1200` 抛 `OverflowError: Result too large`。
#: 一次 10 小时停机（≈1200 次失败）会把循环自己打崩。
#: （`telegram_channel` 那份写的是 `2 ** retries`，int 基不会溢出 ——
#:  那是侥幸不是设计：它先造一个几千位的大整数再取 min。）
MAX_BACKOFF_EXP = 5

#: 连续失败时多久报一行的里程碑（秒）—— 取 2 的幂。
LOG_MILESTONES_S: tuple[int, ...] = (
    60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 30720,
)

#: 前几次每次都报 —— 静默的头几秒会让人以为一切正常。
ALWAYS_LOG_FIRST = 3

#: 连续失败超过这么久，报一句诊断（只报一次）。
#:
#: 它只出声，不自动恢复 —— 恢复动作（重新登录 / 重建连接）几乎总是
#: 交互式的，塞进循环会把整条通道卡死。实例：微信的 `do_login` 自己写着
#: *"Blocks until user confirms on phone"*。
DIAGNOSE_AFTER_S = 1800.0


def exc_chain(exc: BaseException, max_depth: int = 5) -> str:
    """把异常连同它的因果链摊成一行。

    ## 为什么非有不可

    微信通道那个"长挂机之后就连不上"的 bug 出现过多次，
    而每次的签名逐字相同：

        一次 `ReadError` 打头  ->  之后永远 `ConnectError: All connection attempts failed`

    查不出根因 —— 问题就在这一行旧写法上：

        f"({type(exc).__name__}: {exc})"

    · `httpx.ConnectError` 的 `str()` 只有 "All connection attempts failed"
      那句通用文案；真正的 `OSError`（拒绝？超时？DNS？）在 `__cause__` 里，
      被这一行扔掉了。
    · `httpx.ReadError` 的 `str()` 是空的，日志里是 `ReadError: `，什么都没说。

形状修好了（计数/退避/里程碑），内容还是空的。
    这条把"永远查不出来"变成"下一次就知道"。

    深度夹到 *max_depth*、并用 `id()` 去重 —— 异常链可以是环
    （`e.__cause__ is e` 或 A→B→A），不夹的话这一行自己会挂死。
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(parts) < max_depth and id(cur) not in seen:
        seen.add(id(cur))
        txt = str(cur)
        if not txt:
            # 空消息要明说"是空的"，别让读者以为是格式坏了（读不到 != 0）
            txt = f"(消息为空) {cur!r}"
        parts.append(f"{type(cur).__name__}: {txt}")
        cur = cur.__cause__ or cur.__context__
    if cur is not None:
        parts.append("...")
    return " <- ".join(parts)


def backoff(retries: int) -> float:
    """指数退避，封顶 `MAX_BACKOFF_S`。`retries` 从 1 起。

    先夹指数再乘方（见 `MAX_BACKOFF_EXP`）—— 任何 `retries` 都不会溢出。
    """
    return float(min(2 ** min(max(retries, 0), MAX_BACKOFF_EXP), MAX_BACKOFF_S))


def should_log_failure(retries: int, elapsed: float, milestone_i: int) -> bool:
    """这一次失败该不该打一行？—— 纯函数，所以那条对数判据能被独立验。"""
    if retries <= ALWAYS_LOG_FIRST:
        return True
    return (milestone_i < len(LOG_MILESTONES_S)
            and elapsed >= LOG_MILESTONES_S[milestone_i])


def advance_milestone(elapsed: float, milestone_i: int) -> int:
    """把里程碑指针推到「已跨过的最大值」—— 一次失败可能跨过好几个。"""
    while (milestone_i < len(LOG_MILESTONES_S)
           and elapsed >= LOG_MILESTONES_S[milestone_i]):
        milestone_i += 1
    return milestone_i


def fmt_elapsed(seconds: float) -> str:
    """人类可读的时长 —— 挂在日志行里，回答「已经多久了」。"""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


class LoopHealth:
    """一个长跑循环的失败记账。纯状态 + 纯字符串，不 sleep、不 I/O。

    调用方要传一个单调时钟（`now`）—— 不在这里读 `time.monotonic()`，
    否则它就不可测了（探针要能被独立验）。
    """

    __slots__ = ("name", "_retries", "_down_since", "_milestone_i", "_diagnosed")

    def __init__(self, name: str) -> None:
        self.name = name
        self._retries = 0
        self._down_since = 0.0
        self._milestone_i = 0
        self._diagnosed = False

    @property
    def retries(self) -> int:
        return self._retries

    def ok(self, now: float | None = None) -> str | None:
        """循环成功了一次。返回「恢复」那一行（本来就没坏过，`None`）。"""
        if not self._retries:
            return None
        line = (f"[{self.name}] recovered — 连续失败 {self._retries} 次共 "
                f"{fmt_elapsed((now or 0.0) - self._down_since)}，已恢复")
        self._retries = 0
        self._down_since = 0.0
        self._milestone_i = 0
        self._diagnosed = False
        return line

    def fail(self, exc: BaseException, now: float) -> tuple[float, str | None]:
        """循环失败了一次。返回 `(该等多久, 要说的话或 None)`。"""
        self._retries += 1
        if self._retries == 1:
            self._down_since = now
            self._milestone_i = 0
            self._diagnosed = False
        elapsed = now - self._down_since
        wait = backoff(self._retries)

        line = None
        if should_log_failure(self._retries, elapsed, self._milestone_i):
            line = (f"[{self.name}] error #{self._retries} "
                    # 换成异常链（原来是 `{type(exc).__name__}: {exc}`，
                    #   它把 `__cause__` 扔了 —— 那正是查不出根因的原因）
                    f"({exc_chain(exc)}) — 已连续失败 "
                    f"{fmt_elapsed(elapsed)}，{wait:.0f}s 后重试")
            self._milestone_i = advance_milestone(elapsed, self._milestone_i)

        # 诊断只报一次，且追加在上面那行之后（不挤掉它）
        if not self._diagnosed and elapsed >= DIAGNOSE_AFTER_S:
            self._diagnosed = True
            note = (f"[{self.name}] [!] 已连续失败 {fmt_elapsed(elapsed)} —— "
                    f"这不像一次闪断。若是凭证过期或对端不可达，"
                    f"**重启并重新建立连接**；继续重试不会自行恢复。")
            line = (line + "\n" + note) if line else note
        return wait, line
