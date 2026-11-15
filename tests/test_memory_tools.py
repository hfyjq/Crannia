"""记忆工具三件套的接线读者。

钉住"注册 / 定档 / 分类 / 挂载次序"这四件接线（存储逻辑在
`tests/test_memory_store.py`）：

  · 三件常驻（`_CORE_TOOLS`）且 bin 真的注册了（防"写了没接"）；
  · 权限档：两读一写（名字落不到前缀，靠 `_EXPLICIT_PERMS` —— 漏一行就
    静默变 `admin`：不可并发 + 每次一行审计噪声）；
  · 隔离分类：`memory_read`/`memory_search` 在 `_TRUSTED_OUTPUT_TOOLS`
    （刻意的：记忆是背景事实，不给它加 untrusted）·
    回执是纯文本（`PLAIN_TEXT_TOOLS`）；
  · `memory_write` 关掉时是执行侧拒，schema 照发（不从 tools= 里摘）；
  · 挂载：两条路都有（`test_stream_brakes` 另有一条 AST 锁步）·
    技能索引在前、记忆索引在后（前缀只增不改）。

facts 根 / tombstone 账本钉临时目录；开关那条走临时 cwd 的 settings.json。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_tools.py
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import core.agent_loop as AL  # noqa: E402
import core.memory_store as MS  # noqa: E402


@contextlib.contextmanager
def _pinned():
    old_root, old_tob = MS.FACTS_ROOT, MS.TOMBSTONES_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        base = Path(td)
        MS.FACTS_ROOT = base / "facts"
        MS.TOMBSTONES_PATH = base / ".tombstones.json"
        try:
            yield base
        finally:
            MS.FACTS_ROOT, MS.TOMBSTONES_PATH = old_root, old_tob


def _boot():
    from measure_request import boot_registry

    import bus.registry as R

    boot_registry()
    return R


# ----------------------------------------------------------------------
# 1. 注册 / 常驻 / 权限档
# ----------------------------------------------------------------------

def test_registered_core_and_permissions():
    R = _boot()
    names = set(R.list_tools())
    for n in ("memory_read", "memory_search", "memory_write"):
        assert n in names, f"{n} 没注册（接线断了）"
        assert n in R._CORE_TOOLS, f"{n} 不在常驻集（模型不知道它存在就没法用）"
    assert R.get_tool_permission("memory_read") == "read"
    assert R.get_tool_permission("memory_search") == "read"
    assert R.get_tool_permission("memory_write") == "write"

    # schema 的 name 与注册名一致（wire 上唯一的身份）
    by_name = {td["function"]["name"]: td for td in R.get_tool_schemas()}
    for n in ("memory_read", "memory_search", "memory_write"):
        assert by_name[n]["function"]["name"] == n


def test_isolation_and_plain_text_classification():
    for n in ("memory_read", "memory_search"):
        assert n in AL._TRUSTED_OUTPUT_TOOLS, (
            f"{n} 不在豁免表 —— 分类必须是**手动做过决定**的（它名字不落读家族，"
            "反查读者不会替它报警）")
        assert AL._isolation_decision_for(n) is False, "记忆被隔离了 —— 违反 §8.3 #26"
    from core.file_tools import PLAIN_TEXT_TOOLS, as_text

    for n in ("memory_read", "memory_search"):
        assert n in PLAIN_TEXT_TOOLS
        assert as_text("纯文本", n) == "纯文本", "str 回执被套了 JSON 信封"
    assert "memory_write" not in PLAIN_TEXT_TOOLS, "写件回 dict，不许在纯文本名单里"


# ----------------------------------------------------------------------
# 2. 端口直调（真 handler，走注册表）
# ----------------------------------------------------------------------

def test_handlers_roundtrip_via_registry():
    R = _boot()
    with _pinned():
        w = R.get_tool_handler("memory_write")(
            name="住处", description="用户住在杭州", content="西湖区。")
        assert w["success"] is True, w

        r = R.get_tool_handler("memory_read")(name="住处")
        assert isinstance(r, str) and "杭州" in r and "西湖区" in r, r

        s = R.get_tool_handler("memory_search")(query="杭州")
        assert isinstance(s, str) and "住处" in s, s


def test_switch_off_refuses_at_execution_but_schema_stays():
    R = _boot()
    cwd = os.getcwd()
    with _pinned(), tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        os.chdir(td)
        try:
            (Path(td) / "data").mkdir()
            (Path(td) / "data" / "settings.json").write_text(
                json.dumps({"memory_write": False}), encoding="utf-8")
            w = R.get_tool_handler("memory_write")(name="n", description="d", content="c")
            assert w["success"] is False and "关闭" in w["error"], w
            # 执行侧拒：schema 照发（不从 tools= 里摘 —— 摘了断缓存前缀）
            wire = {td_["function"]["name"] for td_ in R.get_tool_schemas()}
            assert "memory_write" in wire, "关开关不许动 tools= 的组成"
        finally:
            os.chdir(cwd)


def test_tombstone_blocks_handler():
    R = _boot()
    with _pinned():
        MS.add_tombstone("删过的事")
        w = R.get_tool_handler("memory_write")(name="删过的事", description="d", content="c")
        assert w["success"] is False and "删" in w["error"], w


# ----------------------------------------------------------------------
# 3. 挂载（#25）：两条路都有 · 技能在前 · 记忆在后
# ----------------------------------------------------------------------

def test_agent_loop_mounts_memory_index_after_skill_index():
    src = (_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.name in ("agent_loop", "agent_loop_stream"):
            calls = {}
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    name = getattr(sub.func, "id", None) or getattr(sub.func, "attr", "")
                    if name in ("build_memory_index", "append_memory_index",
                                "append_skill_index"):
                        calls.setdefault(name, []).append(sub.lineno)
            found[node.name] = calls

    assert set(found) == {"agent_loop", "agent_loop_stream"}, sorted(found)
    for fn, calls in found.items():
        assert "build_memory_index" in calls, f"{fn} 没有构建记忆索引"
        assert len(calls.get("append_memory_index", [])) == 1, (
            f"{fn} 的挂载点不是恰好一处：{calls}")
        skill_at = calls["append_skill_index"][0]
        mem_at = calls["append_memory_index"][0]
        assert mem_at > skill_at, (
            f"{fn}: 记忆索引挂在了技能索引**前面** —— #25 要求技能在前"
            f"（新块追加在最后，前缀只增不改）")


def test_append_order_puts_skill_block_first():
    from core.skill_index import append_skill_index

    msgs = [{"role": "user", "content": "喂"}]
    payload = append_skill_index(msgs, "- skill 行")
    payload = MS.append_memory_index(payload, "- [甲](甲.md) — 描述")
    content = payload[-1]["content"]
    assert content.index("<skill_index>") < content.index("<memory_index>"), (
        "记忆块没有追加在技能块后面")
    assert msgs[-1]["content"] == "喂", "原列表被改了"


# ----------------------------------------------------------------------

def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
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
            print("ERROR  " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
