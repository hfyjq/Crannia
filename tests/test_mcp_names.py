"""
MCP 公开工具名是一个对外契约。

## 契约的两侧

| | |
|---|---|
| MCP 允许 | 工具名到 128 字符、含 `.` |
| 函数名契约允许 | `^[a-zA-Z0-9_-]{1,64}$`（OpenAI 兼容线与 Anthropic 都是它） |

一个名字里带 `.` 或很长的 MCP 工具会在 wire 上 400（整轮白跑）。

两个互相独立的塌缩源（这是本文件要钉的核心）：

| | 塌缩源 | 触发 | 后果 |
|---|---|---|---|
| ① | 字符 —— `.` / 空格 / 任何非 `[A-Za-z0-9_-]`，或超 64 | 第三方工具名 | 上游 400 |
| ② | 分隔符自己 —— `("a__b","c")` 与 `("a","b__c")` 拼出来逐字相同 | 名字里本来就有 `__` | 两个身份塌成一个公开名，静默指错工具 |

②与"有没有规范化"无关 —— 光加一个 `_normalize_name` 堵不住它，
这正是还要拼哈希尾巴的原因。

## 本文件钉的五条

① 干净的名字逐字不变 —— `mcp__github__create_issue` 这类在跑的链接
   一个字节都不许动（模型的历史、权限规则、用户写惯了的话术都依赖它）
② 换过字符，拼哈希尾巴
③ 超长，截到 64 并把哈希尾巴拼在末尾
④ ②那个塌缩源必须被堵住（两个不同身份，两个不同公开名）—— 承重的一条
⑤ 名字是 `(server, raw)` 的纯函数；而执行侧闭包里的是 raw 名字
   （判据：公开名绝不发给 server，也不反解回 raw 名）
   —— 规范化改过名之后这两者不是同一个串，改错就是所有 MCP 调用指错工具

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_mcp_names.py
"""

import ast
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcpx.client import (  # noqa: E402
    MCP_NAME_MAX, _normalize_name, _public_tool_name,
)

#: 函数名契约 —— 两家 provider 线一致（见模块 docstring）。
_WIRE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉。


# 1. 承重：任何 `(server, raw)` 出来的名字都得合法且可区分

def test_a_clean_name_is_untouched():
    """逐字不变 —— 真实世界里那批名字一个字节都不许动。

    这条红了 = 我们把在跑的工具全改名了（模型的历史、权限规则、
    用户写惯了的话术都会跟着失效）。
    """
    cases = [
        ("github", "create_issue"),
        ("web", "search"),
        ("_mcp_builtin_", "web_fetch"),
        ("a", "b"),
    ]
    for server, raw in cases:
        got = _public_tool_name(server, raw)
        assert got == "mcp__%s__%s" % (server, raw), (server, raw, got)


def test_every_produced_name_obeys_the_wire_contract():
    """承重：任何输入出来的名字都必须是 `^[A-Za-z0-9_-]{1,64}$`。

    若直接把非法字符/超长名字发到 wire 上，上游 400，
    而症状是"整轮什么都没发生"（看起来像模型不干活）。
    """
    ugly = [
        ("my.server", "do.thing"),
        ("github", "a" * 200),
        ("my server", "tool name with spaces"),
        ("中文服务器", "中文工具"),
        ("s", "x" * 128),
        ("a__b", "c"),
        ("a", "b__c"),
        ("", ""),
    ]
    for server, raw in ugly:
        got = _public_tool_name(server, raw)
        assert _WIRE_NAME_RE.match(got), (
            "(%r, %r) -> %r 不合函数名契约" % (server, raw, got))
        assert len(got) <= MCP_NAME_MAX, (server, raw, got)


def test_two_identities_never_collapse_into_one_public_name():
    """承重：不同的 `(server, raw)` 永不塌成同一个公开名。

    这也是唯一能证明"加了哈希不是装饰"的判据。

    用例是成对的：每一对的两个身份拼出来逐字相同（或只有一个字符的差别），
    所以"规范化 + 拼前缀"这条路必然把它们并成一个 —— 红的症状不是报错，
    是静默指错工具（针对内置工具的 deny 规则会匹配不到同名的、
    没加前缀的 MCP 替代品）。
    """
    pairs = [
        # ②分隔符歧义 —— 两个身份拼出来逐字相同（这条光靠规范化堵不住）
        (("a__b", "c"), ("a", "b__c")),
        (("a", "b__c__d"), ("a__b", "c__d")),
        # ①换字符 —— 两个身份规范化之后相同
        (("my.server", "do.thing"), ("my_server", "do_thing")),
        (("s", "a.b"), ("s", "a_b")),
        # ③截断 —— 只有末尾的差别，截到 64 就没了
        (("s", "x" * 100 + "AAA"), ("s", "x" * 100 + "BBB")),
    ]
    for (s1, r1), (s2, r2) in pairs:
        n1 = _public_tool_name(s1, r1)
        n2 = _public_tool_name(s2, r2)
        assert n1 != n2, (
            "(%r, %r) 与 (%r, %r) 塌成了同一个名字 %r" % (s1, r1, s2, r2, n1))


def test_the_same_identity_is_deterministic():
    """纯函数：同一个身份每次都给同一个名字。

    这条不是废话：名字一旦不稳定，每次重连都换一批工具名，
    会话历史里的 `tool_call` 与权限规则全部对不上（判据：重连只要
    `serverName` 不变，就要重建同一批名字）。
    """
    for _ in range(3):
        assert _public_tool_name("my.server", "a" * 90) == \
            _public_tool_name("my.server", "a" * 90)
    # 而且不同进程也一样（`hash()` 有随机化，`sha256` 没有）
    assert _public_tool_name("s", "a.b").endswith(
        _public_tool_name("s", "a.b")[-12:]), "哈希尾巴不见了"


# 2. 逐条：换字符 / 超长 / 分隔符

def test_invalid_characters_become_underscore_and_get_a_hash():
    """①换字符，换 `_`，并且拼哈希（判据：替换确实改过名字才拼）
    —— 只换不拼仍是多对一的，会塌（见"两个身份不许塌成一个公开名"那条）。"""
    got = _public_tool_name("my.server", "do.thing")
    assert got.startswith("mcp__my_server__do_thing_"), got
    assert len(got.rsplit("_", 1)[-1]) == 12, got


def test_a_long_name_is_truncated_to_the_contract():
    """①超长，截到 64，尾巴拼在末尾（前面留够位置，别把哈希也截掉）。"""
    got = _public_tool_name("github", "x" * 200)
    assert len(got) == MCP_NAME_MAX, len(got)
    assert re.match(r"^[A-Za-z0-9_-]{51}_[0-9a-f]{12}$", got), got


def test_the_delimiter_ambiguity_is_caught():
    """②`mcp__a__b__c` 拆出来是 4 段，没法反解回唯一个 `(server, raw)`。

    判据是可区分，不是"名字能被拆回三段"：公开名只承诺唯一，
    不承诺可反解。
    """
    got = _public_tool_name("a__b", "c")
    assert got != "mcp__a__b__c", "分隔符歧义没被堵住"
    assert len(got.rsplit("_", 1)[-1]) == 12, "没拼哈希尾巴：%r" % got
    assert _WIRE_NAME_RE.match(got), got
    # 判据是可区分，不是"拆回来正好三段" —— 哈希尾巴挂上去之后
    # 公开名里仍然有 `__`（那是故意的：名字是纯函数，别反解）。
    assert _public_tool_name("a", "b__c") != got, "两个身份还是同一个名字"


def test_normalize_only_touches_characters():
    """`_normalize_name` 只管字符 —— 长度那条在 `_public_tool_name`。

    分开是刻意的：纯字符替换只做这一层，
    "换字符够不够"这个问题由调用方回答 —— 这里回答的是"不够"。
    """
    assert _normalize_name("a.b c") == "a_b_c"
    assert _normalize_name("a" * 200) == "a" * 200, "长度不该在这一层处理"
    assert _normalize_name("ok-name_1") == "ok-name_1"


# 3. 接线：执行侧闭包的是 raw 名字（不是公开名）

def _register_external_server_source() -> str:
    import mcpx.client as C
    src = Path(C.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "register_external_server":
            return ast.get_source_segment(src, node) or ""
    raise AssertionError("register_external_server 不见了")


def test_the_public_name_is_built_by_the_pure_function():
    """接线：注册那一步必须走 `_public_tool_name`，不许退回 f-string。

    按 AST 取那一段源码，不 grep —— 文件注释里会逐字出现
    那行 f-string，grep 会把注释判成用法。
    """
    seg = _register_external_server_source()
    assert "_public_tool_name(server.name, tool_name)" in seg, seg[:400]
    assert 'f"mcp__{server.name}__{tool_name}"' not in seg, (
        "注册那一步又退回裸 f-string 了")


def test_the_executor_closes_over_the_raw_name():
    """承重：`_call_tool` 的默认参数必须是 raw `tool_name`。

    规范化改过名之后，公开名与 raw 名不是同一个串（有哈希尾巴）——
    把公开名发给 server，所有 MCP 调用都指错工具，且多半只是"工具不存在"，
    看起来像 server 坏了。判据：公开名绝不发给 server，也绝不解析回 raw 名。
    """
    seg = _register_external_server_source()
    assert "async def _call_tool(name: str = tool_name," in seg, (
        "执行侧闭包的不是 raw 名字：\n%s" % seg[:600])
    assert "_public_tool_name(server.name, tool_name)" not in seg.split(
        "async def _call_tool")[1][:200], "执行侧用上了公开名"


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
