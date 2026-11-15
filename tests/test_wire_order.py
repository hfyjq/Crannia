"""
wire 上的工具顺序 —— 发现一个工具只许延长前缀，不许改写它。

为什么立这个文件
================
缓存命中率是第一指标。而提示缓存按前缀匹配：
从第一个不同的字节起，后面全部重算。

`bus/registry.py::get_tool_schemas_filtered` 若按 `_registry["tool_schemas"]`
的注册序过滤，模型一旦 `search_tools` 发现了工具，它就被插到注册序里
它原本的位置上 —— 而那个位置可能在常驻工具中段：

    例：parse_pdf 挤开第 4 位 · copy_to_workspace 第 9 位 ·
        send_email / record_note 第 11 位

从那个位置起整段字节改变，缓存从那里断。

修法：出口一律排成 wire 序 —— 常驻在前（注册序）+ 其余追加在后（注册序）。
发现只延长前缀，不改写它（前缀只许增长）。

    反例  [core0 core1 parse_pdf core2 core3 ...]   <- 第 2 位起全变
    正例  [core0 core1 core2 core3 ... parse_pdf]   <- 前 N 位逐字不动

三条本文件要钉住的事
====================
1. 顺序不许依赖输入。`names` 是 `set` 拼出来的，而字符串哈希逐进程随机化
   （`PYTHONHASHSEED`）—— 拿它的迭代序定序，等于每次重启换一个前缀。
   定序只能来自注册序这个确定性来源。
2. 断言打在字节上，不打在"名字列表相等"上：缓存看到的是
   `json.dumps` 之后的字节（provider 也是这么序列化的，`providers/base.py:481`）。
3. 夹具不许空跑：每条都要能证明"按注册序过滤会长得不一样"，
   否则把代码改回按注册序过滤的写法它也照样绿（为错误的理由通过）。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_wire_order.py
"""

import contextlib
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only output -- the Windows console defaults to GBK and an emoji would
# crash the whole run (and the failure reporter with it).

_FAKE_SCHEMA = {"description": "probe", "inputSchema": {"type": "object"}}


def _names(schemas):
    return [t["function"]["name"] for t in schemas]


def _blob(schemas):
    """wire 上真正过线的字节（与 `BaseProvider` 量 token 时同一把序列化）。"""
    return json.dumps(schemas, ensure_ascii=False)


@contextlib.contextmanager
def _sandbox(core):
    """独立注册表 + 假的常驻集；退出时原样还回去（后面的用例会踩到）。"""
    import bus.registry as R
    R.reset()
    saved = R._CORE_TOOLS
    R._CORE_TOOLS = set(core)
    try:
        yield R
    finally:
        R._CORE_TOOLS = saved
        R.reset()


# ------------------------------------------------------------------
# 1. 基线：wire 序 = 常驻（注册序）+ 其余（注册序）
# ------------------------------------------------------------------

def test_core_first_then_the_rest():
    with _sandbox({"core_a", "core_c"}) as R:
        for n in ["other_x", "core_a", "other_y", "core_c"]:
            R.register_tool(n, lambda **k: None, _FAKE_SCHEMA)
        # 非常驻的必须先被发现才会进 wire（wire = 常驻 ∪ 已发现）
        R.note_discovered("s", ["other_x", "other_y"])
        got = _names(R.get_tool_schemas_active("s"))
        assert got == ["core_a", "core_c", "other_x", "other_y"], \
            f"wire 序应当是「常驻在前 + 其余追加在后」: {got}"
        print("  [ok] wire order = core (registration order) + the rest")


# ------------------------------------------------------------------
# 2. 核心不变量：发现只延长前缀（含"夹具不空跑"的证明）
# ------------------------------------------------------------------

def test_discovery_only_extends_the_prefix():
    """`other_a` 注册在最前 —— 按注册序过滤时它会挤开第 0 位。"""
    with _sandbox({"core_b", "core_d"}) as R:
        for n in ["other_a", "core_b", "other_c", "core_d"]:
            R.register_tool(n, lambda **k: None, _FAKE_SCHEMA)

        before = R.get_tool_schemas_active("s")
        R.note_discovered("s", ["other_a"])
        after = R.get_tool_schemas_active("s")

        # 夹具不空跑：按注册序过滤的写法确实给出另一个序。
        wanted = R.get_active_tool_names("s")
        naive = [n for n in _names(R.get_tool_schemas()) if n in wanted]
        assert naive != _names(after), \
            f"夹具空跑：按注册序过滤给出的是同一个序 {naive} —— 测不到东西"

        assert _names(after) == _names(before) + ["other_a"], \
            f"发现的工具没有追加到末尾: {_names(after)}"
        assert _blob(after[:len(before)]) == _blob(before), \
            "前缀的**字节**被改写了 —— 提示缓存会从那里断"
        print("  [ok] discovery extends the prefix; the preceding bytes are unchanged")


# ------------------------------------------------------------------
# 3. 定序来源：注册序，不是输入序（输入是 set）
# ------------------------------------------------------------------

def test_order_ignores_the_input_order():
    with _sandbox({"core_a"}) as R:
        for n in ["other_p", "core_a", "other_q"]:
            R.register_tool(n, lambda **k: None, _FAKE_SCHEMA)
        R.note_discovered("s", ["other_q", "other_p"])
        names = list(R.get_active_tool_names("s"))   # set 拼出来的，序不确定

        forward = _names(R.get_tool_schemas_filtered(names))
        backward = _names(R.get_tool_schemas_filtered(list(reversed(names))))
        as_set = _names(R.get_tool_schemas_filtered(set(names)))

        want = ["core_a", "other_p", "other_q"]
        assert forward == backward == as_set == want, \
            (f"输出序跟着输入序走了（输入是 set，逐进程随机化）: "
             f"{forward} / {backward} / {as_set}")
        print("  [ok] order depends on registration order, never on the input")


# ------------------------------------------------------------------
# 4. 真注册表：挑一个真落在常驻中段的工具，发现它，看前缀动没动
# ------------------------------------------------------------------

def test_real_registry_appends_a_real_mover():
    import bus.registry as R
    R.reset()
    from scripts.measure_request import boot_registry
    boot_registry()

    sess = "probe:wire-order"
    try:
        before = R.get_tool_schemas_active(sess)
        all_names = _names(R.get_tool_schemas())
        core_pos = [i for i, n in enumerate(all_names) if n in R._CORE_TOOLS]
        assert core_pos, "注册表里一个常驻工具都没有？"

        # 注册序里"非常驻、却排在某个常驻之前"的工具 —— 就是会挤开中段的那些
        # 会话条件工具（`_SESSION_TOOLS`）不参与 ——
        #   `note_discovered` 对拿不到它们的会话会拒收（没有接管 / 群聊 / ultra 档），
        #   而本测试要验的是"按注册序过滤会不会挤开中段"，与那条会话闸无关。
        movers = [n for i, n in enumerate(all_names)
                  if i < max(core_pos) and n not in R._CORE_TOOLS
                  and n not in R._SESSION_TOOLS]
        assert movers, ("夹具空跑：注册序里没有任何'非常驻却排在某个常驻之前'的工具 "
                        "—— 按注册序过滤也不会挤开中段，这条测不到东西")

        picked = movers[0]
        R.note_discovered(sess, [picked])
        after = R.get_tool_schemas_active(sess)

        assert _names(after) == _names(before) + [picked], \
            f"发现的 {picked} 没有追加到末尾: 尾巴={_names(after)[-3:]}"
        assert _blob(after[:len(before)]) == _blob(before), \
            f"前缀被改写了（{picked} 从中段挤了进去）"
        print(f"  [ok] real registry: {picked} (reg index "
              f"{all_names.index(picked)}) appends instead of splitting "
              f"the array ({len(movers)} such tools exist)")
    finally:
        R.forget_discovered(sess)


# ------------------------------------------------------------------

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
