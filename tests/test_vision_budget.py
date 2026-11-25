"""图预算表与可见性判定的机械读者。

钉四组判据：

  1. 表按 provider 查，查不到落 `_default`，`_default` 也没了就落内置兜底
     —— 读表失败不许让接管截图链路炸。
  2. `config.yaml` 的 `model.supports_vision` 是用户显式声明，赢过能力表。
  3. 能力表里"只有 text"的模型判成看不见图，且这件事是 confirmed 的。
  4. 表里没有的模型 → 判成"能看图"但 `confirmed=False`（fail-open，且必须出声）。

第 4 条的方向与 `providers/base.py::accepts_images` 同一条：默认关掉的代价是
"绝大多数用户的接管静默残废"，而默认开的代价只是一次可诊断的上游 400。

ASCII-only 输出（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_vision_budget.py
"""

import io
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import vision_budget as VB  # noqa: E402
from tests._sandbox import sandbox  # noqa: E402

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


def _in_root():
    """表是按 cwd 找的（开发树），测试必须站在仓库根上。"""
    os.chdir(_PROJECT_ROOT)
    VB.reset_cache()


def test_budget_follows_the_table():
    _in_root()
    prov = VB.resolve(provider="anthropic", model="claude-fable-5")
    check("anthropic 走表里那一档（1568 / 1.15MP）",
          (prov.max_edge, prov.max_pixels) == (1568, 1_150_000),
          "%s" % ((prov.max_edge, prov.max_pixels),))
    other = VB.resolve(provider="deepseek", model="deepseek-chat")
    check("没有专门一档的供应商落 _default（2000）",
          other.max_edge == 2000, str(other.max_edge))
    unknown = VB.resolve(provider="not-a-provider", model="m")
    check("连 provider 都不认识时也落 _default，不抛也不给 0",
          unknown.max_edge == 2000 and unknown.max_pixels > 0,
          "%s" % ((unknown.max_edge, unknown.max_pixels),))


def test_scale_is_proportional_and_never_upscales():
    _in_root()
    prov = VB.resolve(provider="anthropic", model="claude-fable-5")
    k = prov.scale_for(3000, 2000)
    check("超预算要缩（k < 1）", k < 1.0, str(k))
    check("缩完两轴同比例（横纵比不变）",
          abs((3000 * k) / (2000 * k) - 1.5) < 0.01, str(k))
    check("不放大：小图 k == 1", prov.scale_for(800, 600) == 1.0)
    check("尺寸为 0 时不除零", prov.scale_for(0, 0) == 1.0)


def test_config_declaration_wins():
    """显式声明赢过能力表 —— 这是用户改得动的那一个开关。"""
    _in_root()
    original = VB._model_config
    try:
        VB._model_config = lambda: {"supports_vision": False,
                                    "provider": "anthropic",
                                    "model_name": "claude-fable-5"}
        prov = VB.resolve()
        check("supports_vision: false 时判成看不见图，且 confirmed",
              prov.can_see is False and prov.confirmed is True,
              "%s" % ((prov.can_see, prov.confirmed),))
        check("判定依据写的是 config.yaml",
              "config.yaml" in prov.source, prov.source)
        VB._model_config = lambda: {"supports_vision": True,
                                    "provider": "deepseek",
                                    "model_name": "deepseek-chat"}
        prov2 = VB.resolve()
        check("显式 true 也赢过能力表（deepseek-chat 本来只有 text）",
              prov2.can_see is True and prov2.confirmed is True,
              "%s" % ((prov2.can_see, prov2.confirmed),))
    finally:
        VB._model_config = original
        VB.reset_cache()


def test_capability_table_decides_when_nothing_is_declared():
    _in_root()
    vision = VB.resolve(provider="anthropic", model="claude-fable-5")
    check("能力表有 image 的模型判成能看图",
          vision.can_see is True and vision.confirmed is True,
          "%s" % ((vision.can_see, vision.confirmed),))
    blind = VB.resolve(provider="deepseek", model="deepseek-chat")
    check("能力表只有 text 的模型判成看不见图",
          blind.can_see is False and blind.confirmed is True,
          "%s" % ((blind.can_see, blind.confirmed),))


def test_unknown_model_fails_open_but_says_so():
    _in_root()
    prov = VB.resolve(provider="deepseek", model="a-model-nobody-has-heard-of")
    check("没有记录时往能看图那边倒（fail-open）",
          prov.can_see is True, str(prov.can_see))
    check("同时明确标记 confirmed=False（调用方必须出声）",
          prov.confirmed is False, str(prov.confirmed))
    check("依据这句说得清是'没有记录'，不是'不能看图'",
          "记录" in prov.source, prov.source)


def test_broken_table_falls_back_instead_of_raising():
    _in_root()
    original = VB._read_json
    try:
        VB._read_json = lambda name: {}          # 表整份读空
        prov = VB.resolve(provider="anthropic", model="claude-fable-5")
        check("表读空时给内置预算，不抛", prov.max_edge == 2000, str(prov.max_edge))
        check("表读空时可见性走'没有记录'那一支",
              prov.can_see is True and prov.confirmed is False,
              "%s" % ((prov.can_see, prov.confirmed),))
    finally:
        VB._read_json = original
        VB.reset_cache()


def test_out_of_range_values_are_not_trusted():
    """表是用户可以手改的文件 —— 写个 0 或 99999 不该让截图链路失控。"""
    _in_root()
    original = VB._read_json

    def fake(name):
        if name == "vision_profiles.json":
            return {"_default": {"max_edge": 0, "max_pixels": -5}}
        return {}
    try:
        VB._read_json = fake
        prov = VB.resolve(provider="x", model="y")
        check("越界的 max_edge 落回兜底 2000", prov.max_edge == 2000, str(prov.max_edge))
        check("负的 max_pixels 落回兜底", prov.max_pixels == 4_000_000,
              str(prov.max_pixels))
    finally:
        VB._read_json = original
        VB.reset_cache()


def test_scale_matches_prepare_bytes():
    """预算算出来的比例与图片链路真的缩到的一致 —— 两处各算一遍会漂。"""
    _in_root()
    from PIL import Image
    from core import image_input as II
    prov = VB.resolve(provider="anthropic", model="claude-fable-5")
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (200, 200, 200)).save(buf, format="PNG")
    # `prepare_bytes` 会把归一化后的图写进 `data/attachments/`（内容寻址），
    # 所以必须进沙箱。不进的话开发树因为那张图已经存在而"看着干净"
    #    （`_spill` 命中 `p.exists()` 就不写），公开副本头一回跑就往生产
    #    目录写了 —— 2026-11-25 在 gitC 全量里抓到。
    with sandbox():
        prepared = II.prepare_bytes(buf.getvalue(), max_edge=prov.max_edge,
                                    max_pixels=prov.max_pixels)
    k = prov.scale_for(3000, 2000)
    check("prepare_bytes 的宽度 = 原宽 * scale_for（误差 1px）",
          abs(prepared.width - round(3000 * k)) <= 1,
          "got %d want %d" % (prepared.width, round(3000 * k)))
    check("总像素没超预算", prepared.width * prepared.height <= prov.max_pixels,
          "%d > %d" % (prepared.width * prepared.height, prov.max_pixels))


def main() -> int:
    print("=" * 62)
    print("Vision budget: table lookup / fail-open / proportional scale")
    print("=" * 62)
    try:
        for fn in (test_budget_follows_the_table,
                   test_scale_is_proportional_and_never_upscales,
                   test_config_declaration_wins,
                   test_capability_table_decides_when_nothing_is_declared,
                   test_unknown_model_fails_open_but_says_so,
                   test_broken_table_falls_back_instead_of_raising,
                   test_out_of_range_values_are_not_trusted,
                   test_scale_matches_prepare_bytes):
            print("\n-- %s --" % fn.__name__)
            fn()
    finally:
        os.chdir(_PROJECT_ROOT)
        VB.reset_cache()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
