"""Unit tests for model-driven tool discovery.

它取代"按用户消息关键词过滤工具"的做法。那种做法的泄漏场景：请求是
    「帮我读一下 workspace 里的《…》，输出一份摘要到 txt」
触发词一个都不含 → 写工具全被剔除 → 模型想写文件却没有写工具，
空转一整轮。

现在：常驻工具 ∪ 模型用 search_tools 发现的工具。

Usage:
    .venv/Scripts/python.exe tests/test_tool_discovery.py

NOTE: prints are ASCII elsewhere; jieba 的启动日志会打到 stderr，属正常。
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import bus.registry as R  # noqa: E402

SESS = "test:private:u1"

#: 造一个小目录 —— 不跑 init_bus（那会拉起全部 skill/provider）
_CATALOG = [
    ("create_document",   "创建新 Word 文档或写入纯文本文件"),
    ("add_block",         "向 Word 文档追加内容块"),
    ("delete_worksheet",  "删除 Excel 工作表"),
    ("search_song",       "搜索歌曲"),
    ("get_weather",       "查询天气"),
    ("create_excel_chart", "生成图表"),
]


def _setup():
    """换上测试目录。返回原始状态以便还原。"""
    saved = (
        dict(R._registry["tools"]),
        list(R._registry["tool_schemas"]),
        dict(R._registry["tool_perms"]),
        dict(R._discovered),
        # `tool_perm_sources` 与 `tool_perms` 同生同死 ——
        # 只存一个的话，还原之后"这个档位是怎么来的"就对不上了。
        dict(R._registry["tool_perm_sources"]),
        # 归属那一格 —— 不清它，残留的归属会让本次注册被判成
        # "异源抢名"（`register_tool` 现在会比 owner）。
        dict(R._registry["tool_owners"]),
        # 第二根轴的两格，同为"注册那一刻算出来"的
        # 派生值 —— 不存不还，这里造的那批工具会留在第二根轴的表里。
        dict(R._registry["tool_destructive"]),
        dict(R._registry["tool_destructive_sources"]),
    )
    R._registry["tools"].clear()
    R._registry["tool_schemas"].clear()
    R._registry["tool_perms"].clear()
    R._registry["tool_perm_sources"].clear()
    R._registry["tool_owners"].clear()
    R._registry["tool_destructive"].clear()
    R._registry["tool_destructive_sources"].clear()
    R._discovered.clear()
    for name, desc in _CATALOG:
        R.register_tool(name, lambda **kw: {"ok": True},
                        {"description": desc,
                         "inputSchema": {"type": "object", "properties": {}}})
    return saved


def _restore(saved):
    R._registry["tools"].clear();   R._registry["tools"].update(saved[0])
    R._registry["tool_schemas"].clear(); R._registry["tool_schemas"].extend(saved[1])
    R._registry["tool_perms"].clear();   R._registry["tool_perms"].update(saved[2])
    R._discovered.clear();          R._discovered.update(saved[3])
    R._registry["tool_perm_sources"].clear()
    R._registry["tool_perm_sources"].update(saved[4])
    R._registry["tool_owners"].clear()
    R._registry["tool_owners"].update(saved[5])
    R._registry["tool_destructive"].clear()
    R._registry["tool_destructive"].update(saved[6])
    R._registry["tool_destructive_sources"].clear()
    R._registry["tool_destructive_sources"].update(saved[7])


def test_search_by_name():
    """英文/标识符查询命中工具名。"""
    s = _setup()
    try:
        names = [t["name"] for t in R.search_tools("delete worksheet")["tools"]]
        assert names and names[0] == "delete_worksheet", names
    finally:
        _restore(s)
    print("  [ok] 'delete worksheet' -> delete_worksheet")


def test_search_by_description_chinese():
    """中文查询靠描述命中 —— 且不依赖用户消息里的触发词。"""
    s = _setup()
    try:
        for q in ("把内容写进一个文本文件", "写文件"):
            names = [t["name"] for t in R.search_tools(q)["tools"]]
            assert "create_document" in names, (q, names)
    finally:
        _restore(s)
    print("  [ok] Chinese description match -> create_document")


def test_single_char_fallback():
    """jieba 会切出'来首'/'首歌'这类组合，描述里却只有'歌'。

    单字兜底必须能召回，否则口语化的查询一个都不中。
    """
    s = _setup()
    try:
        names = [t["name"] for t in R.search_tools("来首歌")["tools"]]
        assert "search_song" in names, names
        names = [t["name"] for t in R.search_tools("今天天气怎么样")["tools"]]
        assert "get_weather" in names, names
    finally:
        _restore(s)
    print("  [ok] single-char fallback -> search_song / get_weather")


def test_discovery_flows_into_active_set():
    """search_tools 的命中经 note_discovered 进入可用集，schema 随之出现。"""
    s = _setup()
    try:
        before = R.get_active_tool_names(SESS)
        assert "delete_worksheet" not in before

        found = [t["name"] for t in R.search_tools("删除工作表")["tools"]]
        added = R.note_discovered(SESS, found)
        assert added >= 1, found

        after = R.get_active_tool_names(SESS)
        assert "delete_worksheet" in after

        schema_names = {t["function"]["name"] for t in R.get_tool_schemas_active(SESS)}
        assert "delete_worksheet" in schema_names

        # 再发现一次不应重复计数
        assert R.note_discovered(SESS, found) == 0
    finally:
        _restore(s)
    print("  [ok] discovered tools enter the active set (and are idempotent)")


def test_unknown_names_are_ignored():
    """模型可能编造工具名 —— 不能污染已发现集合。"""
    s = _setup()
    try:
        added = R.note_discovered(SESS, ["totally_made_up_tool"])
        assert added == 0
        assert "totally_made_up_tool" not in R.get_active_tool_names(SESS)
    finally:
        _restore(s)
    print("  [ok] fabricated tool names ignored")


def test_remaining_counts_only_unloaded_registered():
    """remaining = 注册表里还没加载的工具数（不是 _CORE_TOOLS 的条目数）。"""
    s = _setup()
    try:
        r = R.search_tools("delete worksheet")
        active = R.get_active_tool_names(SESS)
        expect = sum(1 for n, _ in _CATALOG if n not in active)
        assert r["remaining"] == expect, (r["remaining"], expect)
    finally:
        _restore(s)
    print("  [ok] remaining counts registered-but-inactive only")


def test_cjk_query_hints_at_english_only_external_tools():
    """外部 MCP 工具按英文描述登记，中文查询要给一条「换英文再搜」的路线。

    没有这条路线时，症状是「服务器接得好好的、工具也注册了，模型就是找不到」——
    而空结果看起来像一个结论。
    """
    s = _setup()
    try:
        for name, desc in (("mcp__demo__run", "Run a demo target"),
                           ("mcp__demo__analyze", "Analyze a demo binary")):
            R.register_tool(name, lambda **kw: {"ok": True},
                            {"description": desc,
                             "inputSchema": {"type": "object", "properties": {}}})
        zh = R.search_tools("逆向一下这个程序")
        assert "hint" in zh, zh
        assert "demo" in zh["hint"], zh["hint"]
        assert "2" in zh["hint"], zh["hint"]
        # 英文查询不给提示：它本来就能匹配到外部工具
        en = R.search_tools("analyze binary")
        assert "hint" not in en, en
        assert any(t["name"].startswith("mcp__") for t in en["tools"]), en
    finally:
        _restore(s)
    print("  [ok] CJK query points at English-only external tools")


def test_no_hint_when_no_external_server():
    """没有外部服务器时，中文查询不该多出这句话 —— 那是纯税。"""
    s = _setup()
    try:
        r = R.search_tools("逆向一下这个程序")
        assert "hint" not in r, r
    finally:
        _restore(s)
    print("  [ok] no hint without external servers")


def test_forget_discovered():
    """会话结束时清理，防无界增长。"""
    s = _setup()
    try:
        R.note_discovered(SESS, ["delete_worksheet", "search_song"])
        assert len(R.get_discovered(SESS)) == 2
        R.forget_discovered(SESS)
        assert R.get_discovered(SESS) == set()
        assert "delete_worksheet" not in R.get_active_tool_names(SESS)
    finally:
        _restore(s)
    print("  [ok] forget_discovered clears the session")


def test_search_tools_is_core():
    """search_tools 本身必须常驻 —— 否则模型无从发现任何东西。"""
    assert "search_tools" in R._CORE_TOOLS
    print("  [ok] search_tools is always loaded")


def test_prompt_does_not_mirror_catalog():
    """system prompt 里不再镜像工具目录（那是两份真相源）。"""
    from core.prompt_assembler import assemble_system_prompt
    sp = assemble_system_prompt(memory_context="")
    assert "<available_tools>" not in sp, "工具目录又被镜像回 prompt 了"
    assert "\n<tool_discovery>" in sp
    print("  [ok] prompt has <tool_discovery>, not <available_tools>")


def test_old_keyword_filter_is_gone():
    """关键词过滤 API 必须彻底移除 —— 留着就是第二套过滤路径。"""
    for dead in ("get_context_tool_names", "get_tool_schemas_for_message",
                 "_CONTEXT_TRIGGERS", "_WORD_TOOLS", "_EXCEL_TOOLS"):
        assert not hasattr(R, dead), f"{dead} 仍在，存在两套过滤路径"
    print("  [ok] keyword-filter API fully removed")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("  Tool Discovery Tests")
    print("=" * 60)
    print()

    tests = [
        ("search by name", test_search_by_name),
        ("search by description", test_search_by_description_chinese),
        ("single-char fallback", test_single_char_fallback),
        ("discovery -> active set", test_discovery_flows_into_active_set),
        ("fabricated names ignored", test_unknown_names_are_ignored),
        ("remaining count", test_remaining_counts_only_unloaded_registered),
        ("CJK hint for external tools", test_cjk_query_hints_at_english_only_external_tools),
        ("no hint without external server", test_no_hint_when_no_external_server),
        ("forget_discovered", test_forget_discovered),
        ("search_tools is core", test_search_tools_is_core),
        ("prompt not mirrored", test_prompt_does_not_mirror_catalog),
        ("keyword filter removed", test_old_keyword_filter_is_gone),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()

    print()
    print("=" * 60)
    print(f"  Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
