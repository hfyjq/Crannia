"""缓存策略表的机械读者。

钉五组判据：

  1. 四家的表值逐条对得上官方文档（策略形态、缓存形状、断点数、回溯窗口）。
  2. 回落链：模型级覆盖 > provider > _default > 内置兜底。
  3. config.yaml 的显式声明赢过表，且只认写清楚的值。
  4. 表读坏 / 字段越界时不抛、不把热路径弄崩，退回上一份或兜底。
  5. 表是数据不是代码：新增一家只加一行，不改解析函数。

ASCII-only 输出（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_cache_policies.py
"""

import io
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import cache_policies as CP  # noqa: E402
from core import vision_budget as VB  # noqa: E402

_PASS = 0
_FAIL = 0


def check(desc, ok, note=""):
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _reset():
    os.chdir(_PROJECT_ROOT)
    CP.reset_cache()


def test_four_providers_match_the_documented_shapes():
    _reset()
    a = CP.resolve(provider="anthropic", model="claude-sonnet-4-6")
    check("anthropic 走显式块、anthropic 形状、4 断点",
          (a.strategy, a.cache_format, a.max_breakpoints)
          == ("explicit_blocks", "anthropic", 4),
          "%s" % ((a.strategy, a.cache_format, a.max_breakpoints),))
    check("anthropic 回溯 20 块、TTL 300 秒、工具定义要稳",
          (a.lookback_blocks, a.ttl_seconds, a.tools_must_be_stable)
          == (20, 300, True))

    d = CP.resolve(provider="deepseek", model="deepseek-flash")
    check("deepseek 隐式、零标记、不设最小前缀数字",
          (d.strategy, d.cache_format, d.max_breakpoints, d.min_prefix_tokens)
          == ("implicit", "none", 0, 0),
          "%s" % ((d.strategy, d.cache_format, d.max_breakpoints, d.min_prefix_tokens),))

    q = CP.resolve(provider="qwen", model="qwen3.8-max")
    check("qwen 显式与隐式互斥、alibaba 形状",
          (q.cache_mode, q.cache_format, q.explicit_enabled)
          == ("mutually_exclusive", "alibaba", False),
          "%s" % ((q.cache_mode, q.cache_format, q.explicit_enabled),))
    check("qwen 先走隐式（开关关着）但保留 4 断点上限",
          q.max_breakpoints == 4 and q.lookback_blocks == 20)

    z = CP.resolve(provider="zhipu", model="glm-5.3")
    check("zhipu 隐式、最小前缀 512",
          z.strategy == "implicit" and z.min_prefix_tokens == 512,
          str(z.min_prefix_tokens))


def test_model_level_prefix_override():
    """同一 provider 下不同模型的最小前缀不同（Anthropic 四档）。"""
    _reset()
    small = CP.resolve(provider="anthropic", model="claude-haiku-5-5")
    large = CP.resolve(provider="anthropic", model="claude-haiku-4-5")
    unknown = CP.resolve(provider="anthropic", model="claude-something-new")
    check("新模型 512 覆盖 provider 级 1024",
          small.min_prefix_tokens == 1024 and small.min_prefix_for() == 512,
          "%d/%d" % (small.min_prefix_tokens, small.min_prefix_for()))
    check("旧模型 4096 也能命中", large.min_prefix_for() == 4096,
          str(large.min_prefix_for()))
    check("表里没有的模型退回 provider 级，不猜", unknown.min_prefix_for() == 1024,
          str(unknown.min_prefix_for()))
    check("长名优先：claude-sonnet-4-6 不被 claude-sonnet-4 抢走",
          CP.resolve(provider="anthropic",
                     model="claude-sonnet-4-6").min_prefix_for() == 1024)


def test_fallback_chain():
    _reset()
    default = CP.resolve(provider="not-a-provider", model="x")
    check("没登记的供应商落 _default：unknown + batch",
          default.strategy == "unknown" and default.eviction == "batch"
          and default.keep_images == 3 and default.batch_every == 25,
          "%s" % ((default.strategy, default.eviction,
                   default.keep_images, default.batch_every),))
    check("回落来源写得清", "cache_policies.json" in default.source, default.source)

    original = CP._read_table
    try:
        CP._read_table = lambda: {}
        p = CP.resolve(provider="anthropic", model="x")
        check("表整份读不到时用内置兜底，不抛",
              p.keep_images == 3 and p.batch_every == 25 and p.max_breakpoints == 0)
    finally:
        CP._read_table = original
        CP.reset_cache()


def test_config_declaration_wins():
    """config.yaml 显式声明赢过表 —— 与能力表那条判据同形。"""
    _reset()
    original = CP._model_config
    try:
        CP._model_config = lambda: {"cache_strategy": "server_clear",
                                    "cache_mode": "none",
                                    "cache_format": "alibaba",
                                    "eviction": "none",
                                    "min_prefix_tokens": 999,
                                    "max_breakpoints": 2,
                                    "explicit_enabled": True}
        p = CP.resolve(provider="anthropic", model="x")
        check("strategy 显式覆盖赢过表",
              p.strategy == "server_clear" and p.eviction == "none",
              "%s/%s" % (p.strategy, p.eviction))
        check("cache_format 显式覆盖赢过表", p.cache_format == "alibaba", p.cache_format)
        check("数字与布尔也覆盖",
              (p.min_prefix_tokens, p.max_breakpoints, p.explicit_enabled)
              == (999, 2, True))
        CP._model_config = lambda: {"cache_strategy": "not-a-strategy",
                                    "max_breakpoints": -1}
        p2 = CP.resolve(provider="anthropic", model="x")
        check("不认的枚举当没写（仍用表里的 explicit_blocks）",
              p2.strategy == "explicit_blocks", p2.strategy)
        check("越界数字落回合法范围或表值", p2.max_breakpoints == 4,
              str(p2.max_breakpoints))
    finally:
        CP._model_config = original
        CP.reset_cache()


def test_broken_and_out_of_range_values():
    _reset()
    original = CP._read_table
    try:
        CP._read_table = lambda: {"_default": {
            "max_breakpoints": 99, "ttl_seconds": -5, "batch_every": 0,
            "keep_images": "abc", "strategy": "nonsense",
            "cache_format": "unknown-format"}}
        p = CP.resolve(provider="x", model="y")
        check("越界的断点数落兜底 0（宁可不打点）",
              p.max_breakpoints == 0, str(p.max_breakpoints))
        check("负 TTL 落 0", p.ttl_seconds == 0, str(p.ttl_seconds))
        check("0 批次落兜底 25", p.batch_every == 25, str(p.batch_every))
        check("非数字 keep 落兜底 3", p.keep_images == 3, str(p.keep_images))
        check("未知枚举落兜底", p.strategy == "unknown" and p.cache_format == "none",
              "%s/%s" % (p.strategy, p.cache_format))
    finally:
        CP._read_table = original
        CP.reset_cache()


def test_table_is_data_not_code():
    _reset()
    raw = json.loads((_PROJECT_ROOT / "providers" / "cache_policies.json").read_text(
        encoding="utf-8"))
    check("表带 fetched / source / note",
          all(k in raw for k in ("fetched", "source", "note")))
    check("四家都在表里",
          all(k in raw for k in ("anthropic", "deepseek", "qwen", "zhipu")))
    check("解析函数不硬编码某家的分支",
          "anthropic" not in (_PROJECT_ROOT / "core" / "cache_policies.py").read_text(
              encoding="utf-8").split("def resolve")[1])


def test_every_provider_has_a_row():
    """新增一家 provider 时，两张表都得跟着长；不许再出现只有四家的形状。"""
    _reset()
    window = json.loads((_PROJECT_ROOT / "providers" / "window_table.json").read_text(
        encoding="utf-8"))["providers"]
    policies = json.loads((_PROJECT_ROOT / "providers" / "cache_policies.json").read_text(
        encoding="utf-8"))
    budgets = json.loads((_PROJECT_ROOT / "providers" / "vision_profiles.json").read_text(
        encoding="utf-8"))
    check("缓存表覆盖窗口表里的全部 provider",
          all(p in policies for p in window),
          str([p for p in window if p not in policies]))
    check("图预算表覆盖窗口表里的全部 provider",
          all(p in budgets for p in window),
          str([p for p in window if p not in budgets]))

    from core.model_configs import CARD_PROVIDER
    missing = [f"{card}->{prov}" for card, prov in CARD_PROVIDER.items()
               if prov not in policies or prov not in budgets]
    check("12 张端配置卡映射到的 provider 都有行（含「其他」）", not missing, str(missing))

    # 每张表里的行都必须能解析，不能有"表里有但解析器读不出"的死行。
    for p in window:
        q = CP.resolve(provider=p, model="probe")
        v = VB.resolve(provider=p, model="probe")
        if not (q.strategy and q.cache_format and v.max_edge > 0):
            check("%s 两表都能解析" % p, False, "%s/%s/%d" % (
                q.strategy, q.cache_format, v.max_edge))
    check("16 家逐行可解析，没有死行", True)


def main() -> int:
    print("=" * 62)
    print("Cache policies: table lookup / model override / config wins / fallbacks")
    print("=" * 62)
    try:
        for fn in (test_four_providers_match_the_documented_shapes,
                   test_model_level_prefix_override,
                   test_fallback_chain,
                   test_config_declaration_wins,
                   test_broken_and_out_of_range_values,
                   test_table_is_data_not_code,
                   test_every_provider_has_a_row):
            print("\n-- %s --" % fn.__name__)
            fn()
    finally:
        os.chdir(_PROJECT_ROOT)
        CP.reset_cache()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
