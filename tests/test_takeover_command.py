# -*- coding: utf-8 -*-
"""`/takeover on|off` —— 接管开关的文本入口。

## 钉住什么

1. 不在完全权限时拒绝并给路线（与 `/api/computer-use/enable` 同一道判据）；
2. 完全权限下 on 成功 -> 一句带 `/takeover off` 的回执；
3. on 失败（比如别的会话正占着）-> 原因原样带出来，不吞；
4. off 只关自己持有的那次（别的会话开着的不替它关）；
5. 无参 -> 看状态 + 用法；大小写不敏感（`ON` 也认）。

全部用替身，不碰真的接管：`session.enable` 一旦真跑会去装全局键盘钩子。

用法::

    .venv/Scripts/python.exe tests/test_takeover_command.py

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.computer import session as CS  # noqa: E402
from core import modes as M  # noqa: E402
from core.pipeline.stages.pre_process import _handle_takeover  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


class Fake:
    """接管会话的替身：记下 enable/disable 的参数，可注入结果。"""

    def __init__(self, *, enable=(True, ""), enabled=False,
                 disable_for=True):
        self.calls: list = []
        self.enable_result = enable
        self._enabled = enabled
        self._disable_for = disable_for

    def enable(self, sid):
        self.calls.append(("enable", sid))
        return self.enable_result

    def enabled_for(self, sid):
        self.calls.append(("enabled_for", sid))
        return self._enabled

    def disable_for_session(self, sid, reason=""):
        self.calls.append(("disable_for_session", sid, reason))
        return self._disable_for


def _patched(fake: Fake, frozen: str | None):
    """临时换掉两个模块的函数；返回还原函数。"""
    old_enable = CS.enable
    old_enabled = CS.enabled_for
    old_disable = CS.disable_for_session
    old_frozen = M.frozen_mode
    CS.enable = fake.enable
    CS.enabled_for = fake.enabled_for
    CS.disable_for_session = fake.disable_for_session
    M.frozen_mode = lambda sid: frozen

    def restore() -> None:
        CS.enable = old_enable
        CS.enabled_for = old_enabled
        CS.disable_for_session = old_disable
        M.frozen_mode = old_frozen
    return restore


def test_refuses_outside_full_access() -> None:
    fake = Fake()
    restore = _patched(fake, "auto")
    try:
        msg = _handle_takeover(["/takeover", "on"], "s1")
        check("非完全权限 -> 明确拒绝", "仅在完全权限下支持" in msg, msg)
        check("并给出下一步（切换至完全权限）", "完全权限" in msg and "重试" in msg, msg)
        check("拒绝时不调用 enable", not any(c[0] == "enable" for c in fake.calls),
              repr(fake.calls))
    finally:
        restore()


def test_enable_in_full_access() -> None:
    fake = Fake(enable=(True, ""))
    restore = _patched(fake, "full_access")
    try:
        msg = _handle_takeover(["/takeover", "on"], "s1")
        check("成功回执", "已启用接管模式" in msg, msg)
        check("告诉用户怎么关", "/takeover off" in msg, msg)
        check("按本会话启用", ("enable", "s1") in fake.calls, repr(fake.calls))
        # 大小写不敏感
        msg2 = _handle_takeover(["/takeover", "ON"], "s1")
        check("大写 ON 也认", "已启用接管模式" in msg2, msg2)
    finally:
        restore()


def test_enable_failure_reason_is_reported() -> None:
    fake = Fake(enable=(False, "另一个对话正在接管这台电脑"))
    restore = _patched(fake, "full_access")
    try:
        msg = _handle_takeover(["/takeover", "on"], "s1")
        check("失败原因原样带出来", "另一个对话正在接管这台电脑" in msg, msg)
        check("不假称成功", "已启用接管模式" not in msg, msg)
    finally:
        restore()


def test_off_only_closes_own_takeover() -> None:
    fake = Fake(disable_for=True)
    restore = _patched(fake, "full_access")
    try:
        msg = _handle_takeover(["/takeover", "off"], "s1")
        check("自己开着 -> 已关闭", "已关闭接管模式" in msg, msg)
        check("按本会话关", ("disable_for_session", "s1", "user") in fake.calls,
              repr(fake.calls))
    finally:
        restore()

    fake2 = Fake(disable_for=False)
    restore = _patched(fake2, "full_access")
    try:
        msg = _handle_takeover(["/takeover", "off"], "s2")
        check("不是持有者 -> 不替别人关", "没有在接管" in msg, msg)
    finally:
        restore()


def test_no_argument_shows_state_and_usage() -> None:
    fake = Fake(enabled=False)
    restore = _patched(fake, "auto")
    try:
        msg = _handle_takeover(["/takeover"], "s1")
        check("无参 -> 看状态", "当前接管状态" in msg, msg)
        check("并给用法", "用法" in msg and "on|off" in msg, msg)
    finally:
        restore()
    fake2 = Fake(enabled=True)
    restore = _patched(fake2, "full_access")
    try:
        msg = _handle_takeover(["/takeover"], "s1")
        check("开着时状态是「已开启」", "已开启" in msg, msg)
    finally:
        restore()


def main() -> int:
    print("=" * 62)
    print("/takeover command")
    print("=" * 62)
    test_refuses_outside_full_access()
    test_enable_in_full_access()
    test_enable_failure_reason_is_reported()
    test_off_only_closes_own_takeover()
    test_no_argument_shows_state_and_usage()

    print()
    print("=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - " + f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
