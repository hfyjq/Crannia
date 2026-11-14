#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型连接重试。

形态：网络错误和其他错误时，绿色横幅变成红色横幅常驻，
内容（网络错误为例）'网络连接异常--请检查宽带互联网连接    正在重试 1/10'，
重试间隔越来越慢：1s/2s/3s/5s/10s/15s/30s/60s/60s；一旦连上，横幅变绿、
取消常驻、恢复原样。

## 钉住什么

  1. 节奏：`RETRY_DELAYS_MS` 就是那 9 个 + 第 10 个 60s（计数器是 n/10）
  2. `note()` 记状态 + 发 `llm.retry`；`clear()` 清 + 发 `llm.retry_ok`
  3. 只有连接 / 超时会重试（401 / 429 / 超长不重试）
  4. 两个 provider 的同步与流式都接了（源码级 + 一个真跑的重试循环）

[!] 输出一律 ASCII（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_llm_retry.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


# ----------------------------------------------------------------------
# 1. 节奏与状态
# ----------------------------------------------------------------------

def test_schedule_and_state():
    print("\n-- 1. 节奏 = 用户给的那串 --")
    from core import llm_retry as R
    check("★★ 10 个间隔（用户给了 9 个，计数器是 n/10 -> 第 10 个沿用 60s）",
          len(R.RETRY_DELAYS_MS) == 10 and R.MAX_RETRIES == 10,
          repr(R.RETRY_DELAYS_MS))
    check("★★ 前九个**逐字**是 1/2/3/5/10/15/30/60/60 秒",
          R.RETRY_DELAYS_MS[:9] == (1000, 2000, 3000, 5000, 10000, 15000,
                                    30000, 60000, 60000),
          repr(R.RETRY_DELAYS_MS[:9]))
    check("★ 越界取最后一项（不会 IndexError）", R.delay_for(99) == 60000)

    class _Bus:
        def __init__(self):
            self.events = []

        def emit(self, type_, session_key="", **data):
            self.events.append((type_, session_key, data))

    from core.events import set_event_bus
    old = None
    bus = _Bus()
    set_event_bus(bus)
    try:
        d = R.note("s1", 1, kind="connection", text="Connection error.")
        check("★ `note` 返回该睡多久（第 1 次 = 1s）", d == 1000, repr(d))
        rec = R.get("s1")
        check("★★ 状态里有 `retry` / `max` / `delay_ms` / `kind`",
              rec and rec["retry"] == 1 and rec["max"] == 10
              and rec["delay_ms"] == 1000 and rec["kind"] == "connection",
              repr(rec))
        check("★★ 发了 `llm.retry`（界面立刻翻红）",
              any(t == "llm.retry" for t, _, _ in bus.events), repr(bus.events))
        check("★ 事件里的会话键是通道键", bus.events[0][1] == "s1")

        check("★★ `clear` 清掉并回 True", R.clear("s1") is True and R.get("s1") is None)
        check("★★ 发了 `llm.retry_ok`（横幅变绿）",
              any(t == "llm.retry_ok" for t, _, _ in bus.events), repr(bus.events))
        check("★ 再清一次回 False（不重复发事件）", R.clear("s1") is False)

        #: 空会话键 = 辅助调用 / 测试直驱，不记状态、不发事件
        before = len(bus.events)
        d = R.note("", 3, kind="connection")
        check("★ 空会话仍返回延时（重试本身照常）", d == 3000)
        check("★ 但不记状态、不发事件", R.get("") is None and len(bus.events) == before)

        #: 过期
        R.note("s2", 1, kind="connection")
        R._state["s2"]["ts"] = time.time() - R.EXPIRY_S - 5
        check("★ 过期状态读成 None（不会永远挂一条红横幅）", R.get("s2") is None)

        R.note("s3", 1, kind="connection")
        R.reset("s3")
        check("★ `reset`（`/clear`）清掉状态", R.get("s3") is None)
    finally:
        R.reset("s1"), R.reset("s2"), R.reset("s3")
        set_event_bus(old)


def test_step_phases():
    """节奏只有一处（`step`）：常规 10 次 -> 静默 10 次 -> 放弃。"""
    from core import llm_retry as R

    st: dict = {}
    kinds = []
    delays = []
    for _ in range(R.MAX_RETRIES + R.SILENT_MAX + 2):
        act, d = R.step(st, "connection")
        kinds.append(act)
        delays.append(d)
    check("★★ 先 10 次常规重试",
          kinds[:R.MAX_RETRIES] == ["retry"] * R.MAX_RETRIES, repr(kinds[:3]))
    check("★★ 再 10 次静默（每 120 秒一次）",
          kinds[R.MAX_RETRIES:R.MAX_RETRIES + R.SILENT_MAX]
          == ["silent"] * R.SILENT_MAX
          and all(d == R.SILENT_DELAY_MS
                  for d in delays[R.MAX_RETRIES:R.MAX_RETRIES + R.SILENT_MAX]),
          repr(delays[R.MAX_RETRIES:R.MAX_RETRIES + 2]))
    check("★★ 静默也用完之后放弃",
          kinds[R.MAX_RETRIES + R.SILENT_MAX] == "giveup", repr(kinds[-2:]))
    check("★ 常规那 10 次按 1s/2s/3s… 递增",
          delays[:3] == [1000, 2000, 3000], repr(delays[:3]))

    for k in ("balance", "request", "auth", "rate_limit", "content",
              "region", "too_large", "context_overflow"):
        check("★ %s 不重试" % k, R.step({}, k) == ("no_retry", 0))
    for k in ("server_fault", "server_busy", "server_other", "timeout"):
        check("★ %s 会重试" % k, R.step({}, k)[0] == "retry")


# ----------------------------------------------------------------------
# 2. Anthropic 那条：真跑一次重试循环
# ----------------------------------------------------------------------

def test_anthropic_sync_retry():
    print("\n-- 2. Anthropic 同步：连不上 -> 按节奏重试 -> 连上 --")
    from providers.anthropic.client import _create_with_retry
    from core import llm_retry as R

    class _Conn(Exception):
        def __str__(self):
            return "Connection error."

    #: 异常类名要像 SDK 那样带 Connection（`classify_transport_error` 按名字认）
    _Conn.__name__ = "APIConnectionError"

    calls = {"n": 0}

    class _Msgs:
        def create(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise _Conn()
            return "OK"

    slept = []
    real_wait = R.wait
    real_note = R.note

    def _fake_wait(sk, delay_ms):
        slept.append(delay_ms / 1000.0)
        return True                 # 睡满（没被打断）

    def _fake_note(sk, idx, **kw):
        slept.append(("note", idx))
        return real_note(sk, idx, **kw)

    R.wait = _fake_wait
    R.note = _fake_note
    try:
        out = _create_with_retry(SimpleNamespace(messages=_Msgs()), {})
        check("★ 第三次成功", out == "OK" and calls["n"] == 3, repr(calls))
        check("★★ 等了两次，节奏是 1s 与 2s",
              [s for s in slept if isinstance(s, float)] == [1.0, 2.0], repr(slept))
        check("★ 重试序号递增（1 -> 2）",
              [s[1] for s in slept if isinstance(s, tuple)] == [1, 2], repr(slept))
        check("★ 成功后状态已清", R.get("") is None)
    finally:
        R.wait = real_wait
        R.note = real_note


# ----------------------------------------------------------------------
# 3. 两个 provider 的两条路都接了
# ----------------------------------------------------------------------

def test_wait_is_cancellable():
    print("\n-- 2b. 重试的等待**可打断**（『还没办法打断』那条反馈）--")
    import core.cancellation as C
    from core import llm_retry as R

    real = C.is_cancelled
    try:
        C.is_cancelled = lambda sid: True
        check("★★ 已打断 -> `wait` 立刻回 False（不等满 60s）",
              R.wait("s1", 60_000) is False)
    finally:
        C.is_cancelled = real
    check("★ 没打断时睡满回 True（100ms 小睡）", R.wait("s1", 100) is True)


def test_provider_wiring():
    print("\n-- 3. 接线：两个 provider × 同步/流式 --")
    oa = (_ROOT / "providers" / "openai_compat.py").read_text(encoding="utf-8")
    an = (_ROOT / "providers" / "anthropic" / "client.py").read_text(encoding="utf-8")
    check("★★ 重试判定只在一处（`llm_retry.step`），provider 不再自己数次数",
          "llm_retry.step(rstate" in oa and "conn_retries" not in oa,
          "providers 里还留着自己的计数 = 两套节奏")
    check("★★ OpenAI 流式：**交出过内容就不重试**",
          "if emitted" in oa and "no_retry" in oa)
    check("★★ 成功那一下清状态（两条路各一处）",
          oa.count("llm_retry.clear(_session_key())") >= 2,
          repr(oa.count("llm_retry.clear(_session_key())")))
    check("★ Anthropic 同步走 `_create_with_retry`", "_create_with_retry(self._client" in an)
    check("★ Anthropic 流式也重试（`not emitted`）",
          "if emitted" in an and "llm_retry.step(rstate" in an)
    check("★★ 收尾时挂一条「失败」横幅（不重试的那些也要看得见）",
          oa.count("llm_retry.fail(") >= 2 and "llm_retry.fail(" in an)
    check("★★ SDK 自带的内部重试关掉（否则界面上的次数与实际对不上）",
          "max_retries=0" in oa)

    ui = (_ROOT / "packages" / "ui" / "src" / "StatusBar.jsx").read_text(encoding="utf-8")
    act = (_ROOT / "packages" / "ui" / "src" / "ActivityBlock.jsx").read_text(encoding="utf-8")
    app = (_ROOT / "packages" / "ui" / "src" / "App.jsx").read_text(encoding="utf-8")
    api = (_ROOT / "packages" / "ui" / "src" / "api.js").read_text(encoding="utf-8")
    check("★★ 红横幅：进行中与「没救回来」两个形态都在",
          "retry" in ui and "status-bar retry" in ui and "data.failed" in act)
    check("★ 文案走 `retryLabel(kind, text)`（后端那句优先），次数走 `retryCounter`",
          "retryLabel(retry.kind, retry.text)" in ui and "retryCounter" in ui)
    check("★★ 静默期与收尾不显示次数（`phase`）",
          "phase" in ui and "silent" in ui and "failed" in ui)
    check("★★ 加事件 = 两处（名单 + 订阅）",
          '"llm.retry"' in api and 'subscribe("llm.retry"' in app
          and 'subscribe("llm.retry_ok"' in app)
    check("★ `/api/status` 有兜底读数",
          '"llm_retry"' in (_ROOT / "web" / "server.py").read_text(encoding="utf-8"))
    #: 重试的 `sleep` 必须可打断。
    check("★★ 两个 provider 的等待都用 `llm_retry.wait*`（可打断）",
          "llm_retry.wait(_session_key()" in oa and "llm_retry.wait_async" in oa
          and "llm_retry.wait(_session_key()" in an and "llm_retry.wait_async" in an)
    check("★★ 红横幅上有「打断」（`status-bar-cancel`）",
          "status-bar-cancel" in ui and "onCancel" in ui)
    css = (_ROOT / "packages" / "ui" / "src" / "styles.css").read_text(encoding="utf-8")
    check("★★ 红底**纯白字**（用户点名）", ".status-bar.retry" in css and "var(--c-on-danger)" in css)
    #: 重试期间步骤流要藏起来；轮末必须清状态。
    check("★★ 重试期间步骤流整块藏起来（恢复后自己回来）",
          "m.live && llmRetry ? null" in app)
    sched = (_ROOT / "core" / "pipeline" / "scheduler.py").read_text(encoding="utf-8")
    check("★★ 轮末清「还在重试」的状态（否则 `/api/status` 每 5 秒把红条复活）",
          "_lr.reset_if_retrying(session_key)" in sched)
    check("★★ 失败收尾的那条**留着**（那是结果，用户得看得见）",
          "reset_if_retrying" in (_ROOT / "core" / "llm_retry.py").read_text(encoding="utf-8")
          and '"failed"' in (_ROOT / "core" / "llm_retry.py").read_text(encoding="utf-8"))
    check("★★ 新一轮开始时收掉上一轮的红条（换成绿条）",
          "_lr.clear(session_key)" in sched)


def main() -> int:
    print("=" * 62)
    print("llm retry -- red banner + backoff")
    print("=" * 62)
    for fn in (test_schedule_and_state, test_step_phases, test_anthropic_sync_retry,
               test_wait_is_cancellable, test_provider_wiring):
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
