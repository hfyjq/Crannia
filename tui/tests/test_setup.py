"""`crannia setup`：首次引导。

钉五件事：

1. 还没有默认配置时 `needs_setup` 为真；有默认就不再挡启动。
2. 选卡 → 名称 → 模型 → 密钥，全部落进那一句子进程调用（TUI 不 import core）。
3. 回车 = 「其他」；它才问地址。普通卡用 manifest 给的地址与默认模型。
4. 联通测试失败后只改一项再测；跳过也保留配置并设为默认。
5. 非交互环境直接拒（密钥要从键盘读）。

子进程用假的 `query_fn` 顶掉，不碰真的配置。

ASCII-only 输出（Windows GBK 控制台）。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from crannia_tui import setup as S  # noqa: E402

_n = 0

CARDS = [
    {"id": "gpt", "label": "GPT", "provider": "openai",
     "base_url": "https://api.openai.com/v1", "default_model": "gpt-5"},
    # Anthropic 原生 SDK 自己补地址 —— manifest 里就是空串。
    {"id": "claude", "label": "Claude", "provider": "anthropic",
     "base_url": "", "default_model": "claude-sonnet-4-5"},
    {"id": "deepseek", "label": "Deepseek", "provider": "deepseek",
     "base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-flash"},
    {"id": "other", "label": "其他", "provider": "openai",
     "base_url": "", "default_model": ""},
]


def check(cond: bool, msg: str) -> None:
    global _n
    _n += 1
    if not cond:
        raise AssertionError(f"#{_n} {msg}")


def run_capture(fn) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def make_query(*, state=None, probe_results=None, calls=None):
    """按代码里的动作分流；不认识的动作直接报错（免得断言靶子打偏）。"""
    probes = list(probe_results if probe_results is not None else [True])
    seen = calls if calls is not None else []

    def run(code: str, timeout: float = 25.0) -> str:
        seen.append(code)
        if "mc.card_defaults" in code:
            return json.dumps(CARDS, ensure_ascii=False)
        if "mc.default_id()" in code:
            return json.dumps(state or {"default": "", "name": "", "model": ""},
                              ensure_ascii=False)
        if "mc.create(" in code:
            return json.dumps({"ok": True, "id": "m12345678"})
        if "mc.update(" in code:
            return json.dumps({"ok": True})
        if "mc.set_default(" in code:
            return json.dumps({"ok": True})
        if "mc.probe(" in code:
            item = probes.pop(0) if probes else True
            if isinstance(item, dict):
                return json.dumps(item, ensure_ascii=False)
            ok = bool(item)
            return json.dumps({"ok": ok, "kind": "" if ok else "auth",
                               "error": "" if ok else "401: invalid api key"},
                              ensure_ascii=False)
        raise AssertionError("没有预料到的子进程调用：" + code)
    return run, seen


def answers(values):
    """按顺序喂答案；同时记下每条提示词（`input` 自己负责打印提示，
    替身不写 stdout —— 要断言提示文案就得从这里取）。"""
    it = iter(values)
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("还有提示没喂答案：" + prompt) from None
    ask.prompts = prompts                      # type: ignore[attr-defined]
    return ask


def test_needs_setup() -> None:
    run, _ = make_query()
    check(S.needs_setup(Path("py"), Path("."), query_fn=run) is True,
          "没有默认配置时要走引导")
    run2, _ = make_query(state={"default": "m1", "name": "n", "model": "x"})
    check(S.needs_setup(Path("py"), Path("."), query_fn=run2) is False,
          "已经有默认配置时不挡启动")


def test_plain_card_flow() -> None:
    run, seen = make_query()
    ask = answers(["", "1", "", "", ])          # 开始 / 选第 1 张 / 名称默认 / 模型默认
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk-test", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    creates = [c for c in seen if "mc.create(" in c]
    check(len(creates) == 1, repr(seen))
    code = creates[0]
    check("'gpt'" in code, "卡 id 要落进创建调用：" + code)
    check("'GPT'" in code, "名称默认取卡名：" + code)
    check("'gpt-5'" in code, "模型默认取 manifest：" + code)
    check("https://api.openai.com/v1" in code, "地址取 manifest：" + code)
    check("'sk-test'" in code, "密钥要落进创建调用")
    check(any("mc.set_default(" in c and "m12345678" in c for c in seen),
          "最后要设默认：" + repr(seen))
    check("已确认" in out, out)


def test_other_card_asks_for_address() -> None:
    run, seen = make_query()
    ask = answers(["", "", "", "my-model", "https://example.test/v1"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    code = [c for c in seen if "mc.create(" in c][0]
    check("'other'" in code, code)
    check("https://example.test/v1" in code, "「其他」要用用户填的地址：" + code)
    check("'my-model'" in code, code)


def test_pick_by_label() -> None:
    run, seen = make_query()
    ask = answers(["", "Deepseek", "", ""])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        S.setup_command(python=Path("py"), home=Path("."),
                        query_fn=run, ask=ask,
                        secret=lambda p: "sk", interactive=True)
    code = [c for c in seen if "mc.create(" in c][0]
    check("'deepseek'" in code, "按名字也能选：" + code)
    check("'deepseek-flash'" in code, code)


def test_list_explains_missing_addresses() -> None:
    """两种"没有地址"要分开说：SDK 决定 vs 自己填。"""
    run, seen = make_query()
    ask = answers(["", "Claude", "", ""])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    check("（地址由 SDK 决定）" in out, "Claude 那一行要说明地址由 SDK 定：" + out)
    check("模型列表：" in out, "列表前要有标题：" + out)
    other_line = [ln for ln in out.splitlines() if "其他" in ln and "回车" not in ln]
    check(bool(other_line) and not other_line[0].rstrip().endswith("）"),
          "「其他」那一行不显示地址：" + repr(other_line))
    code = [c for c in seen if "mc.create(" in c][0]
    check("'claude'" in code and "''" in code, "Claude 的地址留空：" + code)


def test_probe_failure_names_the_kind_and_suggests_the_item() -> None:
    """报错要分类型：模型名不对 -> 标签 + 默认改 2 号。"""
    run, seen = make_query(probe_results=[
        {"ok": False, "kind": "model", "error": "model `x` does not exist"}, True])
    ask = answers(["", "1", "", "", "", "gpt-5-mini"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    check("（模型名错误）" in out, "要说出类别：" + out)
    check(any("[2]" in p and "直接回车以更改推荐条目" in p for p in ask.prompts),
          "默认项要跟着类别走，并说明回车=改推荐项：" + repr(ask.prompts))
    updates = [c for c in seen if "mc.update(" in c]
    check(bool(updates) and '"model"' in updates[0], "要改的是模型那一格：" + repr(updates))


def test_repeated_failure_uses_the_same_banner() -> None:
    """连续失败不另起口吻 —— 与第一次逐字同一个呈现。"""
    fail = {"ok": False, "kind": "connection", "error": "Connection error."}
    run, _ = make_query(probe_results=[dict(fail), dict(fail), True])
    ask = answers(["", "1", "", "", "", "https://example.test/v1", "0"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    check(out.count("API 异常（连接错误）：") == 2, "两次失败两行横幅：" + out)
    check("还是不通" not in out, "不再有另一个口吻：" + out)


def test_probe_failure_fix_key_then_pass() -> None:
    run, seen = make_query(probe_results=[False, True])
    ask = answers(["", "1", "", "", "3"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk-new", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    updates = [c for c in seen if "mc.update(" in c]
    check(len(updates) == 1, repr(seen))
    check("sk-new" in updates[0], "只改密钥那一格：" + updates[0])
    check("还是不通" not in out, out)


def test_probe_failure_skip_keeps_and_defaults() -> None:
    run, seen = make_query(probe_results=[False])
    ask = answers(["", "1", "", "", "0"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask,
                             secret=lambda p: "sk", interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    check("已保留，联通测试未通过" in out, out)
    check(any("mc.set_default(" in c for c in seen), repr(seen))


def test_existing_default_can_be_kept() -> None:
    run, seen = make_query(state={"default": "m1", "name": "旧配置",
                                  "model": "x"})
    ask = answers(["n"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, ask=ask, interactive=True)
    out = buf.getvalue()
    check(rc == 0, out)
    check("旧配置" in out, out)
    check(not any("mc.create(" in c for c in seen), "保留时不许写账本：" + repr(seen))


def test_non_interactive_refuses() -> None:
    run, seen = make_query()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.setup_command(python=Path("py"), home=Path("."),
                             query_fn=run, interactive=False)
    check(rc == 2, buf.getvalue())
    check(seen == [], "拒之前不该碰账本：" + repr(seen))
    check("终端" in buf.getvalue(), buf.getvalue())


def test_snippets_prepare_the_data_root() -> None:
    """动账本的子进程要先 `paths.prepare()`。

    安装版里 `data/…` 是相对路径，靠入口那一下 chdir 才指到用户数据根；
    TUI 的子进程不是入口 —— 不 prepare 就读到安装目录里那份模板
    （开发树两者重合，所以别的测试看不出来）。
    """
    from crannia_tui import channel as C
    from crannia_tui import profile as P

    snippets = [S._STATE_CODE, S._CREATE_CODE, S._UPDATE_CODE,
                S._DEFAULT_CODE, S._PROBE_CODE,
                P._LIST_CODE, P._SWITCH_CODE,
                C._LIST_CODE, C._CREATE_CODE, C._UPDATE_CODE,
                C._DELETE_CODE, C._DEFAULT_CODE]
    bad = [c[:40] for c in snippets
           if not c.startswith("from core import paths as _paths; _paths.prepare()")]
    check(not bad, "这些子进程没先 prepare 数据根：%r" % (bad,))


def test_running_backend_is_written_through_the_endpoint() -> None:
    """有后端在跑时，建配置与设默认走它的端点（不让它的内存镜像过期）。"""
    from crannia_tui import profile as P

    calls: list = []
    old_live, old_post = S._live_endpoint, P.http_json
    S._live_endpoint = lambda *_a: {"port": 5000, "token": "t"}
    P.http_json = staticmethod(lambda ep, path, payload=None:
                               calls.append((path, payload)) or
                               ({"ok": True, "entry": {"id": "cid-9"}}
                                if path.endswith("/create") else {"ok": True}))
    try:
        q, _seen = make_query(state={"default": "", "name": "", "model": ""},
                              probe_results=[True])
        rc = S.setup_command(python=Path("py"), home=Path("."), query_fn=q,
                             ask=answers(["", "1", "", ""]),
                             secret=lambda _p: "sk-test", interactive=True)
    finally:
        S._live_endpoint = old_live
        P.http_json = old_post
    check(rc == 0, "引导应成功")
    paths = [c[0] for c in calls]
    check(paths == ["/api/model-configs/create", "/api/model-configs/default"],
          "建配置与设默认都该走端点，实际 %r" % (paths,))
    check(calls[0][1].get("card") == "gpt", "卡片要传过去：%r" % (calls[0][1],))
    check(calls[1][1].get("id") == "cid-9", "默认那条用端点回的 id：%r" % (calls[1][1],))


def main() -> int:
    S._live_endpoint = lambda *_a: None   # 测试不联网、不探测端点
    for fn in [test_needs_setup, test_plain_card_flow,
               test_other_card_asks_for_address, test_pick_by_label,
               test_list_explains_missing_addresses,
               test_probe_failure_names_the_kind_and_suggests_the_item,
               test_repeated_failure_uses_the_same_banner,
               test_probe_failure_fix_key_then_pass,
               test_probe_failure_skip_keeps_and_defaults,
               test_existing_default_can_be_kept,
               test_non_interactive_refuses,
               test_running_backend_is_written_through_the_endpoint,
               test_snippets_prepare_the_data_root]:
        fn()
    print(f"{_n} checks: {_n} passed, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
