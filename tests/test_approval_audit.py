"""审批的留痕 —— 审计记录。

## 它在防什么

审批若没有任何留痕记录，遇到同类问题就查不出因。措辞要准：
不是"审批有 bug"，是"审批没有痕迹" —— 后者更慢才发作。

## 本文件钉的六条

  1. 按选项表判 outcome，不按数字判 —— `2` 在 `cross_zone` 里是
     "缩小范围"、在 `egress` 里是"本次对话内不再问"、在 `delete_bulk` 里是
     不同意。按数字猜会把用户的"不"记成"是"（`core/approval.py:366` 那条老坑）。
  2. 审计里必须看得见 `kind` / `key` / `subject`（事后第一个问题就是"哪一条"）。
  3. `matchedRule` 记"结算之后哪一条会命中"，拒绝时是 `none`。
  4. 超时也留痕，而且 outcome 是 `unavailable`（不是 `rejected`）——
     那两条给模型的指令方向相反。
  5. 审计失败不许影响结算（结算已经发生了）· 不许抛。
  6. 自动模式那条路也留痕，而且 `rule` 的字段名是 `classifierRule`
     （与授权表的 `matchedRule` 分开 —— 它们该去查两份不同的证据）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_approval_audit.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _sandbox import sandbox  # noqa: E402

from core.security import SecurityGuard  # noqa: E402


# ----------------------------------------------------------------------
# 夹具：一个真的把行记下来的审计收口
# ----------------------------------------------------------------------

class _Guard(SecurityGuard):
    """真的 `SecurityGuard`，只是把 `_audit` 收到的行抄一份下来。

    不用 MagicMock 替身 —— 那样测到的是"我调了它"，而这一批要的正是
    "那一行长什么样"（字段名、顺序、`none` 怎么写）。
    """

    lines: list = []

    def _audit(self, action: str, target: str) -> None:   # noqa: D102
        _Guard.lines.append((action, target))
        # 不落盘：真 `_audit` 会写文件，而本文件在 sandbox 里跑。
        #    父类那行不调（判据：这一批只关心内容）。


def _guard():
    _Guard.lines = []
    return _Guard(allowed_dirs=[], command_blacklist=[],
                  audit_log_path=os.devnull)


class _with_guard:
    """把注册表里的 workspace manager 换成一个带 security 的替身。"""

    def __init__(self, guard):
        class _WM:
            security = guard
        self._wm = _WM()

    def __enter__(self):
        import bus.registry as R
        self._prev = R._registry.get("workspace_manager")
        R._registry["workspace_manager"] = self._wm
        return self

    def __exit__(self, *exc):
        import bus.registry as R
        R._registry["workspace_manager"] = self._prev
        return False


class _S:
    def __init__(self, key, platform=""):
        self._key = key
        self.platform = platform

    def __str__(self):
        return self._key


def _ctx(key="audit:private:probe"):
    from core.pipeline.stage import StageContext
    return StageContext(session=_S(key), user_message="")


def _pend(**over):
    p = {"active": True, "stage": "ask", "call_id": "c1",
         "tool_name": "probe", "arguments": "{}", "kind": "egress",
         "subject": "example.com"}
    p.update(over)
    return p


def _lines():
    """只挑审批那几条（这一批的产物）。"""
    return [(a, t) for (a, t) in _Guard.lines if str(a).startswith("APPROVAL_")]


# 1-3. 手动结算：outcome / 字段 / matchedRule

def test_an_approved_approval_is_recorded_with_its_subject():
    """批了，一行 `APPROVAL_APPROVED`，且看得见是哪一条。"""
    from core.pipeline.stages import pre_process as PP
    with sandbox():
        g = _guard()
        with _with_guard(g):
            ctx = _ctx()
            pid = id(ctx.pending_egress)
            ctx.pending_egress = _pend()
            ctx.pending_egress["_pid"] = pid
            PP._answer_ask("egress", ctx.pending_egress, "1", ctx)
        got = _lines()
    assert len(got) == 1, got
    act, tgt = got[0]
    assert act == "APPROVAL_APPROVED", act
    assert "kind=egress" in tgt, tgt
    assert "key=1" in tgt, tgt
    assert "subject=example.com" in tgt, tgt
    assert "matchedRule=" in tgt, tgt


def test_the_outcome_comes_from_the_option_table_not_the_digit():
    """同一个数字在不同的表里意思相反 —— 而这一条就是那个判据。

    `delete_bulk` 的 `2` 是不同意（它只有两项），而 `egress` 的 `2` 是
    "本次对话内不再询问"，按数字判会把用户的不记成是。
    """
    from core.pipeline.stages import pre_process as PP
    with sandbox():
        g = _guard()
        with _with_guard(g):
            ctx = _ctx()
            ctx.pending_delete_bulk = _pend(kind="delete_bulk",
                                            subject="a.docx", approval_kind="delete_bulk",
                                            file_paths=["a.docx"])
            PP._answer_ask("delete_bulk", ctx.pending_delete_bulk, "2", ctx)
        got = _lines()
    assert len(got) == 1, got
    act, tgt = got[0]
    assert act == "APPROVAL_REJECTED", (
        "`delete_bulk` 的 2 是「不同意」，而审计记成了 %s —— "
        "按数字猜就是这个后果" % act, act)
    assert "matchedRule=none" in tgt, tgt


def test_a_wildcard_grant_is_recorded_as_wildcard():
    """`matchedRule` 分得开「点名的那一条」与「设置页那个裸 `*`」。

    用 `"1"`（第一档就给的键）—— 第 5 项要到 `escalate_at` 次才出现，
       给一个没发出去的键，`parse_reply` 根本不认，走不到结算
       （那会看起来像审计没写）。
    """
    from core.pipeline.stages import pre_process as PP
    with sandbox():
        g = _guard()
        with _with_guard(g):
            import bus.registry as R
            wm = R.get_workspace_manager()

            class _Real:
                security = g

                def matched_target_grant(self, kind, subject, **_kw):
                    return "wildcard"

            R._registry["workspace_manager"] = _Real()
            try:
                ctx = _ctx()
                ctx.pending_egress = _pend()
                PP._answer_ask("egress", ctx.pending_egress, "1", ctx)
            finally:
                R._registry["workspace_manager"] = wm
        got = _lines()
    assert got and got[0][0] == "APPROVAL_APPROVED", got
    assert "matchedRule=wildcard" in got[0][1], got[0][1]


# 4-5. 超时也留痕；审计失败不弄挂结算

def test_a_timeout_is_recorded_as_unavailable_not_rejected():
    """超时，`unavailable`。

    与"人拒了"分开是承重的：那两条给模型的指令方向相反
    （一条说"不要重试"、一条说"换条路"），审计里合成就等于把事后能查到的
    事实也弄错。
    """
    from core.pipeline.stages import pre_process as PP
    with sandbox():
        g = _guard()
        with _with_guard(g):
            ctx = _ctx()
            pend = _pend()
            PP._settle_timeout(pend, ctx)
        got = _lines()
    assert len(got) == 1, got
    assert got[0][0] == "APPROVAL_UNAVAILABLE", got[0]
    assert "subject=example.com" in got[0][1], got[0][1]


def test_a_broken_audit_does_not_break_the_settlement():
    """审计失败，只印一行，结算照旧（结算已经发生了）。"""
    from core.pipeline.stages import pre_process as PP
    with sandbox():
        class _Boom(SecurityGuard):
            def _audit(self, action, target):
                raise RuntimeError("磁盘满了")

        boom = _Boom(allowed_dirs=[], command_blacklist=[],
                     audit_log_path=os.devnull)
        with _with_guard(boom):
            ctx = _ctx()
            ctx.pending_egress = _pend()
            PP._answer_ask("egress", ctx.pending_egress, "1", ctx)
            # 结算真的发生了（pending 被清掉、给模型的话写好了）
            assert not ctx.pending_egress.get("active"), "结算被审计拖垮了"
            assert ctx.user_message, "结算没有把话写给模型"


def test_auditing_an_unknown_kind_does_not_raise():
    """认不出来的 kind，`unknown`，不抛。"""
    from core.pipeline.stages.pre_process import _audit_approval
    with sandbox():
        g = _guard()
        with _with_guard(g):
            _audit_approval("no_such_kind_at_all", "x", "1", 1)
    assert _lines() and _lines()[0][0] == "APPROVAL_UNKNOWN", _lines()


# 6. 自动模式那条路：字段名分开

def test_the_auto_path_records_with_a_different_field_name():
    """自动模式也是一次审批决定（它代答的正是审批），也要留痕。

    而且 `rule` 的字段名是 `classifierRule` —— 与授权表的 `matchedRule`
      分开：它们该去查两份不同的证据（分类器说了什么 vs 哪条授权命中）。
      合成一个字段的后果：事后看到 `rule=xxx` 分不清该去查哪一份。
    """
    from core.agent_loop import _audit_auto
    with sandbox():
        g = _guard()
        with _with_guard(g):
            _audit_auto("run_command", "blocked", "rm -rf 是危险操作")
        got = _lines()
    assert len(got) == 1, got
    act, tgt = got[0]
    assert act == "APPROVAL_BLOCKED", act
    assert tgt.startswith("kind=auto:run_command"), tgt
    assert "classifierRule=rm -rf 是危险操作" in tgt, tgt
    assert "matchedRule" not in tgt, (
        "两处 rule 合成一个字段了 —— 事后分不清该查哪一份证据", tgt)


# 收尾

def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print("  [ok] %s" % t.__name__)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  FAIL  %s: %s" % (t.__name__, e))
    print("\n%d tests: %d passed, %d failed" % (passed + failed, passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
