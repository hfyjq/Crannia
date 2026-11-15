"""窗口的四层来源与 45% 记忆档的机械读者。

钉住两批判据：

  四层 + 来源（`core/window_estimate.py`）
  · 优先级 `config声明 > 实测 > 查表 > manifest兜底 > 保守兜底`（每一步一条）；
  · 实测只上不下（一次成功的请求给的是下界，不构成"窗口更小"的证据）；
  · 多候选不猜（归一化后撞多个，落回下一层）；
  · 表坏 / 缺 provider，落回下一层（"读不到"要有降级路，不是拿 0 顶上）；
  · 表的映射必须跟着 `providers/` 目录长（目录里多一家而映射没有，红）；
  · 源级读者：`observe` 必须在 early-return 之前 ——
    顺序反了，下界就只在"已经超过声明窗口"时才记账（而那正是最不需要它的情形）。

  45% 记忆档 —— 两个纯函数判据
  · 「记忆」档的上限 = `窗口 × 0.45`（"没有上限"会让这一档**一装载完就踩在
    压缩阈值 0.85 上** —— 884,303 = 84.33% + 0.67%）；
  · 来源是"兜底"时降级（记忆档 → 均衡档）。

全程 `sandbox()`（chdir 临时目录）—— 但表是随代码发布的数据，
它按模块位置解析、不受 chdir 影响（那正是一条要钉住的行为）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_window_table.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _sandbox import sandbox  # noqa: E402

from core import window_estimate as W  # noqa: E402


def _load_refetch():
    """`scripts/refetch_window_table.py`（它不是包，按路径加载）。"""
    p = _PROJECT_ROOT / "scripts" / "refetch_window_table.py"
    spec = importlib.util.spec_from_file_location("_refetch_wt", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# 1. 表本体
# ----------------------------------------------------------------------

def test_table_loads_and_has_provenance():
    """表要能读，而且带出处（`source` / `fetched`）—— 一张不知道哪来的表
    比没有表更坏（来源必须跟着值走）。"""
    data = json.loads(W.TABLE_PATH.read_text(encoding="utf-8"))
    assert data.get("source", "").startswith("https://models.dev"), data.get("source")
    assert data.get("fetched"), "表里没有抓取日期"
    rows = data["providers"]
    assert len(rows) >= 10, "表里的 provider 太少了（摘漏了？）"
    total = sum(len(v) for v in rows.values())
    assert total >= 100, "条目数 %d 看着不像摘过的" % total
    # 表的一条记录从裸整数改成嵌套字典。
    #   两种形状都认（`_row_field`），但这张表自己必须是 schema 2 ——
    #   否则 `output` / `reasoning` 两格永远读不到，而"读不到"看起来像"没有"。
    for prov, models in rows.items():
        for name, row in models.items():
            assert isinstance(row, dict), (
                f"表还是 schema 1 的裸整数（{prov}/{name} = {row!r}）—— "
                f"重跑 scripts/refetch_window_table.py")
            ctx = row.get("context")
            assert isinstance(ctx, int) and ctx > 0, (prov, name, ctx)
            if "output" in row:
                assert isinstance(row["output"], int) and row["output"] > 0, \
                    (prov, name, row["output"])
            if "reasoning" in row:
                r = row["reasoning"]
                # `reasons`（上游那个布尔）与 `types`（可切换的选项）
                #   是两件事 —— 只带 `{"reasons": False}` 是一条合法的记录
                #   （例如 `azure/codestral-2501` 就长这样）：它说的是
                #   「这家明确没有思考能力」，不是"没摘到"。
                assert isinstance(r, dict) and (
                    "types" in r or "reasons" in r), (prov, name, r)


def test_table_path_is_module_relative_not_cwd():
    """表是随代码发布的数据，按模块位置解析，不按 cwd。

    用相对路径的话，`sandbox()`（或任何 chdir）之后它会静默读空表 ——
    而"读不到"与"没有这一层"长得一模一样。
    """
    assert W.TABLE_PATH.is_absolute(), "TABLE_PATH 是相对路径"
    with sandbox():
        assert W.load_table(path=W.TABLE_PATH), "chdir 之后表读不出来了"
        # 惰性缓存那条也要能过（默认路径同样不受 cwd 影响）
        assert W.load_table() is not None


def test_provider_map_covers_every_provider_dir():
    """映射必须跟着 `providers/` 目录长 —— 目录里多一家而映射没有，
    那家就永远查不到表，而且没有任何地方会红（除非有这条）。"""
    mod = _load_refetch()
    dirs = {p.name for p in (_PROJECT_ROOT / "providers").iterdir()
            if p.is_dir() and (p / "manifest.yaml").is_file()}
    mapped = set(mod.PROVIDER_MAP)
    assert dirs == mapped, (
        "映射与目录不符：\n  目录里多出来的（去加映射）：%r\n  映射里多出来的（目录删了？）：%r"
        % (sorted(dirs - mapped), sorted(mapped - dirs)))


def test_excerpt_negative_control():
    """自造靶子：`excerpt` 只收有真数的，缺 `limit.context` 的一律不收
    （读不到 ≠ 0，不能拿 0 当窗口）。"""
    mod = _load_refetch()
    raw = {
        "deepseek": {"models": {
            "好的": {"limit": {"context": 12345}},
            "缺 limit": {},
            "context 是 0": {"limit": {"context": 0}},
            "context 是字符串": {"limit": {"context": "65536"}},
        }},
        "不在映射里的 provider": {"models": {"x": {"limit": {"context": 1}}}},
    }
    got = mod.excerpt(raw)
    assert got == {"deepseek": {"好的": {"context": 12345}}}, got


def test_excerpt_collects_output_and_reasoning_but_never_fakes_them():
    """`output` / `reasoning` 有就收、没有就不出现。

    负侧是这条的重点：不许拿 0 或 `false` 顶替。
    `output` 缺，键不存在（下游 `lookup_output` 回 `None`，`max_tokens` 走兜底）；
    `reasoning` 缺，键不存在（下游回 `None` = "这家没告诉我"，维持现状）。
    若顶成 `false`，下游会读成"这家明确不支持思考"而拦下来 —— 那是两个决定
    （读不到 ≠ 0）。
    """
    mod = _load_refetch()
    raw = {
        "deepseek": {"models": {
            "全都有": {"limit": {"context": 1000, "output": 393216},
                       "reasoning_options": [{"type": "toggle"},
                                             {"type": "effort", "values": ["low", "high"]}]},
            "只有 context": {"limit": {"context": 1000}},
            "reasoning 是空表": {"limit": {"context": 1000}, "reasoning_options": []},
            "reasoning 缺 type": {"limit": {"context": 1000},
                                  "reasoning_options": [{"values": ["x"]}]},
            "output 是 0": {"limit": {"context": 1000, "output": 0}},
        }},
    }
    got = mod.excerpt(raw)["deepseek"]
    assert got["全都有"] == {"context": 1000, "output": 393216,
                             "reasoning": {"types": ["effort", "toggle"],
                                           "effort_values": ["low", "high"]}}, got["全都有"]
    assert got["只有 context"] == {"context": 1000}, got["只有 context"]
    assert got["reasoning 是空表"] == {"context": 1000}, got["reasoning 是空表"]
    assert got["reasoning 缺 type"] == {"context": 1000}, got["reasoning 缺 type"]
    assert got["output 是 0"] == {"context": 1000}, got["output 是 0"]


# ----------------------------------------------------------------------
# 2. 匹配规则
# ----------------------------------------------------------------------

def test_lookup_exact_then_case_then_normalized():
    rows = {"claude-sonnet-4-5": 1000000, "qwen3.7-plus": 999999,
            "deepseek-ai/DeepSeek-V3.2": 163840}
    old = W.load_table()
    W._table_cache = {"probe": rows}
    try:
        assert W.lookup("probe", "claude-sonnet-4-5") == 1000000      # 精确
        assert W.lookup("probe", "Qwen3.7-PLUS") == 999999            # 大小写
        assert W.lookup("probe", "DeepSeek-V3.2") == 163840           # 归一化（丢前缀）
        assert W.lookup("probe", "没这个模型") is None
        assert W.lookup("没这家", "claude-sonnet-4-5") is None
    finally:
        W._table_cache = old


def test_output_and_reasoning_lookups_share_the_one_matcher():
    """三个查询（context / output / reasoning）共用同一个匹配器。

    判据有两半，都要：
    · 三级规则一路继承 —— `output` / `reasoning` 也要能按大小写、按"丢聚合商前缀"
      匹配得上（否则同一个模型，窗口查得到而输出上限查不到，`max_tokens` 就掉兜底）；
    · 两种形状都认 —— 旧的裸整数表下，`lookup` 照常、而
      `lookup_output` / `lookup_reasoning` 回 `None`（不许崩、不许编一个 0）。
      这一半是"代码与表可以各自单独前进/回退"的那条判据（`_row_field` 的 docstring）。
    """
    new_rows = {
        "deepseek-v4-flash": {"context": 1000000, "output": 393216,
                              "reasoning": {"types": ["effort", "toggle"],
                                            "effort_values": ["low", "high", "max"]}},
        "vendor-x/qwen-max": {"context": 32768, "output": 8192},
    }
    old = W.load_table()
    W._table_cache = {"probe": new_rows}
    try:
        assert W.lookup("probe", "deepseek-v4-flash") == 1000000
        assert W.lookup_output("probe", "deepseek-v4-flash") == 393216
        assert W.lookup_reasoning("probe", "deepseek-v4-flash")["types"] == \
            ["effort", "toggle"]
        # 大小写 / 去前缀 —— 三个查询走的是同一条规则
        assert W.lookup_output("probe", "QWEN-MAX") == 8192
        assert W.lookup_output("probe", "qwen-max") == 8192
        # `reasoning` 没声明，`None`（不是空 dict、不是 False）
        assert W.lookup_reasoning("probe", "qwen-max") is None
        assert W.lookup_output("probe", "没这个模型") is None
    finally:
        W._table_cache = old

    # 裸整数表（schema 1）：窗口照常，新字段一律 `None`
    W._table_cache = {"probe": {"m": 4096}}
    try:
        assert W.lookup("probe", "m") == 4096
        assert W.lookup_output("probe", "m") is None, "旧表上编出了一个 output"
        assert W.lookup_reasoning("probe", "m") is None
    finally:
        W._table_cache = old


def test_lookup_does_not_guess_when_ambiguous():
    """归一化撞多个，不猜（落回下一层）。挑一个"看起来对"的正是要防的病。"""
    rows = {"vendor-a/gpt-x": 100, "vendor-b/gpt-x": 200000}
    old = W.load_table()
    W._table_cache = {"probe": rows}
    try:
        assert W.lookup("probe", "gpt-x") is None, "多候选却挑了一个"
        assert W.lookup("probe", "vendor-a/gpt-x") == 100   # 写全了就还能查到
    finally:
        W._table_cache = old


def test_broken_table_falls_through():
    """表坏了，空表 + 落回下一层（不是崩、也不是拿假值顶上）。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    bad = tmp / "broken.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    assert W.load_table(path=bad) == {}, "坏表没被当成空表"

    # 空表之下：查表那一层给 None，resolve 落到 manifest / 兜底
    old = W.load_table()
    W._table_cache = {}
    try:
        assert W.lookup("deepseek", "deepseek-v4-flash") is None
        assert W.resolve(provider_dir="deepseek", model="deepseek-v4-flash",
                         config_value=None, manifest_value=65536) == (
            65536, W.SRC_MANIFEST), "表空了却没落回 manifest"
        assert W.resolve(provider_dir="deepseek", model="deepseek-v4-flash",
                         config_value=None, manifest_value=None) == (
            W.FALLBACK_WINDOW, W.SRC_FALLBACK), "表空了却没落回兜底"
    finally:
        W._table_cache = old


# ----------------------------------------------------------------------
# 3. 优先级 + 实测方向
# ----------------------------------------------------------------------

def test_priority_order():
    old = W.load_table()
    W._table_cache = {"probe": {"m": 500000}}      # 表里只有 "m"
    W.forget_observed()
    try:
        M = lambda **kw: W.resolve(provider_dir="probe", model="m", **kw)  # noqa: E731
        U = lambda **kw: W.resolve(provider_dir="probe", model="没这个", **kw)  # noqa: E731
        # 表里没有那个模型，只剩 manifest
        assert U(config_value=None, manifest_value=100000) == (100000, W.SRC_MANIFEST)
        # manifest vs 表，表赢（表是外部维护的真值，manifest 是粗兜底）
        assert M(config_value=None, manifest_value=100000) == (500000, W.SRC_TABLE)
        # config 最高
        assert M(config_value=2000000, manifest_value=100000) == (2000000, W.SRC_CONFIG)
        # 实测盖过表
        W.observe("m", 900000)
        assert M(config_value=None, manifest_value=100000) == (900000, W.SRC_MEASURED)
        # 但盖不过 config 声明（用户显式声明第一）
        assert M(config_value=2000000, manifest_value=100000) == (2000000, W.SRC_CONFIG)
        # 什么都没有，兜底
        assert U(config_value=None, manifest_value=None) == (
            W.FALLBACK_WINDOW, W.SRC_FALLBACK)
    finally:
        W._table_cache = old
        W.forget_observed()


def test_measured_only_raises_never_lowers():
    old = W.load_table()
    W._table_cache = {"probe": {"m": 500000}}
    W.forget_observed()
    try:
        W.observe("m", 100)                      # 一条很小的观测
        assert W.resolve(provider_dir="probe", model="m", config_value=None,
                         manifest_value=None) == (500000, W.SRC_TABLE), \
            "小观测把窗口调小了（证据只能证明下界，不能证明'更小'）"
        W.observe("m", 50)                       # 更小的一条也不该覆盖
        assert W.observed_lower_bound("m") == 100
    finally:
        W._table_cache = old
        W.forget_observed()


def test_is_fallback_set():
    assert W.is_fallback(W.SRC_FALLBACK)
    assert W.is_fallback(W.SRC_MANIFEST), "`声明(manifest 兜底)` 名字里就写着是兜底"
    assert not W.is_fallback(W.SRC_CONFIG)
    assert not W.is_fallback(W.SRC_TABLE)
    assert not W.is_fallback(W.SRC_MEASURED)
    assert W.FALLBACK_WINDOW == 250_000, "设计 §8.3 #30 定的是 250,000"


# ----------------------------------------------------------------------
# 4. 接线（源级读者）
# ----------------------------------------------------------------------

def test_provider_loader_delegates_window_resolution():
    """装载链只递输入，判定在 `core/window_estimate.py`（一处定义）。

    顺带钉住那条行为变化：兜底值不再是 65,536 —— 装载链里不许再出现
    那个字面量（它会让"我没改过兜底"重新变成可能）。
    """
    src = (_PROJECT_ROOT / "bus" / "loaders" / "provider_loader.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "resolve"]
    assert calls, "provider_loader 没有调用 window_estimate.resolve"
    # 判据按 AST 字面量、不按 grep —— 注释里写着 65,536
    #    （它说明的是"那个匿名链式 get"），直接 grep 会把它判成违规。
    #    这正是那个坑（检查器自己打自己）。
    lits = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
            and isinstance(n.value, int) and n.value == 65536]
    assert not lits, \
        "装载链里又出现了 int 字面量 65536 —— 兜底值现在只许在 window_estimate 里定义"


def test_observe_happens_before_the_early_return():
    """顺序读者：`observe` 必须是函数体顶层的、在任何 `if` 之前的那条语句。

    判据不是"行号小于某个 return" —— 行号判据抓不到真正的坏形状：
    把 `observe` 挪进那个 `if` 里、`return` 的前一行，行号依然更小。
    真正的不变量是「它无条件执行」：下界在你每条成功请求上都要记，
    而不仅是在"这条已经超过声明窗口"时（那是最不需要它的情形 ——
    `_check_window_claim` 自己会吼）。
    """
    for rel in ("providers/openai_compat.py", "providers/anthropic/client.py"):
        tree = ast.parse((_PROJECT_ROOT / rel).read_text(encoding="utf-8"))
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_check_window_claim"),
                  None)
        assert fn is not None, rel + " 里找不到 _check_window_claim"
        top_observe = [i for i, st in enumerate(fn.body)
                       if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                       and isinstance(st.value.func, ast.Attribute)
                       and st.value.func.attr == "observe"]
        assert top_observe, (
            "%s：函数体顶层没有 `window_estimate.observe(...)` —— "
            "多半是被挪进了 `if` 里（那样它只在'已超过声明'时才记账，实测层基本是空的）"
            % rel)
        first_if = [i for i, st in enumerate(fn.body) if isinstance(st, ast.If)]
        assert first_if and min(top_observe) < min(first_if), (
            "%s：`observe` 不在最早的 `if` 之前 —— 它必须**无条件执行**" % rel)


# ----------------------------------------------------------------------
# 5. 45% 记忆档 + 兜底降级（`_tier_params` 是纯函数，直接打）
# ----------------------------------------------------------------------

def test_memory_tier_ceiling_is_45_percent():
    from core.memory.manager import _tier_params
    r, c = _tier_params("memory", 1_000_000, W.SRC_CONFIG)
    assert c == 450_000, "记忆档的上限不是窗口的 45%%，实得 %r" % (c,)
    assert r == 0.15
    # 反证："没有上限"那种形状（0.85×window）必须大于新上限，
    #   否则这条改动什么也没改（用例在失去靶子）
    old_style = int(1_000_000 * 0.85)
    assert old_style > c, "夹具坏了：45% 没有比旧的 0.85×window 更紧"


def test_fallback_source_downgrades_memory_tier():
    from core.memory.manager import BALANCED_TIER_CEILING, _tier_params
    for src in (W.SRC_FALLBACK, W.SRC_MANIFEST):
        r, c = _tier_params("memory", 1_000_000, src)
        assert (r, c) == (0.35, BALANCED_TIER_CEILING), \
            "来源是兜底（%s）时记忆档没降到均衡档" % src
    for src in (W.SRC_CONFIG, W.SRC_TABLE, W.SRC_MEASURED):
        assert _tier_params("memory", 1_000_000, src)[1] == 450_000, \
            "来源是可信的那几档（%s）时不该降级" % src


def test_other_tiers_unchanged():
    """均衡 / 更新档一个字节没动（这条改动只碰「记忆」档）。"""
    from core.memory.manager import BALANCED_TIER_CEILING, FRESH_TIER_CEILING, _tier_params
    assert _tier_params("balanced", 1_000_000, W.SRC_CONFIG) == (0.35, BALANCED_TIER_CEILING)
    assert _tier_params("fresh", 1_000_000, W.SRC_CONFIG) == (0.35, FRESH_TIER_CEILING)
    assert _tier_params("没听说过的档", 1_000_000, W.SRC_CONFIG) == (0.35, BALANCED_TIER_CEILING)


def test_load_session_uses_the_shared_tier_params():
    """一处定义：装载预算的判定只有 `_tier_params` 一处。

    这条的坏形状是：`if continuity == "fresh" …` 块没删干净，
    于是同一份判定在 `load_session` 里有了两份 ——
    一份是刚接的 `_tier_params`、一份是下面那个死块（它看起来活的）。
    所以判据要两个方向都查：用了它，且没有第二份分支。
    """
    src = (_PROJECT_ROOT / "core" / "memory" / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "load_session")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_tier_params"]
    assert calls, "load_session 没有用 _tier_params（装载预算判定被抄了第二份）"
    lits = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
            and isinstance(n.value, int) and n.value in (16384, 2048)]
    assert not lits, "load_session 里又出现了档位上限的字面量 %r（两个真相源）" % (lits,)
    # 第二份判定的形状是"又给 `reserve_ratio` / `ceiling_tokens` 赋了一次值"
    #   —— 整份文件里只许有 `_tier_params(...)` 那个解包赋值。
    #   （`continuity == "memory"` 另有别处的合法比较：它管的是"历史怎么注入"
    #   〔独立消息 vs 一整块〕，不是预算 —— 所以判据不能落在"有没有比过它"。）
    dup = []
    for n in ast.walk(tree):
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, ast.AnnAssign):
            targets = [n.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id in ("reserve_ratio", "ceiling_tokens"):
                dup.append(n.lineno)
    assert not dup, (
        "manager.py 里又给 reserve_ratio/ceiling_tokens 直接赋值了（行 %r）—— "
        "档位判定只许 `_tier_params` 一处，别处一律用它的返回值" % (dup,))


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
