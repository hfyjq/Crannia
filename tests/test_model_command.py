#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""`/model` 的教学清单与子命令（more / set / 编号）。

    .venv/Scripts/python.exe tests/test_model_command.py

## 它钉什么

1. 无参回复不只是一句状态：带配置清单（含默认标记与撞名 id）与用法。
2. 清单最多 8 条，超出时明说还有更多并指向 `/model more`。
3. 编号解析：先按名字 / id 精确找，找不到才当编号。
4. `/model more` 打完整清单；`/model set` 在非 TUI 通道只给指路，
   绝不把密钥当命令参数收。
5. 没有配置时回用户定的那句，并指向 `crannia setup` 与 GUI 端配置页。

不碰磁盘：`model_configs` / `session_model` 的读数直接替换成假数据。

ASCII-only 输出（Windows 控制台 GBK）。
"""

from __future__ import annotations

import sys
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
    print("  [%s] %s%s" % ("ok" if cond else "!!", label,
                          ("  -- " + detail) if detail and not cond else ""))
    if cond:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(label)


ROWS = [
    ("deepseek", "m11111111", "DeepSeek", "deepseek-chat"),
    ("我的中转站", "m22222222", "OpenAI", "gpt-4o"),
    ("我的中转站", "m33333333", "OpenAI", "gpt-4.1"),
    ("Gemini", "m44444444", "Gemini", "gemini-h"),
]


class _FakeMC:
    """`core.model_configs` 的几个读数 —— 只实现这四条命令用到的。"""

    def __init__(self, rows=None, default="m11111111"):
        self.rows = list(ROWS if rows is None else rows)
        self.default = default

    def list_names(self):
        return list(self.rows)

    def default_id(self):
        return self.default

    def find_by_ref(self, ref):
        key = str(ref or "").strip().lower()
        by_id = [r for r in self.rows if r[1].lower() == key]
        if by_id:
            return {"id": by_id[0][1], "name": by_id[0][0]}
        same = [r for r in self.rows if str(r[0]).lower() == key]
        return ({"id": same[0][1], "name": same[0][0]}
                if len(same) == 1 else None)

    def name_clashes(self, ref):
        key = str(ref or "").strip().lower()
        return [{"id": r[1], "name": r[0]} for r in self.rows
                if str(r[0]).lower() == key]

    def get(self, cid):
        for r in self.rows:
            if r[1] == cid:
                return {"id": r[1], "name": r[0], "model": r[3]}
        return None

    def preflight(self, cid):
        return ""

    def set_for(self, key, cid):                # 被 _switch 替换，只作占位
        return cid


class _FakeSM:
    def __init__(self, *, frozen=False, config_id="m11111111"):
        self.info = {"config_id": config_id, "frozen": frozen}

    def describe(self, key):
        return dict(self.info)

    def follow_default(self, key):
        self.info = {"config_id": "", "frozen": False}

    def set_for(self, key, cid):
        self.switched = cid
        return cid


def _install(monkeypatch, mc, sm):
    """把 pre_process 里那两处 `from core import …` 换成假的。"""
    import core.model_configs as MC
    import core.session_model as SM
    for name in ("list_names", "default_id", "find_by_ref", "name_clashes",
                 "get", "preflight"):
        monkeypatch.setattr(MC, name, getattr(mc, name))
    for name in ("describe", "follow_default", "set_for"):
        monkeypatch.setattr(SM, name, getattr(sm, name))


def _restore(monkeypatch):
    monkeypatch.undo()


def test_no_config_points_at_setup_and_gui() -> None:
    print("\n-- 没有配置时的答复 --")
    monkeypatch = _Patch()
    mc, sm = _FakeMC(rows=[]), _FakeSM(config_id="")
    _install(monkeypatch, mc, sm)
    try:
        import core.pipeline.stages.pre_process as P
        text = P._describe_session_model("k")
        check("明确说未配置", "未配置模型" in text, text)
        check("给出终端命令", "crannia setup" in text, text)
        check("给出 GUI 入口", "GUI" in text and "端配置页" in text, text)
    finally:
        _restore(monkeypatch)


def test_following_lists_profiles_and_usage() -> None:
    print("\n-- 跟随默认：清单 + 用法 --")
    monkeypatch = _Patch()
    mc, sm = _FakeMC(), _FakeSM()
    _install(monkeypatch, mc, sm)
    try:
        import core.pipeline.stages.pre_process as P
        text = P._describe_session_model("k")
        check("首行是状态", text.startswith("跟随默认配置中。"), text)
        check("条数正确", "已配置 4 条" in text, text)
        check("默认那条带标记", "deepseek-chat，默认" in text, text)
        check("★ 撞名的两条带 id", "id m22222222" in text
              and "id m33333333" in text, text)
        check("编号从 1 起", "1、「deepseek」" in text, text)
        check("用法含切换", "/model 编号" in text and "/model 默认" in text, text)
        check("用法含 more 与 set", "/model more" in text
              and "/model set" in text, text)
    finally:
        _restore(monkeypatch)


def test_frozen_head_keeps_the_pinned_name() -> None:
    print("\n-- 冻结那一段：先报固定在哪条 --")
    monkeypatch = _Patch()
    mc, sm = _FakeMC(), _FakeSM(frozen=True, config_id="m22222222")
    _install(monkeypatch, mc, sm)
    try:
        import core.pipeline.stages.pre_process as P
        text = P._describe_session_model("k")
        check("首行是当前固定", text.startswith("当前固定：「我的中转站」。"), text)
        check("清单仍然在", "已配置 4 条" in text, text)
    finally:
        _restore(monkeypatch)


def test_limit_is_eight_and_more_is_pointed_at() -> None:
    print("\n-- 超过 8 条：只列前 8 条并指向 more --")
    monkeypatch = _Patch()
    rows = [("cfg%d" % i, "m%08d" % i, "X", "model-%d" % i)
            for i in range(1, 10)]
    mc, sm = _FakeMC(rows=rows), _FakeSM(config_id="m00000001")
    _install(monkeypatch, mc, sm)
    try:
        import core.pipeline.stages.pre_process as P
        text = P._describe_session_model("k")
        check("明说只显示前 8 条", "显示前 8 条" in text, text)
        check("第 8 条在", "8、「cfg8」" in text, text)
        check("第 9 条不在", "9、「cfg9」" not in text, text)
        check("指向 /model more", "/model more" in text, text)

        full = P._list_all_models()
        check("more 打全部", "全部配置（9 条）" in full
              and "9、「cfg9」" in full, full)
    finally:
        _restore(monkeypatch)


def test_number_after_names_and_ids() -> None:
    print("\n-- 编号解析：名字 / id 优先，最后才是编号 --")
    monkeypatch = _Patch()
    mc = _FakeMC()
    _install(monkeypatch, mc, _FakeSM())
    try:
        import core.pipeline.stages.pre_process as P
        check("名字命中", P._resolve_model_ref("Gemini")["id"] == "m44444444")
        check("id 命中", P._resolve_model_ref("m33333333")["id"] == "m33333333")
        check("编号 2 命中第二条", P._resolve_model_ref("2")["id"] == "m22222222")
        check("编号越界 -> None", P._resolve_model_ref("9") is None)
    finally:
        _restore(monkeypatch)

    #: 与上一条分开一个假账本：一条真的叫 `2` 的配置必须压过编号 2。
    monkeypatch = _Patch()
    mc = _FakeMC(rows=list(ROWS) + [("2", "m99999999", "X", "x")])
    _install(monkeypatch, mc, _FakeSM())
    try:
        import core.pipeline.stages.pre_process as P
        hit = P._resolve_model_ref("2")
        check("★ 叫 2 的配置优先于编号 2", hit and hit["id"] == "m99999999",
              repr(hit))
    finally:
        _restore(monkeypatch)


def test_switch_and_set_branches() -> None:
    print("\n-- /model 编号 与 /model set --")
    monkeypatch = _Patch()
    mc, sm = _FakeMC(), _FakeSM()
    _install(monkeypatch, mc, sm)
    try:
        import core.pipeline.stages.pre_process as P
        out = P._switch_session_model("k", "2")
        check("按编号切换到了第二条", getattr(sm, "switched", "") == "m22222222",
              getattr(sm, "switched", ""))
        check("回执说明下一轮生效", "下一条消息起生效" in out, out)

        usage = P._edit_model_guidance("")
        check("set 不带条目 -> 用法", "/model set <编号|配置名|id>" in usage, usage)

        guide = P._edit_model_guidance("2")
        check("set 带条目 -> 指名道姓", "我的中转站" in guide, guide)
        check("★ 不承诺后端收密钥", "crannia setup" in guide
              and "端配置" in guide, guide)

        miss = P._edit_model_guidance("nope")
        check("找不到时列候选", "没有找到此配置" in miss, miss)
    finally:
        _restore(monkeypatch)


def test_cmd_model_dispatch() -> None:
    print("\n-- _cmd_model 的子命令分派 --")
    import core.pipeline.stages.pre_process as P

    calls = {}
    originals = {name: getattr(P, name) for name in
                 ("_describe_session_model", "_list_all_models",
                  "_edit_model_guidance", "_switch_session_model")}

    def fake_describe(key):
        calls["describe"] = key
        return "STATUS"

    def fake_more():
        calls["more"] = True
        return "MORE"

    def fake_set(ref):
        calls["set"] = ref
        return "SET"

    def fake_switch(key, ref, **_kw):
        calls["switch"] = ref
        return "SWITCH"

    P._describe_session_model = fake_describe
    P._list_all_models = fake_more
    P._edit_model_guidance = fake_set
    P._switch_session_model = fake_switch
    try:
        stage = P.PreProcessStage()
        ctx = SimpleNamespace(metadata={}, session_pool=None, provider=None,
                              session=SimpleNamespace(platform="cli"))

        stage._cmd_model(ctx, "/model")
        check("无参 -> 状态", ctx.response == "STATUS" and "describe" in calls)

        stage._cmd_model(ctx, "/model more")
        check("more -> 完整清单", ctx.response == "MORE" and calls.get("more"))

        stage._cmd_model(ctx, "/model set 2")
        check("set -> 指路并带条目", ctx.response == "SET"
              and calls.get("set") == "2", repr(calls.get("set")))

        stage._cmd_model(ctx, "/model 3")
        check("编号 -> 切换", ctx.response == "SWITCH"
              and calls.get("switch") == "3", repr(calls.get("switch")))

        stage._cmd_model(ctx, "/model 我的 中转站")
        check("★ 带空格的配置名整段交给切换",
              calls.get("switch") == "我的 中转站", repr(calls.get("switch")))
    finally:
        for name, fn in originals.items():
            setattr(P, name, fn)


class _Patch:
    """极简 monkeypatch：只实现 setattr 与 undo。"""

    def __init__(self):
        self._undo: list[tuple] = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


def main() -> int:
    print("=" * 62)
    print("/model command guide")
    print("=" * 62)
    for fn in (
        test_no_config_points_at_setup_and_gui,
        test_following_lists_profiles_and_usage,
        test_frozen_head_keeps_the_pinned_name,
        test_limit_is_eight_and_more_is_pointed_at,
        test_number_after_names_and_ids,
        test_switch_and_set_branches,
        test_cmd_model_dispatch,
    ):
        try:
            fn()
        except Exception as exc:                             # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 抛异常（%s）" % (fn.__name__, type(exc).__name__), False)

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
