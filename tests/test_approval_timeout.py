"""审批超时（`core/approval_timeout.py`）—— auto 档下"没人答"的那条出路。

钉住四件事：

1. 读不到 -> 默认 150；`0` 是合法值 = 关；
2. 坏值 -> 关 + 出声（不是用默认硬套）—— 判据：
   辅助件坏了不替用户做决定（"因为一个坏配置把用户的事自动毙掉"比
   "超时没生效"坏得多）；
3. `should_arm` 挂 `auto` 与 `full_access` 两档 ——
   这两档的前提都是「用户可能不在」；手动 / 计划不挂（人在场，
   超时会把正要批的那件事也毙掉）；
4. `bool` 必须先挡（`isinstance(True, int)` 是 `True`）。

[!] 输出一律 ASCII（Windows 控制台 GBK）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 项目根必须自己插（`tests/` 是 `sys.path[0]`，而 cwd 不进 sys.path）。
# 不插的后果不是"找不到" —— 本机 `.venv` 里有一条
# `_editable_impl_crannia.pth` 指着另一份 checkout
#`from core import …` 会静默地 import 另一个仓库，而测试照跑、输出照绿。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import sandbox, prod_snapshot, prod_diff  # noqa: E402

from core import approval_timeout as AT  # noqa: E402

_FAILED: list[str] = []
_PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS
    if cond:
        _PASS += 1
        print("  [ok]   " + name)
    else:
        _FAILED.append(name)
        print("  [FAIL] " + name + (("  <- " + detail) if detail else ""))


def _write(settings) -> None:
    p = Path("data") / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")


def _fresh_bad_note():
    AT._bad_noted.clear()


def test_default_when_absent():
    with sandbox():
        check("★ 没有设置文件 -> 默认 150（不让一轮对话因为一个设置失败）",
              AT.seconds() == AT.DEFAULT_SECONDS, str(AT.seconds()))
        _write({})
        check("★ 文件在、但**没有这个键** -> 默认",
              AT.seconds() == AT.DEFAULT_SECONDS, str(AT.seconds()))
        _write({"web_token": "x"})
        check("★ 有别的键、没有它 -> 还是默认",
              AT.seconds() == AT.DEFAULT_SECONDS, str(AT.seconds()))


def test_zero_means_off_and_is_legal():
    with sandbox():
        for v in (0, -1):
            _write({AT.SETTING_KEY: v})
            check("★ %r -> **关**（0 是合法值，不是坏值）" % (v,),
                  AT.seconds() == 0, str(AT.seconds()))


def test_good_values_pass_through():
    with sandbox():
        for v in (1, 30, 150, 3600):
            _write({AT.SETTING_KEY: v})
            check("★ %d 秒原样读出来" % v, AT.seconds() == v, str(AT.seconds()))


def test_bad_values_turn_it_off_not_default():
    """这一条是方向：坏值 -> 关，不是 -> 150。

    判据：超时是辅助件、不是判据。辅助件坏了，
    不许替用户做决定 —— 而"用默认值硬套"就是替他决定
    （他明明写了个看不懂的值，我们却按 150 秒把他正要批的事毙了）。
    """
    with sandbox():
        for v in ("abc", True, False, 3.5, None, [150], {"s": 1}, 99999):
            _fresh_bad_note()
            _write({AT.SETTING_KEY: v})
            got = AT.seconds()
            check("★★ %r -> **关**（不是 150）" % (v,), got == 0, str(got))
        # `True` 单独再钉一次：它是 `int` 的子类，不先挡就会被当成 1 秒
        _fresh_bad_note()
        _write({AT.SETTING_KEY: True})
        check("★★ `true` 不许被当成 1 秒（`isinstance(True, int)` 是 True）",
              AT.seconds() == 0, str(AT.seconds()))


def test_bad_setting_speaks_once():
    with sandbox():
        _fresh_bad_note()
        _write({AT.SETTING_KEY: "abc"})
        AT.seconds("sess-a")
        check("★ 喊过一次之后不再重复喊（同一个会话）",
              AT._already_noted("sess-a"), "")
        check("★ 但**别的会话**还会喊（免得一整轮都静默）",
              not AT._already_noted("sess-b"), "")


def test_should_arm_in_auto_and_full_access():
    """`auto` 与 `full_access` 挂；手动 / 计划不挂。

    每个档一个独立会话键 —— `current_mode` 第一次问就冻结
      （进提示缓存键，那条 45x 尖峰），同一个键连问两次只会拿到第一次的档。
    `full_access` 不能写进 `default_mode`（`default_allowed=False`），
      它只能 `set_mode` 按会话开。
    """
    with sandbox():
        from core import modes as M
        for mode in ("auto", "full_access"):
            sid = "arm-%s" % mode
            _write({AT.SETTING_KEY: 150})
            M.set_mode(sid, mode)
            check("★★ %s 档 -> 挂倒计时" % mode,
                  AT.should_arm(sid) is True, str(AT.should_arm(sid)))
        for mode in ("manual", "plan"):
            sid = "arm-%s" % mode
            _write({AT.SETTING_KEY: 150})
            M.set_mode(sid, mode)
            check("★★ %s 档 -> 不挂（人在场）" % mode,
                  AT.should_arm(sid) is False, str(AT.should_arm(sid)))
        _write({AT.SETTING_KEY: 0})
        sid = "arm-auto"
        check("★ 秒数 0 -> 不挂（关掉了）",
              AT.should_arm(sid) is False, "")


class _Handle:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeLoop:
    """假 loop —— 只记下"谁在多少秒后要跑"，不真的等。"""

    def __init__(self):
        self.calls = []

    def call_later(self, delay, fn, *args):
        h = _Handle()
        self.calls.append((delay, fn, args, h))
        return h


def test_arm_and_cancel_and_forget():
    """定时器的三段生命周期：arm -> cancel -> forget。

    这一格证明的是"挂在哪、撤不撤得掉、清不清得干净"，
    不是"到点真的会去结算"（那要一个真 loop + 真事件，见
    `test_approval_dispatch.py` 那三条）。
    """
    with sandbox():
        _fresh_bad_note()
        AT.forget()
        _write({AT.SETTING_KEY: 150, "default_mode": "auto"})
        sid = "probe:private:%s" % __name__
        AT.remember(sid, object())

        loop = _FakeLoop()
        check("★★ auto 档 + 有待审批 -> 挂上（延时就设置里那个数）",
              AT.arm(sid, loop, "FIRE") is True
              and len(loop.calls) == 1 and loop.calls[0][0] == 150.0,
              str(loop.calls)[:120])
        check("★ 幂等：再挂一次**不会**多一个定时器",
              AT.arm(sid, loop, "FIRE") is False and len(loop.calls) == 1, "")

        AT.cancel(sid)
        check("★★ 撤得掉（句柄 `cancel()` 被调到）",
              loop.calls[0][3].cancelled is True, "")
        check("★★ 但**那一格还在**（事件还要留给下一轮重入用）",
              sid in AT._timers, "")

        AT.forget(sid)
        check("★★★ `/clear` 清得干净（`purge_session` 走的就是它）",
              sid not in AT._timers, "")


def test_arm_in_full_access_but_not_manual():
    """负对照：full_access 挂、manual 不挂。"""
    with sandbox():
        from core import modes as M
        _fresh_bad_note()
        AT.forget()
        _write({AT.SETTING_KEY: 150})
        sid = "probe2:private:%s" % __name__
        M.set_mode(sid, "manual")
        AT.remember(sid, object())
        loop = _FakeLoop()
        check("★★ manual 档 -> 不挂（人在场）",
              AT.arm(sid, loop, "FIRE") is False and not loop.calls, str(loop.calls))
        _write({AT.SETTING_KEY: 150})
        AT.forget()
        sid3 = "probe4:private:%s" % __name__
        M.set_mode(sid3, "full_access")
        AT.remember(sid3, object())
        loop3 = _FakeLoop()
        check("★★ full_access 档 -> 挂（用户可能不在）",
              AT.arm(sid3, loop3, "FIRE") is True and bool(loop3.calls), str(loop3.calls))
        AT.forget()
        # 关掉（秒数 0）要不挂 —— 唯一的变量就是秒数。
        _write({AT.SETTING_KEY: 0, "default_mode": "auto"})
        AT.forget()
        sid2 = "probe3:private:%s" % __name__
        AT.remember(sid2, object())
        loop2 = _FakeLoop()
        check("★★ auto 档但秒数 0 -> 也不挂（关掉了）",
              AT.arm(sid2, loop2, "FIRE") is False and not loop2.calls, str(loop2.calls))
        AT.forget()


def main() -> int:
    tests = [
        test_default_when_absent,
        test_zero_means_off_and_is_legal,
        test_good_values_pass_through,
        test_bad_values_turn_it_off_not_default,
        test_bad_setting_speaks_once,
        test_should_arm_in_auto_and_full_access,
        test_arm_and_cancel_and_forget,
        test_arm_in_full_access_but_not_manual,
    ]
    before = prod_snapshot()
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))
    leaked = prod_diff(before, prod_snapshot())
    check("★★ 测试没碰生产目录", not leaked, str(leaked)[:200])

    print("")
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + len(_FAILED), _PASS, len(_FAILED)))
    return 1 if _FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
