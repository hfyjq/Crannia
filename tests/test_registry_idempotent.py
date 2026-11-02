"""
工具注册的幂等性 —— 「重启框架」不许让工具表长胖，更不许让 wire 出现重名。

背景
====
`/api/restart` 会在同一个进程里再跑一遍 `_init_framework()`，于是每个
工具都会被重新注册一次。`bus/registry.py::register_tool` 的 `tools` /
`tool_perms` 是 dict（天然幂等），schema 那一格是 list —— 如果同名注册
走 append，每重启一次就多一份 schema，`get_tool_schemas_filtered` 会把重复项
原样送上 wire，上游直接 400：

    Tool names must be unique.

后果是双份的：会话不可用，而且每条请求的 `tools=` 越来越贵。

钉住三件事
==========
  1. `register_tool` 同名就地替换，不是追加；
  2. 连续 boot（等价于连续点「重启框架」）之后，注册表与 wire 的尺寸都不变、
     也没有重名；
  3. wire 边界若真的发现重复，必须出声并去重（静默兜底等于没有兜底）。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_registry_idempotent.py
"""

import contextlib
import io
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).


def _names(schemas):
    return [t["function"]["name"] for t in schemas]


def _dups(names):
    from collections import Counter
    return {k: v for k, v in Counter(names).items() if v > 1}


def _fresh():
    """清空注册表 —— 每个用例都要一个确定的起点。"""
    import bus.registry as R
    R.reset()
    return R


# ------------------------------------------------------------------
# 1. 单元：同名注册两次 = 注册一次
# ------------------------------------------------------------------

def test_register_tool_is_idempotent():
    R = _fresh()
    schema = {"description": "first", "inputSchema": {"type": "object"}}
    R.register_tool("probe_tool", lambda **k: None, schema)
    assert len(R.get_tool_schemas()) == 1, \
        f"首注册就该是 1 条，实得 {len(R.get_tool_schemas())}"

    # 第二次用不同的描述 —— 要能验出"替换"而不是"忽略"
    schema2 = {"description": "second", "inputSchema": {"type": "object"}}
    R.register_tool("probe_tool", lambda **k: None, schema2)
    got = R.get_tool_schemas()
    assert len(got) == 1, \
        f"重复注册又塞了一条（这本该是幂等的）: {_names(got)}"
    assert "second" in got[0]["function"]["description"], \
        "同名注册应当**就地替换**成新的 schema，而不是保留旧的"
    print("  [ok] register_tool twice == register once (and it replaces)")


# ------------------------------------------------------------------
# 2-3. 集成：连续 boot（= 按「重启框架」）不许让 wire 长胖、不许重名
# ------------------------------------------------------------------

def _boot_n(n: int):
    """连续 boot n 次，返回每次的 (全量条数, wire 条数, wire 重名数)。"""
    from scripts.measure_request import boot_registry
    out = []
    for _ in range(n):
        boot_registry()
        R = sys.modules["bus.registry"]
        all_names = _names(R.get_tool_schemas())
        wire_names = _names(R.get_tool_schemas_active("probe:session"))
        out.append((len(all_names), len(wire_names), len(_dups(wire_names))))
    return out


def test_restart_does_not_grow_the_wire():
    """连续重启之后，注册表与 wire 的尺寸都必须保持不变。

    只看 wire 会假绿。如果 `register_tool` 失去幂等，注册表会重复，
    但 wire 边界那道去重兜底会把重复挡掉 —— "wire 尺寸没变"会以一个**错误的
    理由成立。所以这里同时断言注册表本身**：wire 那条是用户看到的症状，
    注册表那条才压住根因，两条都要。

    重复注册的典型增长形状是 14 → 27 → 40（wire）与 100 → 199 → 298（注册表）。
    """
    _fresh()
    stats = _boot_n(3)
    wire_sizes = [w for _, w, _ in stats]
    reg_sizes = [a for a, _, _ in stats]
    assert wire_sizes[0] == wire_sizes[1] == wire_sizes[2], \
        f"重启让工具表长胖了（修前是 14 -> 27 -> 40）: {stats}"
    assert reg_sizes[0] == reg_sizes[1] == reg_sizes[2], \
        f"重启让注册表长胖了（修前是 100 -> 199 -> 298）: {stats} —— " \
        f"注意：wire 那半可能被去重兜底遮住，这条才是根因"
    print(f"  [ok] restart keeps both the wire and the registry the same "
          f"size: {stats}")


def test_no_duplicate_names_anywhere_after_reboot():
    """上游 400 的直接原因：wire 上有重名。"""
    _fresh()
    _boot_n(3)
    from scripts.measure_request import boot_registry
    boot_registry()
    R = sys.modules["bus.registry"]
    all_dups = _dups(_names(R.get_tool_schemas()))
    wire_dups = _dups(_names(R.get_tool_schemas_active("probe:session")))
    assert not all_dups, f"注册表里有重名: {list(all_dups.items())[:5]}"
    assert not wire_dups, \
        f"wire 上有重名 -> 上游会 400 'Tool names must be unique': " \
        f"{list(wire_dups.items())[:5]}"
    print("  [ok] no duplicate tool names in the registry or on the wire")


# ------------------------------------------------------------------
# 4. 第二道：wire 边界去重必须出声，不许静默兜底
# ------------------------------------------------------------------

def test_wire_dedup_is_loud():
    """绕过 `register_tool` 直接往 list 里塞重复项 —— 模拟"别的地方又漏了"。

    兜底要吼一行：静默生效的兜底等于没有兜底。负对照：把
    `get_tool_schemas_filtered` 里那句 `print` 删掉，本条立刻红。
    """
    R = _fresh()
    R.register_tool("probe_tool", lambda **k: None,
                    {"description": "d", "inputSchema": {}})
    # 绕过去重逻辑，直接制造重复（模拟未来的某条注册路径）
    R._registry["tool_schemas"].append(dict(R._registry["tool_schemas"][0]))

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = R.get_tool_schemas_filtered(["probe_tool"])
    printed = buf.getvalue()

    assert len(got) == 1, f"wire 边界没去重: {_names(got)}"
    assert "WARNING" in printed and "Tool names must be unique" in printed, \
        f"去重是静默的 —— 那等于没有兜底。实得输出: {printed!r}"
    print("  [ok] wire dedup drops the duplicate AND says so out loud")


# ------------------------------------------------------------------
# `_EXPLICIT_PERMS` 的机械读者
# ------------------------------------------------------------------

def test_explicit_perms_point_at_real_tools():
    """`_EXPLICIT_PERMS` 里的每个名字都必须是真注册了的工具，档位合法。

    为什么需要它：这是一张"要人记得改的名单"，而没有任何读者时，
    改名或删工具都会在表里留下一个静默失效的死名 —— 那个工具的档位
    会悄悄掉回兜底值，且不会报错。

    判据两条：
      ① 名字必须在注册表里（改名，红）。
         负对照验过：塞一个 `no_such_tool_zzz` 进去，当场红。
      ② 值必须是 `read`/`write`/`admin` 之一。
         非法值在装载期就会炸 —— `register_tool` 里的
         `_PERM_LABEL[perm]` 会直接 `KeyError`。② 是纵深，不是主判据。
    """
    from scripts.measure_request import boot_registry
    boot_registry()
    import bus.registry as R

    registered = {td["function"]["name"] for td in R.get_tool_schemas()}
    assert registered, "setup: 装载失败，注册表是空的 -- 这条会恒真地通过"

    valid = {"read", "write", "admin"}
    dead = sorted(n for n in R._EXPLICIT_PERMS if n not in registered)
    assert not dead, (
        "`_EXPLICIT_PERMS` 里有注册表里不存在的名字：%r -- "
        "改过名？那这一条档位**静默失效**了（会掉回 admin 兜底）" % (dead,)
    )
    bad = {n: v for n, v in R._EXPLICIT_PERMS.items() if v not in valid}
    assert not bad, "_EXPLICIT_PERMS 里有非法档位：%r" % (bad,)
    print("  [ok] _EXPLICIT_PERMS %d 条都指向真工具、档位合法"
          % len(R._EXPLICIT_PERMS))


# ------------------------------------------------------------------

# ------------------------------------------------------------------
# 5. 常驻集里的每个名字都必须真实存在
# ------------------------------------------------------------------

def test_every_core_tool_actually_exists():
    """`_CORE_TOOLS` 是手写死的名字清单，而它是"新工具怎么能被模型看见"
    这条链上唯一的手动环节（其余全自动：注册表扫描 · `search_tools` 实时扫
    注册表 · `<tool_discovery>` 数字每轮算）—— 手写集合会腐烂。

    名字写得再整齐，也没有任何机制会告诉你它不存在：一个拼错的名字只会让
    模型拿到的 `tools=` 少一项，而少的那一项看起来完全像正常。
    要断言，不要人眼。

    这条不是唯一的读者：`scripts/measure_request.boot_registry()` 里有一道
    同样的自检（而且它会 raise），走那个夹具的测试都顺带被它护着。
    留这一条是为了给它一个名字 —— 出红时人得先知道是哪条不变量破了。
    """
    _fresh()
    from scripts.measure_request import boot_registry
    boot_registry()
    R = sys.modules["bus.registry"]

    registered = set(R.list_tools())
    missing = sorted(set(R._CORE_TOOLS) - registered)
    assert not missing, (
        "常驻集里有注册表里**不存在**的名字：%r —— 模型拿到的 `tools=` 会少它们，"
        "而少的那几个**看起来完全像正常**（这正是 `extract_pdf_pages` 那次的形状）"
        % missing)

    # 负对照：这个集合差真的在做减法。少了这三条，上面那句可以退化成
    #   "自己等于自己"（例如有人把判据写成 `_CORE_TOOLS <= _CORE_TOOLS`）。
    assert R._CORE_TOOLS, "常驻集是空的 —— 上面那条断言恒真"
    assert registered - set(R._CORE_TOOLS), (
        "注册表恰好等于常驻集 —— 上面那条断言可能退化成'自己等于自己'")
    probe = sorted((set(R._CORE_TOOLS) | {"no_such_tool_d9_probe"}) - registered)
    assert probe == ["no_such_tool_d9_probe"], (
        "编造一个名字，这个判据居然挑不出来 -> 它是空跑")
    print("  [ok] 常驻 %d 个名字全部真实存在（注册表共 %d 个）"
          % (len(R._CORE_TOOLS), len(registered)))


def main():
    tests = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            failed += 1
            print("  FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("  ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d tests: %d passed, %d failed" % (len(tests), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
