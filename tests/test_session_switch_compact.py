#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""切换模型那一刻就按新窗口重算（复核轮的两项收尾之一）。

## 它钉什么

`/model` 的不变量：切换成功、且新模型装不下历史时，当场压一次，
而不是等下一轮在溢出自救里失败一遍。

四格：
  1. 没到阈值 -> 一个字节不动（不替用户付一次摘要调用）；
  2. 到阈值 -> 压一次、写回会话池缓存、回执里带压缩结果；
  3. 压不动 -> 照实说原因（不编"已完成"）；
  4. 检查本身失败 -> 切换仍然算成功，只把失败说清楚。

另加两条接线读者：`/model` 真的把 pool / provider 交给切换函数。

ASCII-only 输出（GBK 控制台）。

用法：
    .venv/Scripts/python.exe tests/test_session_switch_compact.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.pipeline.stages import pre_process as PP  # noqa: E402

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


class FakePool:
    def __init__(self, *, usage, was_done=True, note="", post_tokens=None):
        self.msgs = [{"role": "user", "content": "hi"}]
        self.compacted = [{"role": "user", "content": "hi"},
                          {"role": "user", "content": "摘要"}]
        self.usage = usage
        self.was_done = was_done
        self.last_skip_note = note
        self.last_compression = ({"post_tokens": post_tokens}
                                 if was_done and post_tokens else None)
        self._loaded = {"chan": ("sid", self.msgs)}
        self.called = 0

    def get_or_create(self, key, provider=None):
        return ("sid", self.msgs)

    def get_context_usage(self, key, msgs):
        return dict(self.usage)

    def compact(self, key, msgs):
        self.called += 1
        return (self.compacted, self.was_done)


def test_no_compaction_under_threshold() -> None:
    pool = FakePool(usage={"estimated_tokens": 900_000,
                           "threshold_tokens": 1_000_000,
                           "context_window": 1_200_000})
    note = PP._compact_after_model_switch("chan", pool)
    check("没到阈值 -> 不加话", note == "", repr(note))
    check("没到阈值 -> 不调用压缩", pool.called == 0, str(pool.called))


def test_compaction_on_tight_window() -> None:
    pool = FakePool(usage={"estimated_tokens": 1_100_000,
                           "threshold_tokens": 1_000_000,
                           "context_window": 1_200_000},
                    post_tokens=600_000)
    note = PP._compact_after_model_switch("chan", pool)
    check("到阈值 -> 压一次", pool.called == 1, str(pool.called))
    check("回执说明立即压缩并带上压缩后的占用",
          "已立即压缩" in note and "50%" in note, repr(note))
    check("会话池缓存写回压缩后的那份",
          pool._loaded["chan"][1] is pool.compacted, repr(pool._loaded))


def test_compaction_refusal_is_reported() -> None:
    pool = FakePool(usage={"estimated_tokens": 1_100_000,
                           "threshold_tokens": 1_000_000,
                           "context_window": 1_200_000},
                    was_done=False, note="可压的轮次不够")
    note = PP._compact_after_model_switch("chan", pool)
    check("压不动 -> 照实说原因",
          "没压成" in note and "可压的轮次不够" in note, repr(note))
    check("压不动 -> 不改会话池缓存",
          pool._loaded["chan"][1] is pool.msgs, repr(pool._loaded))


def test_failure_does_not_fail_the_switch() -> None:
    class Boom(FakePool):
        def get_context_usage(self, key, msgs):
            raise RuntimeError("probe")

    note = PP._compact_after_model_switch("chan", Boom(usage={}))
    check("检查失败 -> 说清是切换成功后的附带失败",
          "切换成功" in note and "检查失败" in note, repr(note))
    check("没有 pool 时安静返回空串",
          PP._compact_after_model_switch("chan", None) == "")


def test_the_wiring_is_there() -> None:
    src = (_ROOT / "core" / "pipeline" / "stages" / "pre_process.py").read_text(
        encoding="utf-8")
    check("`/model` 把 pool / provider 交给切换函数",
          "pool=ctx.session_pool" in src and "provider=getattr(ctx" in src, "")
    check("两条切换分支（指定 / 跟随默认）都走重算",
          src.count("_compact_after_model_switch(") >= 3,  # def + 两处调用
          str(src.count("_compact_after_model_switch(")))


def main() -> int:
    print("=" * 62)
    print("Model switch: compact immediately against the new window")
    print("=" * 62)
    for fn in (test_no_compaction_under_threshold,
               test_compaction_on_tight_window,
               test_compaction_refusal_is_reported,
               test_failure_does_not_fail_the_switch,
               test_the_wiring_is_there):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                            # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
