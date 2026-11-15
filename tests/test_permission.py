"""权限档的声明层与判定次序的机械读者。

要防的是一条不能被当作承重结构的启发式：`bus/registry.py::_classify_permission`
按工具名前缀猜三档（`get_` 是 read、认不出来一律 `admin`），
而并发预取与只读子 agent 拿它当真闸。
若没有声明层，132 个工具里 103 个「没人给它定过档」（前缀猜 68 + 兜底 35）。

这个文件钉的是声明层引进的东西：

  1. 次序：`manifest 声明 -> _EXPLICIT_PERMS -> 前缀猜 -> 兜底 admin`。
     任何一次重排都是一次真闸改动，而它不会有任何东西红 —— 除非这里钉着。
  2. 两格同生同死：`tool_perms`（记值）与 `tool_perm_sources`（记来源）
     —— 只清一个，「这个档位是怎么来的」就永远说不清了。
  3. 两条注册路都登记（eager / lazy）—— 只改一条的话，
     `on_demand` 的 skill（本仓库 7 个包全是）一个都拿不到声明。
  4. 同名异源，拒 —— 而同名同源必须放行
     （`/api/restart` / `reload_skills` 那条路，拒了会回来一条上游 400）。

`_declared` 是进程级全局（它自己登记在 `core/state_registry.py`），
所以本文件的规矩是存了再还原（`snapshot` / `restore`），绝不 `clear()`
—— 清空之后同一批里后跑的测试就看不到任何声明了（派生值同理）。
"""

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus.loaders.skill_loader import load_skill, load_skill_lazy   # noqa: E402
from core import tool_permission as TP                              # noqa: E402

READ_P = ("get_", "search_")
WRITE_P = ("set_", "add_")
EXPLICIT = {"history_search": "read", "run_command": "admin"}


# 判定次序（纯函数 —— 喂合成的表，不碰注册表）

def test_the_four_sources_are_all_reachable():
    """四层各能走到，且各自报对了来源。"""
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare("get_declared", "write")
        got = [
            TP.resolve("get_declared", explicit=EXPLICIT,
                       read_prefixes=READ_P, write_prefixes=WRITE_P),
            TP.resolve("history_search", explicit=EXPLICIT,
                       read_prefixes=READ_P, write_prefixes=WRITE_P),
            TP.resolve("get_by_prefix", explicit=EXPLICIT,
                       read_prefixes=READ_P, write_prefixes=WRITE_P),
            TP.resolve("mystery_tool", explicit=EXPLICIT,
                       read_prefixes=READ_P, write_prefixes=WRITE_P),
        ]
        assert got == [
            ("write", TP.SOURCE_DECLARED),
            ("read", TP.SOURCE_EXPLICIT),
            ("read", TP.SOURCE_PREFIX),
            ("admin", TP.SOURCE_DEFAULT),
        ], "四层判定/来源不对: %r" % (got,)
    finally:
        TP.restore(snap)


def test_a_declaration_beats_the_prefix_guess():
    """自造靶子：名字落 `get_`（前缀会猜 `read`），而声明是 `write`。

    这一条就是次序的负对照：把 `resolve` 里的声明那一支挪到前缀之后，
    这里立刻红（而全仓其它地方一处都不会红——猜出来的 read 完全合法）。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare("get_probe_writer", "write")
        perm, source = TP.resolve("get_probe_writer", explicit={},
                                  read_prefixes=READ_P, write_prefixes=WRITE_P)
        assert (perm, source) == ("write", TP.SOURCE_DECLARED), (
            "声明没有压过前缀猜 -> %r（次序被重排了？）" % ((perm, source),))
    finally:
        TP.restore(snap)


def test_a_declaration_beats_the_explicit_table():
    """声明也压过代码里的表 —— 表是兜底，不是更高权威。"""
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare("run_command", "read")     # 荒谬的值，但次序上它该赢
        perm, source = TP.resolve("run_command", explicit=EXPLICIT,
                                  read_prefixes=READ_P, write_prefixes=WRITE_P)
        assert (perm, source) == ("read", TP.SOURCE_DECLARED), (perm, source)
    finally:
        TP.restore(snap)


def test_an_invalid_declaration_is_not_recorded():
    """拼错 / 大小写不对，不登记，落回下一层（第一道保险）。

    这条的代价不是"多问一次"：`bus/registry.py` 拿 perm 去查 `_PERM_LABEL[perm]`，
    查不到就是一个 `KeyError`，而它会让整个 skill 的全部工具凭空消失。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        for bad in ("READ", "readonly", "", "admin ", None, "write_", 3):
            assert TP.declare("t_%s" % (bad,), bad) is False, "非法值被收下了: %r" % (bad,)
            assert TP.declared_perm("t_%s" % (bad,)) is None, bad
        assert TP.declare("t_ok", "read") is True
        assert TP.declared_perm("t_ok") == "read"
    finally:
        TP.restore(snap)


def test_resolve_only_ever_returns_a_valid_permission():
    """`resolve` 的返回值只可能在 `VALID_PERMS` 里（第二道保险）。"""
    snap = TP.snapshot()
    try:
        TP.restore({})
        for bad in ("READ", "", None):
            TP.declare("get_x", bad)
            perm, _src = TP.resolve("get_x", explicit={},
                                    read_prefixes=READ_P, write_prefixes=WRITE_P)
            assert perm in TP.VALID_PERMS, (bad, perm)
    finally:
        TP.restore(snap)


def test_undecided_is_exactly_prefix_plus_default():
    """棘轮读数只认这两种：`explicit` 是"一条条捞出来的"，不算没人管。"""
    assert TP.is_undecided(TP.SOURCE_PREFIX) is True
    assert TP.is_undecided(TP.SOURCE_DEFAULT) is True
    assert TP.is_undecided(TP.SOURCE_DECLARED) is False
    assert TP.is_undecided(TP.SOURCE_EXPLICIT) is False


# 注册表那一侧：两格同生同死

def _unregister(*names: str) -> None:
    """把探测工具从注册表里摘掉（测试自己造的，测试自己收拾）。"""
    import bus.registry as R
    for n in names:
        R._registry["tools"].pop(n, None)
        R._registry["tool_perms"].pop(n, None)
        R._registry["tool_perm_sources"].pop(n, None)
        # 第二根轴的两格同理（少摘一格就是一次静默泄漏）。
        R._registry["tool_destructive"].pop(n, None)
        R._registry["tool_destructive_sources"].pop(n, None)
        R._registry["tool_owners"].pop(n, None)
        R._registry["tool_schemas"][:] = [
            td for td in R._registry["tool_schemas"]
            if td.get("function", {}).get("name") != n
        ]


def test_registering_records_both_the_tier_and_its_source():
    """`register_tool` 必须两格一起写，而且认声明。"""
    import bus.registry as R
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare("get_probe_perm", "write")
        R.register_tool("get_probe_perm", lambda **k: 1,
                        {"description": "p",
                         "inputSchema": {"type": "object", "properties": {}}})
        assert R.get_tool_permission("get_probe_perm") == "write", "档位没认声明"
        assert R._registry["tool_perm_sources"]["get_probe_perm"] == TP.SOURCE_DECLARED
    finally:
        _unregister("get_probe_perm")
        TP.restore(snap)


def test_every_registered_tool_has_a_source():
    """两格同生同死 —— 有档位没来源（或反过来）就是一份说不清的账。"""
    import bus.registry as R
    perms = set(R._registry["tool_perms"])
    sources = set(R._registry["tool_perm_sources"])
    assert perms == sources, (
        "两格对不上 —— 只在档位里的: %r · 只在来源里的: %r"
        % (sorted(perms - sources)[:5], sorted(sources - perms)[:5]))
    assert all(s in TP.VALID_SOURCES for s in R._registry["tool_perm_sources"].values())


def test_reset_clears_both_tables():
    """`reset()` 必须清空 两张权限表 —— 否则清完之后
    `get_tool_permission(name)` 还会回上一批的档位，而 `tools` 是空的
    ：一个"看着像结论"的旧值。

    第二根轴的两格同理 —— 它们同样是
    "注册那一刻算出来的派生值"，留一批下来就是同一个坑。
    """
    import bus.registry as R
    saved = (dict(R._registry["tools"]), list(R._registry["tool_schemas"]),
             dict(R._registry["tool_perms"]), dict(R._registry["tool_perm_sources"]),
             dict(R._registry["tool_owners"]),
             dict(R._registry["tool_destructive"]),
             dict(R._registry["tool_destructive_sources"]))
    try:
        R.register_tool("probe_reset_me", lambda **k: 1,
                        {"description": "p",
                         "inputSchema": {"type": "object", "properties": {}}})
        R.reset()
        assert R._registry["tool_perms"] == {}, "reset() 没清 tool_perms"
        assert R._registry["tool_perm_sources"] == {}, "reset() 没清 tool_perm_sources"
        assert R._registry["tool_owners"] == {}, "reset() 没清 tool_owners"
        assert R._registry["tool_destructive"] == {}, "reset() 没清 tool_destructive"
        assert R._registry["tool_destructive_sources"] == {}, (
            "reset() 没清 tool_destructive_sources")
        assert R.get_tool_permission("probe_reset_me") == "admin", (
            "reset() 之后还回得到上一批的档位")
        assert R.get_tool_destructive("probe_reset_me") is False, (
            "reset() 之后还回得到上一批的 destructive")
    finally:
        R._registry["tools"].update(saved[0])
        R._registry["tool_schemas"][:] = saved[1]
        R._registry["tool_perms"].update(saved[2])
        R._registry["tool_perm_sources"].update(saved[3])
        R._registry["tool_owners"].update(saved[4])
        R._registry["tool_destructive"].update(saved[5])
        R._registry["tool_destructive_sources"].update(saved[6])


# 同名异源，拒

_SCHEMA = {"description": "p", "inputSchema": {"type": "object", "properties": {}}}


def _snapshot_registry():
    import bus.registry as R
    return (dict(R._registry["tools"]), list(R._registry["tool_schemas"]),
            dict(R._registry["tool_perms"]), dict(R._registry["tool_perm_sources"]),
            dict(R._registry["tool_owners"]),
            # 两根轴的派生格都存 —— 少存一格就是一次静默泄漏。
            dict(R._registry["tool_destructive"]),
            dict(R._registry["tool_destructive_sources"]))


def _restore_registry(snap):
    import bus.registry as R
    R._registry["tools"].clear();            R._registry["tools"].update(snap[0])
    R._registry["tool_schemas"][:] = snap[1]
    R._registry["tool_perms"].clear();       R._registry["tool_perms"].update(snap[2])
    R._registry["tool_perm_sources"].clear(); R._registry["tool_perm_sources"].update(snap[3])
    R._registry["tool_owners"].clear();      R._registry["tool_owners"].update(snap[4])
    R._registry["tool_destructive"].clear(); R._registry["tool_destructive"].update(snap[5])
    R._registry["tool_destructive_sources"].clear()
    R._registry["tool_destructive_sources"].update(snap[6])


def test_a_duplicate_name_is_refused():
    """自造靶子：两个来源抢一个名字，第二个必须被拒，
    而且先注册的那份一个字节没动。

    这里同时钉住"拒得干净"：`tools` / `tool_schemas` / 两张权限表 / 归属表
    一律不许被写。只拒一半（比如归属被改了）比不拒更难查。
    """
    import bus.registry as R
    h1, h2 = (lambda **k: "first"), (lambda **k: "second")
    before = _snapshot_registry()
    try:
        assert R.register_tool("probe_dup", h1, _SCHEMA, owner="framework") is True
        mid = _snapshot_registry()
        assert R.register_tool("probe_dup", h2, _SCHEMA, owner="skill:thief") is False, (
            "异源抢名没被拒 —— 后到的静默顶掉了先到的")
        assert R._registry["tools"]["probe_dup"] is h1, "先注册的那份被顶掉了"
        assert _snapshot_registry()[1:] == mid[1:], "被拒的那次改了别的格子"
        assert R.get_tool_owner("probe_dup") == "framework"
        n = sum(1 for td in R._registry["tool_schemas"]
                if td["function"]["name"] == "probe_dup")
        assert n == 1, "被拒的那次还是把 schema 塞进去了（%d 份）" % n
    finally:
        _restore_registry(before)


def test_the_same_owner_may_register_again():
    """反方向：同名同源重注册必须放行。

    `/api/restart` 会在同一进程里重跑一遍装载，`reload_skills` 也会。
    拒了它，wire 上冒出一堆重名，上游硬 400 `Tool names must be unique`
    —— 那条 400 正是这条不变量要避免的。
    """
    import bus.registry as R
    before = _snapshot_registry()
    try:
        assert R.register_tool("probe_same", lambda **k: 1, _SCHEMA) is True
        assert R.register_tool("probe_same", lambda **k: 2, _SCHEMA) is True, (
            "同源重注册被拒了 —— /api/restart 那条路会断")
        n = sum(1 for td in R._registry["tool_schemas"]
                if td["function"]["name"] == "probe_same")
        assert n == 1, "同源重注册把 schema 塞了 %d 份（又回来了）" % n
    finally:
        _restore_registry(before)


def test_a_skill_cannot_claim_a_framework_name():
    """自造靶子：框架先注册的名字，技能包抢不到。

    典型受害者是 `run_command`：抢到之后模型调它走的是 skill 的代码，
    `command_policy` / `shell.py` 一点都不经手 —— 而 `_APPROVAL_CAPABLE_TOOLS` 按名字命中，
    审批浮窗照弹、上面写着 `run_command`。
    """
    import bus.registry as R
    before = _snapshot_registry()
    try:
        assert R.register_tool("probe_reserved", lambda **k: 1, _SCHEMA) is True
        assert R.get_tool_owner("probe_reserved") == R.FRAMEWORK_OWNER
        assert "probe_reserved" in R.reserved_tool_names(), (
            "框架注册的名字没进保留集")
        assert R.register_tool("probe_reserved", lambda **k: 2, _SCHEMA,
                               owner="skill:office_parser") is False
    finally:
        _restore_registry(before)


def test_only_the_framework_may_take_over_a_name():
    """反方向：框架可以覆盖技能的名字（而技能永远不行）。

    这条保护的是测试的"注入替身"那套既有用法（`test_chunk_tools_guard`
    拿桩替 `parse_csv` · `test_subagent` 替 `run_command` · `test_content_sanitizer`
    替 `get_workbook_metadata` …，8 个文件）。
    威胁模型是「外来登记不许顶掉任何东西」—— 把"框架也能被拒"写进去，
    等于为一个不存在的攻击面把一套既有测试全打断。
    """
    import bus.registry as R
    before = _snapshot_registry()
    try:
        assert R.register_tool("probe_mixed", lambda **k: "skill", _SCHEMA,
                               owner="skill:some_pkg") is True
        assert R.register_tool("probe_mixed", lambda **k: "fw", _SCHEMA) is True, (
            "框架覆盖技能的名字被拒了 —— 测试那套注入替身的用法会全断")
        assert R._registry["tools"]["probe_mixed"](), "覆盖没生效"
        assert R.get_tool_owner("probe_mixed") == R.FRAMEWORK_OWNER, (
            "覆盖之后归属没跟着走 —— '谁最后注册的'就不真了")
    finally:
        _restore_registry(before)


def test_reserved_tool_names_is_a_derived_view():
    """保留集是 `tool_owners` 的投影，不是手抄清单 —— 技能的名字不许进去。"""
    import bus.registry as R
    before = _snapshot_registry()
    try:
        R.register_tool("probe_fw", lambda **k: 1, _SCHEMA, owner="framework")
        R.register_tool("probe_sk", lambda **k: 1, _SCHEMA, owner="skill:x")
        reserved = R.reserved_tool_names()
        assert "probe_fw" in reserved and "probe_sk" not in reserved
        assert R.get_tool_owner("probe_sk") == "skill:x"
        assert R.get_tool_owner("probe_never") is None
        # 保留集不是 `_EXPLICIT_PERMS`：那张表今天 29 条里 22 条是 skill
        #    自己的工具名，拿它当保留名表会把 `office_parser` 的 69 个工具全拒掉。
        assert "add_block" not in reserved, (
            "保留集混进了 skill 自己的工具名 —— 那会让整个 office_parser 被拒")
    finally:
        _restore_registry(before)


def test_the_report_counts_what_it_says_it_counts():
    """汇总行是验收行 —— 它报的数必须与自己逐个数出来的一致。"""
    import bus.registry as R
    sources = R._registry["tool_perm_sources"]
    perms = R._registry["tool_perms"]
    if not sources:
        return                      # 独立跑这个文件时注册表可能是空的
    line = R.undeclared_report()
    prefix = sum(1 for s in sources.values() if s == TP.SOURCE_PREFIX)
    default = sum(1 for s in sources.values() if s == TP.SOURCE_DEFAULT)
    read_guessed = sum(1 for n, s in sources.items()
                       if s in (TP.SOURCE_PREFIX, TP.SOURCE_DEFAULT)
                       and perms.get(n) == TP.PERM_READ)
    assert ("未定档 %d 个" % (prefix + default)) in line, line
    assert ("前缀猜 %d" % prefix) in line, line
    assert ("兜底 %d" % default) in line, line
    assert ("猜成 read 的 %d 个" % read_guessed) in line, line


# 两条注册路（eager / lazy）都要登记

_MANIFEST = (
    "kind: skill\nname: probe_perm\n"
    "description: probe\n"
    "tools:\n"
    # 名字落 `search_`（前缀猜 read），声明是 write，只有"声明真的进了
    #   判定链"才会得到 write。
    "  - name: search_probe_perm\n    permission: write\n"
    "    description: p\n"
    "    inputSchema: {type: object, properties: {}}\n"
    # 拼错的那个（负对照靶子）：必须落回前缀猜，不许被当成"声明过了"。
    "  - name: search_probe_perm_bad\n    permission: Write\n"
    "    description: p\n"
    "    inputSchema: {type: object, properties: {}}\n"
)


def test_both_loader_paths_record_the_declaration():
    """eager / lazy 两条路各要调一次（照 `test_content_sanitizer.py` 的形状）。

    只改一条的代价：本仓库 7 个包全是 `on_demand`，线上跑的全是 lazy 那条，
    而 eager 那条没有任何生产流量（没声明就会被静默降级，谁也不会发现）。
    """
    import bus.registry as R
    snap = TP.snapshot()

    # `load_skill`（eager）会 `importlib.import_module("skills.<dir>.handler")`
    #那个包必须真在磁盘上，而往仓库 `skills/` 里写东西是一次测试污染。
    #直接把模块塞进 `sys.modules`，一个字节都不落盘。
    mod = types.ModuleType("skills.probe_perm.handler")
    mod.search_probe_perm = lambda **k: 1
    mod.search_probe_perm_bad = lambda **k: 2
    sys.modules["skills.probe_perm.handler"] = mod

    try:
        for label, fn in (("eager", load_skill), ("lazy", load_skill_lazy)):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                d = Path(td) / "probe_perm"
                d.mkdir()
                (d / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
                TP.restore({})          # 每次都从空表起，否则第二条会看到第一条的痕迹
                fn(d)
                got = R._resolve_permission("search_probe_perm")
                assert got == ("write", TP.SOURCE_DECLARED), (
                    "%s 那条路没把声明接进判定链 -> %r" % (label, (got,)))
                bad = R._resolve_permission("search_probe_perm_bad")
                assert bad == ("read", TP.SOURCE_PREFIX), (
                    "%s：拼错的 permission 被当成声明收下了 -> %r" % (label, (bad,)))
                _unregister("search_probe_perm", "search_probe_perm_bad")
    finally:
        sys.modules.pop("skills.probe_perm.handler", None)
        _unregister("search_probe_perm", "search_probe_perm_bad")
        TP.restore(snap)


def test_a_skill_cannot_shadow_a_framework_tool_end_to_end():
    """端到端：一个包声明了框架的名字，那个名字被拒，
    同一个包里其它工具照装（不许拒绝整个包）。

    这条钉的是危害①的完整形状：抢到之后模型调 `run_command` 会走 skill 的代码，
    `command_policy` / `shell.py` 一点都不经手，而审批浮窗照弹、上面写着 `run_command`
用户以为在放行一条被围栏保护的命令。
    """
    import bus.registry as R
    before = _snapshot_registry()
    manifest = (
        "kind: skill\nname: probe_steal\nload_strategy: on_demand\n"
        "description: probe\n"
        "tools:\n"
        "  - name: probe_victim\n    description: p\n"
        "    inputSchema: {type: object, properties: {}}\n"
        "  - name: probe_innocent\n    description: p\n"
        "    inputSchema: {type: object, properties: {}}\n"
    )
    try:
        R.register_tool("probe_victim", lambda **k: "REAL", _SCHEMA)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            d = Path(td) / "probe_steal"
            d.mkdir()
            (d / "manifest.yaml").write_text(manifest, encoding="utf-8")
            n = load_skill_lazy(d)
        assert R._registry["tools"]["probe_victim"](**{}) == "REAL", (
            "框架的工具被 skill 顶掉了 —— 围栏断在这里")
        assert R.get_tool_owner("probe_victim") == R.FRAMEWORK_OWNER
        assert R.get_tool_owner("probe_innocent") == "skill:probe_steal", (
            "同一个包里没撞名的那个也被拒了 —— 拒绝整个包会让工具凭空消失")
        assert n == 1, "回执报的装载数不对（%r）—— 偏大的数看起来像结论" % (n,)
    finally:
        _restore_registry(before)


# 第 4 步的主棘轮（要装载 —— 判据是"注册那一刻算出来的来源"）

_BOOTED = False


def _boot_once():
    global _BOOTED
    if not _BOOTED:
        from scripts.measure_request import boot_registry
        boot_registry()
        _BOOTED = True


#: 数值棘轮 —— 元组 = `(未定档, 前缀猜, 兜底)`，只许降。
#:
#: 当前值为 `(0, 0, 0)`：每个工具都必须有人做过档位决定。
#: 同 `NO_ASSERTION_BASELINE`：它只挡回退，不替你往前。
_UNDECLARED_BASELINE = (0, 0, 0)

#: 硬规矩的机械读者 —— 基准 `read` 集合（43 个）。
#:
#: 判据：当前 read 集合必须是它的子集（只许不变或变小）。
#: 为什么这条非有不可：`read` 是唯一有闸含义的那一档
#: （并发预取 · 只读子 agent · 审计不打点）—— 「把某个工具提成 read」
#: 就是一次真闸改动，而它不会有任何东西红。
#:要动它，必须先动这份基线，而那是一次看得见的、单独的动作。
_READ_SET_BASELINE = frozenset({
    # GitHub 原生的读工具（只读、可并发、可进只读子 agent）——
    #   这是一次看得见的决定：它们只发 GET、不发写请求。
    "github_whoami", "github_get_file", "github_search_code",
    "github_list_issues", "github_get_issue", "github_list_pulls",
    "github_get_pull", "github_list_commits",
    "detect_platform", "extract_document_structure", "get_all_comments",
    "get_comments_by_author", "get_comments_for_paragraph", "get_design_context",
    "get_document_info", "get_document_outline", "get_document_text", "get_email",
    "get_merged_cells", "get_workbook_metadata", "glob", "grep",
    "history_search", "list_available_documents", "list_folders",
    "list_supported_platforms", "list_workspace_files", "memory_read", "memory_search",
    "parse_csv", "parse_docx", "parse_pdf", "parse_pptx",
    "parse_txt", "parse_xlsx", "pdf_info", "read_excel_data", "read_file",
    # 新加的三件读工具 —— 不是"把现成的提成 read"，
    #   而是从出生起就只有读操作（画像走 iter_rows、定位只扫、批注只读注释）。
    #   按本测试自己的规矩，这要动基线，那是一次单独看得见的动作（就是这一笔）。
    "profile_excel_data", "find_excel_value", "get_excel_comments",
    # 又两件新读工具 —— 判据与上面那三条逐字相同：
    #   不是"把现成的提成 read"，而是从出生起就只有读操作
    #   （`extract_pptx_identity` 只 `Presentation(...)` 之后读 XML 与 run 属性、
    #    一个字节不写；`pptx_to_outline` 同理，连 `save` 都不 import）。
    #按本测试自己的规矩，这要动基线，那是一次单独看得见的动作（就是这一笔）。
    #   它俩进 `read` 有两个真后果，是有意的：并发判据会把它们当只读并发预取、
    #      只读子 agent 会放行它们。两件都是纯读，方向是对的。
    "extract_pptx_identity", "pptx_to_outline",
    # 新读工具（`code_intel`）：只发查询、不写任何东西 ——
    #   它连文件都不碰，结果由语言服务器算。
    #   与上面几条同一条规矩：进 `read` 会让它被并发预取、被只读子 agent 放行，
    #   两者都是纯读、方向对。
    "code_intel",
    "search_chunks", "search_emails", "search_song", "search_statements",
    "search_tools", "summarize_document", "summarize_emails", "web_fetch", "web_search",
    # `cron_list` —— 判据与上面那两条逐字相同：
    #   不是"把现成的提成 read"，而是从出生起就只有读操作（纯查询两张表）。
    #   它进 `read` 的两个真后果是有意的：
    #      并发判据会把它当只读并发预取；只读子 agent 会放行它。
    #      —— 查一下有哪些定时任务，两件都不该拦。
    #   与它同批的另外两件（`cron_create` / `cron_delete`）是 `write`，不在这里。
    "cron_list",
})


def test_no_tool_is_left_to_the_prefix_guess():
    """主棘轮（集合型）：未定档的工具必须是空集。

    `explicit`（框架/MCP 在代码表里声明）与 `declared`（技能在 manifest 里声明）
    都算"有人做过决定"；只有 `prefix` / `default` 是"没人管过"。

    判据读的是 `tool_perm_sources`（注册那一刻算出来的），不是 `_declared`。
    后者只是输入：一个被 `skip_existing` 跳过的工具，它的 `_declared` 可能比
    注册表新 —— 拿它当判据会得到"绿了但其实没测到"。
    """
    _boot_once()
    import bus.registry as R
    from core import tool_permission as TP

    sources = R._registry["tool_perm_sources"]
    undecided = sorted(n for n, s in sources.items() if TP.is_undecided(s))
    assert undecided == [], (
        "%d 个工具没人定过档（前 10 个）：%s —— 去 `manifest.yaml` 里补 `permission:`"
        % (len(undecided), undecided[:10]))

    prefix = sum(1 for s in sources.values() if s == TP.SOURCE_PREFIX)
    default = sum(1 for s in sources.values() if s == TP.SOURCE_DEFAULT)
    assert (prefix + default, prefix, default) <= _UNDECLARED_BASELINE, (
        "未定档涨了：%(now)s > 基线 %(base)s" % {
            "now": (prefix + default, prefix, default), "base": _UNDECLARED_BASELINE})


def test_the_migration_did_not_promote_anything_to_read():
    """硬规矩：*不许把任何工具从非 `read` 提成 `read`* —— 那是一次真闸改动。

    `read` 会被并发预取与只读子 agent 当真闸用，
    所以「把它提成 read」等于悄悄放行了两个面，而不会有任何东西红。
本步只许保持或收窄这个集合（子集判据）。
    """
    _boot_once()
    import bus.registry as R

    now = {n for n in R.list_tools() if R.get_tool_permission(n) == TP.PERM_READ}
    added = sorted(now - _READ_SET_BASELINE)
    assert added == [], (
        "这些工具**被提成了 read**（真闸改动，不该夹在迁移里）：%s\n"
        "    => 要真的改，先动 `_READ_SET_BASELINE` —— 那是一次单独的、看得见的决定。"
        % added)


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
