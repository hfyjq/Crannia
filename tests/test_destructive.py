"""`core/tool_permission.py` 第二根轴（`destructive`）的机械读者。

它钉的是声明层，不是闸：运行期闸（`destructive` 且不在
`_APPROVAL_CAPABLE_TOOLS`，问）单独落。

判据与 `tests/test_permission.py` 同族（那个文件钉第一根轴）：

  1. `None` 与 `False` 必须分得开 —— 那边"没声明"落进一个非空的档
     （`admin`），一眼看得出不对；这边"没声明"与"声明了可逆"长得一模一样
     （都是 `False`），棘轮数的就是那个 `None`，它塌掉整条棘轮就恒为 0。
  2. 只收真 `bool` —— `0` / `"false"` / `yes` 一律不登记。
     与第一根轴代价方向相反：那边的非法值落 `admin`（fail-closed），
     这边落兜底的 `False`（fail-open），非法值必须由三层挡住。
  3. 判定次序（声明 -> 代码表 -> 兜底）—— 每条配一个自造靶子。
  4. 兜底方向是刻意钉住的（见 `test_the_fallback_direction_is_pinned`）。

**`_declared` / `_declared_destructive` 都是进程级全局**（两张表都登记在
`core/state_registry.py`），本文件的规矩是存了再还原（`snapshot` / `restore`），
绝不 `clear()`。`snapshot()` / `restore()` 一对管两根轴，就是为了防
"只还原了一根、另一根泄漏到同批后跑的测试里"。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_destructive.py
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bus.loaders.skill_loader import load_skill, load_skill_lazy   # noqa: E402
from core import tool_permission as TP                              # noqa: E402

#: 合成表 —— 与生产无关，只用来验顺序（`bus/registry.py` 注入真的那张）。
#: `add_block` 在真实世界里是 `write` + 可逆，这里当"表说 False"
#: 而"声明说 True"的靶子。
EXPLICIT = {"delete_files": True, "add_block": False}


# 1. 声明层：None vs False

def test_none_and_false_are_distinguishable():
    """**`declared_destructive` 必须把"没声明"与"声明了可逆"分开**。

    这条是整根轴的承重件：棘轮（`SOURCE_DEFAULT` 的个数只许降）数的就是 `None`。
    两者一旦塌成同一个值（比如都用 `or False` 读），棘轮恒为 0、
    而它看起来是"全都声明过了"（读不到 ≠ 0 那个形状）。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        assert TP.declared_destructive("add_block") is None, (
            "没声明读出来不是 None（塌成 False 了？）")
        assert TP.declare_destructive("add_block", False) is True
        assert TP.declared_destructive("add_block") is False, (
            "声明了 False 与'没声明'必须是两个值")
        assert TP.declared_destructive("add_block") is not None
    finally:
        TP.restore(snap)


def test_only_real_bools_are_accepted():
    """只收真 `bool`。非法值落的是 fail-open 那一侧（见模块 docstring）。

    `0` / `1` 是 `int` 不是 `bool` —— 这一条同时是
    `isinstance(True, int)` 那个坑的反面。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        for bad in (0, 1, "true", "false", "", None, [], (), {}, 0.0, 1.0):
            name = "t_bad_%d" % id(bad)
            assert TP.declare_destructive(name, bad) is False, (
                "非法值被收下了: %r" % (bad,))
            assert TP.declared_destructive(name) is None, bad
        assert TP.declare_destructive("t_true", True) is True
        assert TP.declare_destructive("t_false", False) is True
        assert TP.declared_destructive("t_true") is True
        assert TP.declared_destructive("t_false") is False
    finally:
        TP.restore(snap)


def test_declaration_is_idempotent_and_overwrites_in_place():
    """重启框架会在同一进程里重新注册一遍，就地覆盖，不是 append。

    覆盖包括从 `True` 改回 `False` —— 一个包改了主意，声明必须跟着改
    （`declare()` 从不清空，所以"删掉声明"这件事今天做不到，是已知的不对称）。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare_destructive("probe", True)
        TP.declare_destructive("probe", False)
        assert TP.declared_destructive("probe") is False, "第二次声明没覆盖掉第一次"
        assert TP.snapshot()["destructive"] == {"probe": False}, (
            "表里留了多余的登记: %r" % (TP.snapshot()["destructive"],))
    finally:
        TP.restore(snap)


# 2. 判定次序（每层一个自造靶子）

def test_a_declaration_beats_the_explicit_table():
    """自造靶子：表说 `False`，声明说 `True`，必须是 `True`。

    这条就是次序的负对照：把 `resolve_destructive` 的声明那一支挪到
    表之后，这里立刻红（而全仓其它地方一处都不会红）。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare_destructive("add_block", True)
        got = TP.resolve_destructive("add_block", explicit=EXPLICIT)
        assert got == (True, TP.SOURCE_DECLARED), (
            "声明没有压过代码表 -> %r（次序被重排了？）" % (got,))
    finally:
        TP.restore(snap)


def test_the_explicit_table_beats_the_default():
    """自造靶子（另一半）：表里两条都算数 —— 尤其 `False` 那一条。

    只钉 `True` 那半会漏掉一个真 bug：把"表里没有"与"表里是 `False`"
    写成同一条分支，显式写下的"这个工具可逆"被兜底吃掉，
    而两条看起来都像"没查着"。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        assert TP.resolve_destructive("delete_files", explicit=EXPLICIT) == (
            True, TP.SOURCE_EXPLICIT)
        assert TP.resolve_destructive("add_block", explicit=EXPLICIT) == (
            False, TP.SOURCE_EXPLICIT), "表里写了 False，来源却不是 explicit"
    finally:
        TP.restore(snap)


def test_the_fallback_direction_is_pinned_on_purpose():
    """兜底是 `False`（fail-open），这是刻意的、有账的 —— 别顺手"修正"它。

    闸是「`destructive` 且不在 `_APPROVAL_CAPABLE_TOOLS`，问」，
    而那张能力表今天只有三个名字（`run_command` / `delete_files` /
    `request_cross_zone_search`）。兜底若翻成 `True`，
    **`add_block` 与每一个 `set_*` 都会弹窗**（129 个工具），闸当场不可用。

未声明的不靠兜底承重，靠另外两层：
    可数（`SOURCE_DEFAULT`，棘轮只许降）+ 可证伪（第 5 步 AST 反查：
    命中不可逆原语却没声明，红）。

    谁要把它翻成 fail-closed，先读模块 docstring 的「第二根轴」一节
    并想清楚那 129 个弹窗怎么收场 —— 本测试就是拦住"顺手改一下"的那道闸。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        val, src = TP.resolve_destructive("mystery_tool", explicit={})
        assert (val, src) == (False, TP.SOURCE_DEFAULT), (
            "兜底方向变了：%r（见本测试 docstring 的那笔账）" % ((val, src),))
        assert TP.is_undecided(src) is True, "兜底没被算进'没人管'，棘轮会恒为 0"
        assert TP.is_undecided(TP.SOURCE_DECLARED) is False
        assert TP.is_undecided(TP.SOURCE_EXPLICIT) is False, (
            "explicit 是'一条条定过的'，不该算没人管")
    finally:
        TP.restore(snap)


def test_the_resolver_only_returns_real_bools():
    """`resolve_destructive` 的返回值只可能是真 `bool`（第二道保险）。

    非法声明 + 非法表值都喂一遍 —— 闸那边是 `if is_destructive(...)`，
    一个 `1` / `"true"` 今天碰巧判对，而那正是它不该被依赖的样子。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        for bad in (0, 1, "true", None, []):
            TP.declare_destructive("m1", bad)          # 不收，落兜底
            val, _src = TP.resolve_destructive("m1", explicit={})
            assert isinstance(val, bool) and val is False, (bad, val)
            # 表值非法也不许把非 bool 透出去
            val2, _s2 = TP.resolve_destructive("m2", explicit={"m2": bad})
            assert isinstance(val2, bool), (bad, val2)
    finally:
        TP.restore(snap)


# 3. 两根轴互不干扰 + 夹具覆盖两根

def test_the_two_axes_are_orthogonal():
    """`permission` 与 `destructive` 互不干扰 —— 两张表、两组函数。

    反例（要防的形状）：拿 `permission != "read"` 当 `destructive` 的近似。
    今天 `add_block` 是 `write` 而完全可逆，`run_command` 是 `admin`
    而不可逆，两者不重合，近似会在两个方向各错一次。
    """
    snap = TP.snapshot()
    try:
        TP.restore({})
        TP.declare("x", "write")
        TP.declare_destructive("x", True)
        assert TP.declared_perm("x") == "write"
        assert TP.declared_destructive("x") is True
        TP.declare("y", "read")
        assert TP.declared_destructive("y") is None, "声明档位动了 destructive 表"
        TP.declare_destructive("z", False)
        assert TP.declared_perm("z") is None, "声明 destructive 动了档位表"
    finally:
        TP.restore(snap)


def test_snapshot_and_restore_cover_both_axes():
    """一对函数管三根轴 —— 少还原一根，它就泄漏到同批后跑的测试里。

    症状长这样：某个测试莫名看到一条不属于它的声明，而它自己的
    `restore` 明明调了。`tool_output_kind` 那个坑（清空，后跑的看不到）
    是同一个根的另一半。

    加第三根轴时它会当场红（`snapshot()` 多了一格）—— 那正是它该做的事。
    """
    before = TP.snapshot()
    try:
        TP.restore({})
        assert TP.snapshot() == {"perm": {}, "destructive": {},
                                 "concurrency": {}}, TP.snapshot()
        TP.declare("probe_perm", "read")
        TP.declare_destructive("probe_dest", True)
        TP.declare_concurrency("probe_conc", True)

        snap = TP.snapshot()
        TP.restore({})                                   # 空值 = 三张表都清
        assert TP.declared_perm("probe_perm") is None
        assert TP.declared_destructive("probe_dest") is None
        assert TP.declared_concurrency("probe_conc") is None

        TP.restore(snap)                                 # 还原 = 三张表都回来
        assert TP.declared_perm("probe_perm") == "read"
        assert TP.declared_destructive("probe_dest") is True
        assert TP.declared_concurrency("probe_conc") is True
    finally:
        TP.restore(before)


# 4. 注册表那一层

_SCHEMA = {"description": "p", "inputSchema": {"type": "object", "properties": {}}}

#: 注册表里派生自注册那一刻的全部格子 —— 探针注册一次会写六格，
#: 清理就必须清六格。少清一格 = 一条静默泄漏到同批后跑的测试里。
_CELLS = ("tools", "tool_perms", "tool_perm_sources",
          "tool_destructive", "tool_destructive_sources", "tool_owners")


def _snap_all():
    import bus.registry as R
    return {k: dict(R._registry[k]) for k in _CELLS} | {
        "tool_schemas": list(R._registry["tool_schemas"])}


def _restore_all(snap):
    import bus.registry as R
    for k in _CELLS:
        R._registry[k].clear()
        R._registry[k].update(snap[k])
    R._registry["tool_schemas"][:] = snap["tool_schemas"]


def test_registering_records_the_second_axis_too():
    """`register_tool` 必须两根轴都记，且各带来源。"""
    import bus.registry as R
    snap = TP.snapshot()
    allsnap = _snap_all()
    try:
        TP.restore({})
        R.register_tool("probe_dest_1", lambda **k: 1, _SCHEMA)
        assert R.get_tool_destructive("probe_dest_1") is False, (
            "未声明应当落到兜底 False（fail-open，见 test_the_fallback_direction...）")
        assert R._registry["tool_destructive_sources"]["probe_dest_1"] == TP.SOURCE_DEFAULT
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


def test_a_declaration_reaches_the_registry():
    """自造靶子：声明 `True`，注册之后 `get_tool_destructive` 必须回 `True`。

    这条同时钉住"声明压过兜底" —— 把 `_resolve_destructive` 里的
    `explicit` 那一支提到声明之前，或者干脆不读声明表，这里当场红。
    """
    import bus.registry as R
    snap = TP.snapshot()
    allsnap = _snap_all()
    try:
        TP.restore({})
        TP.declare_destructive("probe_dest_2", True)
        R.register_tool("probe_dest_2", lambda **k: 1, _SCHEMA)
        assert R.get_tool_destructive("probe_dest_2") is True
        assert R._registry["tool_destructive_sources"]["probe_dest_2"] == TP.SOURCE_DECLARED
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


def test_the_two_cells_are_born_and_die_together():
    """两格同生同死 —— 与第一根轴那条同族：有值没来源（或反过来）
    就是一份说不清的账。
    """
    import bus.registry as R
    allsnap = _snap_all()
    try:
        R.register_tool("probe_dest_3", lambda **k: 1, _SCHEMA)
        vals = set(R._registry["tool_destructive"])
        srcs = set(R._registry["tool_destructive_sources"])
        assert vals == srcs, (
            "第二根轴两格对不上 —— 只在值里的: %r · 只在来源里的: %r"
            % (sorted(vals - srcs), sorted(srcs - vals)))
        assert all(s in TP.VALID_SOURCES
                   for s in R._registry["tool_destructive_sources"].values())
    finally:
        _restore_all(allsnap)


#: 判为不可逆的工具 —— 裁决的结果。
#:
#: 第 6 步（落闸）不会改这个集合 —— 闸读的是差集，它不写声明。
#: 要动它 = 一次策略改动，而这里就是那道"看得见"的闸。
#:
#: 加两个 —— 理由是诚实的：
#:   * `remove_excel_duplicates` —— 丢的行回不来（没有撤销）
#:   * `recalculate_excel_formulas` —— LibreOffice 往返**可能丢掉图表 / 条件格式 /
#:     数据验证 / 透视表**（它按自己的理解重新序列化整个文件）
#:两条都按「改得回来吗」的判据如实声明 `true`。
_DESTRUCTIVE_TRUE = frozenset({
    "delete_files", "send_email", "run_command",
    "remove_excel_duplicates", "recalculate_excel_formulas",
    # `pii_reveal` —— 真值一旦进了这段对话的历史
    #   就收不回来（落库 + 之后每次请求都发上游）。判据是「改得回来吗」。
    #   而它在 `_APPROVAL_CAPABLE_TOOLS` 里，闸跳过它（它自己会弹）——
    #   所以它不在下面那条爆炸半径里。
    "pii_reveal",
    # `memory_delete` —— 记忆内容不保留（封条只挡"再生成"，不恢复正文）。
    #   它不在 `_APPROVAL_CAPABLE_TOOLS`（没有自己的发射点），走兜底闸，
    #   于是出现在下面那条爆炸半径里。
    "memory_delete",
    # 周期任务两件：
    #   · `cron_create`：定义本身删得掉，但它造的是一件以后会自己跑起来的活；
    #     真到点时若没人在场，那条任务只会挂起（审批靠"回答变成新的一轮"，
    #     那条路永不挂起），无事发生。问在创建那一刻才有效。
    #   · `cron_delete`：删掉的定义回不来（与 `delete_files` 同族）。
    "cron_create",
    "cron_delete",
    # `run_program` —— 子进程里能跑任意 Python（与 `run_command` 同级），
    #   脚本里又没有审批可挂（没有"回答变成下一轮"）。
    #按「改得回来吗」如实判 True，走兜底闸每次问。
    "run_program",
    # 接管 —— 它直接控制用户的鼠标键盘，
    #   能点出一个收不回来的结果（删数据 / 付款）。判据是「改得回来吗」。
    #   它在 `_APPROVAL_CAPABLE_TOOLS` 里（逐应用授权的发射点在它自己的
    #   handler 里），不在下面那条爆炸半径里。
    "computer",
    # 浏览器接管 —— 能点出收不回来的结果
    #   （提交表单 / 付款 / 对外发送），与 `computer` 同一条判据。
    "browser",
    # 出站：发出去收不回，而且是对外可见的动作。`_EXPLICIT_DESTRUCTIVE`
    #   里早就写着 True，但这条断言一直没看见它 ——
    #   `scripts/measure_request.boot_registry` 没注册出站工具，
    #   于是这三个"枚举全注册表"的读者都少算了一个。补注册之后才照出来。
    "send_outbound",
})


def test_exactly_the_known_tools_are_destructive():
    """判为不可逆的恰好这些。

    各有出处：`delete_files`（删用户的文件）·
    `send_email`（发出去收不回来）·
    `run_command`（能做没人要求过的事 —— 如实声明，而它在能力名单里，闸跳过它）·
    `remove_excel_duplicates` / `recalculate_excel_formulas`（见 `_DESTRUCTIVE_TRUE` 的注释）·
    `pii_reveal`（真值进历史收不回来）·
    `cron_create` / `cron_delete`（见上面那两行）。

    这条钉的是裁决结果。别把断言删掉（删掉就没人盯着这个数了）。
    """
    _boot_once()
    import bus.registry as R
    got = frozenset(n for n, v in R._registry["tool_destructive"].items() if v)
    assert got == _DESTRUCTIVE_TRUE, (
        "判为不可逆的集合变了：多了 %r · 少了 %r\n"
        "  ★ 多一个 = 闸可能开始为一个新工具弹窗；少一个 = 那个工具静默不再被问。"
        % (sorted(got - _DESTRUCTIVE_TRUE), sorted(_DESTRUCTIVE_TRUE - got)))


def test_the_gate_blast_radius_is_exactly_five():
    """闸的爆炸半径 —— 机械钉住。

    闸是「`destructive` 且不在 `_APPROVAL_CAPABLE_TOOLS`，问」。
    满足这条的是八个：`send_email`（`delete_files` / `run_command` / `pii_reveal`
    各自有审批发射点、已在能力名单里 —— 它们不在这里）
    + 两个 Excel 写工具 + 两个周期任务工具。

 第四次变大（6 → 7）：`run_program` —— 子进程里能跑
      任意 Python，而且脚本里没有审批可挂，每次问。

 第三次变大（5 → 6）：`memory_delete`。      删掉的内容不保留（封条只挡重生），
      不可逆，走兜底闸，每次都问。

    这一格两次变大，两次都是看得见的策略改动：
      · 从 1 变成 3：`remove_excel_duplicates` /
        `recalculate_excel_formulas` 被如实判为不可逆，会弹审批浮窗。
      · 从 3 变成 5：`cron_create` / `cron_delete`。
        `cron_create` 的判据在 `_DESTRUCTIVE_TRUE` 那两行里逐字写着
        （造的是以后会自己跑起来的活）。
这条测试就是那些"看得见"的落点 —— 改它必须说得出为什么。

    这条不是在测闸本身 —— 它测的是"闸落下之后会有几个工具被问"。
    """
    _boot_once()
    import bus.registry as R
    from core.agent_loop import _APPROVAL_CAPABLE_TOOLS as CAP
    hits = sorted(n for n, v in R._registry["tool_destructive"].items()
                  if v and n not in CAP)
    assert hits == ["cron_create", "cron_delete", "memory_delete",
                    "recalculate_excel_formulas",
                    "remove_excel_duplicates", "run_program",
                    "send_email", "send_outbound"], (
        "闸一落地就会问这几个工具：%r\n"
        "  （期望 ['cron_create', 'cron_delete', 'recalculate_excel_formulas', "
        "'remove_excel_duplicates', 'send_email']）\n"
        "  多了 = 爆炸半径被改宽了；少了 = 某个不可逆的工具静默不再被问。\n"
 "从 3 变成 5：cron_create / cron_delete-- 用户点名要的\n"
        "  （拍板 1「cron_create 要过审批」）。判据：定义本身删得掉，但它造的是\n"
        "  一件以后会自己跑起来的活，而到点时若没人在场，那条任务只会挂起\n"
        "  （审批靠'回答变成新的一轮'，那条路永不挂起）=> 无事发生。\n"
        "  所以问在**创建那一刻**才是有效的。它**不进** _APPROVAL_CAPABLE_TOOLS\n"
        "  （那个集合是'自己有发射点、闸别再问'，方向相反）。\n"
        "  [!] 这段字面量列的是 GBK 安全的一串（本仓 GBK 基线只许降）"
        % (hits,))


def test_the_report_counts_undeclared_and_true():
    """启动汇总行把两个数都报出来：未声明（fail-open 那一侧）与判为不可逆。"""
    import bus.registry as R
    snap = TP.snapshot()
    allsnap = _snap_all()
    try:
        TP.restore({})
        R.reset()
        R.register_tool("probe_rep_a", lambda **k: 1, _SCHEMA)      # 未声明
        TP.declare_destructive("probe_rep_b", True)                 # 声明不可逆
        R.register_tool("probe_rep_b", lambda **k: 1, _SCHEMA)
        line = R.undeclared_destructive_report()
        assert "未声明 1 个" in line, line
        assert "已声明 1" in line, line
        assert "判为不可逆 1 个" in line, line
    finally:
        _restore_all(allsnap)
        TP.restore(snap)


_MANIFEST = (
    "kind: skill\nname: probe_dest\n"
    "description: probe\n"
    "tools:\n"
    # 真布尔，声明必须进判定链，注册之后 `get_tool_destructive` 回 True。
    #   （`output_kind` 是别的轴的声明，写上只为让输出安静 —— 与本条无关。）
    "  - name: get_probe_dest\n    permission: read\n    output_kind: structured\n"
    "    destructive: true\n"
    "    description: p\n"
    "    inputSchema: {type: object, properties: {}}\n"
    # 变异靶子：`yes` 会被 PyYAML 读成 True（碰巧对），所以这里用字符串
    #   `"true"` —— 它必须落回兜底 False。
    "  - name: get_probe_dest_bad\n    permission: read\n    output_kind: structured\n"
    "    destructive: \"true\"\n"
    "    description: p\n"
    "    inputSchema: {type: object, properties: {}}\n"
)


def test_both_loader_paths_record_the_declaration():
    """eager / lazy 两条路各要调一次（照 `test_permission.py` 的形状）。

    只改一条的代价：本仓 7 个包全是 `on_demand`，线上跑的全是 lazy 那条，
    而 eager 那条没有任何生产流量 —— 漏了它，谁也不会发现。

    判据读的是注册表（`tool_destructive`，注册那一刻算出来的），
    不是 `_declared` 声明表 —— 后者只是输入，拿它当判据会得到
    "绿了但其实没测到"（声明表对，注册表没跟上）。
    """
    import bus.registry as R
    snap = TP.snapshot()
    allsnap = _snap_all()

    # `load_skill`（eager）会 `importlib.import_module("skills.<dir>.handler")`
    #那个包必须真在磁盘上，而往仓库 `skills/` 里写东西是一次测试污染。
    #直接把模块塞进 `sys.modules`，一个字节都不落盘。
    mod = types.ModuleType("skills.probe_dest.handler")
    mod.get_probe_dest = lambda **k: 1
    mod.get_probe_dest_bad = lambda **k: 2

    try:
        for label, fn in (("eager", load_skill), ("lazy", load_skill_lazy)):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                d = Path(td) / "probe_dest"
                d.mkdir()
                (d / "manifest.yaml").write_text(_MANIFEST, encoding="utf-8")
                TP.restore({})       # 每次都从空表起，否则第二条会看到第一条的痕迹
                sys.modules["skills.probe_dest.handler"] = mod
                fn(d)
                assert R.get_tool_destructive("get_probe_dest") is True, (
                    "%s 那条路没把 destructive 声明接进判定链" % label)
                assert R._registry["tool_destructive_sources"]["get_probe_dest"] \
                    == TP.SOURCE_DECLARED
                assert R.get_tool_destructive("get_probe_dest_bad") is False, (
                    "%s：字符串 \"true\" 被当成布尔收下了（fail-open 的一侧）" % label)
                _restore_all(allsnap)
    finally:
        sys.modules.pop("skills.probe_dest.handler", None)
        _restore_all(allsnap)
        TP.restore(snap)


def test_the_second_axis_has_exactly_the_known_production_readers():
    """闸落地了，而且两条路都有 —— 锁步判据。

    这条钉的是"点名那一处"（同 `test_tiers` 的
    `test_consumers_are_exactly_the_known_ones`：
    多一处要解释、少一处要报警）。

    少一处最危险：闸整个消失，而别的测试照样全绿 ——
    `_destructive_gate_needed` 只在派发链里被调，没有第二条路会碰它。
所以这里数两个数：判据函数定义处 1 个（`get_tool_destructive`），
    调用点 2 个（同步 + 流式）。

    判据按 AST，不 grep —— grep 会把注释判成用法。
    """
    import ast
    roots = ("core", "bus", "web", "channels", "providers", "mcpx", "skills", "scripts")
    root = Path(__file__).resolve().parent.parent
    #: 生产代码里允许出现的调用点 —— `{文件: 次数}`。
    #:
    #: 4 = 每条派发路 2 处 × 2 条路：
    #:   ① 子 agent 那条闸的条件里一次（"子 agent 里要开审批的" —— 兜底闸算一种）
    #:   ② 闸自己那一支一次
    #: 那两处不是重复计算：`subagent.depth() > 0 and (...)` 是短路求值，
    #: 非子 agent 的轮次里 ① 的第二个操作数根本不会执行。
    # 4 -> 5。多出来那一处是自动模式的预授权
    # （`_auto_review_gate` 里那一次）—— 它问的正是"这个工具要不要开一次审批"
    # （要的话，分类器说安全就得先替用户放行）。它不是新开了一条审批路，
    # 而是复用同一条判据去决定"要不要让下面那条路弹窗"。
    # 按本条自己的规矩：多一处要解释 —— 这就是解释。
    #: 从 5 变 7 —— 加了第三条派发路：定时任务那一轮。
    #: 它不是"多了一条会开审批的路"，正相反：它是一条会【拒绝】开审批的路
    #: （判据见 `core/agent_loop.py::_approval_in_cron_message`）。判据同上面那句
    #: "多一处要解释" —— 解释在这里：到点时没有人能回答审批，而挂起的代价是
    #: 一个冻结用户桌面的全屏浮窗 + 一轮没有任何回复。
    #:净效果是审批变少。少了 7 才该报警（某条路上的判定被删了）。
    expected = {"core/agent_loop.py": 7}
    calls: dict[str, int] = {}
    registry_hits = []
    for r in roots:
        for p in sorted((root / r).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(root).as_posix()
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)):
                    continue
                if node.func.id == "_destructive_gate_needed":
                    calls[rel] = calls.get(rel, 0) + 1
                if node.func.id == "get_tool_destructive" and rel != "bus/registry.py":
                    registry_hits.append("%s:%d" % (rel, node.lineno))
    assert calls == expected, (
 "闸的调用点变了：%r（期望 %r —— 每条派发路 2 处：条件里 + 闸自己那一支）\n"
        "  ★ **少一处** = 有一条派发路没有闸（`A11`：sync 与 stream 必须锁步）；\n"
        "  ★ **多一处** = 有第二条路会开审批，来这条把预期的数改掉并说明为什么。"
        % (calls, expected))
    #: 新增了第二个读者 —— 判据是这条不在"闸"那条线上，
    #: 所以它不该并进上面的 `expected`（那是"派发路有几处开审批"）。
    #:
    #: `core/auto_review.py` 读它，问的是另一个问题：
    #: 「这个工具可逆吗」—— 因为第 ② 层跳过（写 + 可逆的快通道）
    #: 只对「写 + 可逆」生效：删除不可逆，删工作区里的文件照样要过闸。
    #:那一处加宽了闸（多问一次），而不是新开了一条审批路。
    #: 这正是本仓那条「多一处要解释」—— 上面那句 `assert` 数的量
    #: （`_destructive_gate_needed` 的调用点）没变，变的是第二根轴的读者数。
    # 第二处：`core/tool_program.py` 问的是
    #: "这个工具能不能在编排脚本里调"，其中一半判据正是"它是不是不可逆"
    #: （脚本里没有审批可挂）。它不是派发路上的一次开审批。
    _ALLOWED_EXTRA_READERS = {"core/auto_review.py", "core/tool_program.py"}
    readers = {h.split(":")[0] for h in registry_hits}
    assert readers <= {"core/agent_loop.py"} | _ALLOWED_EXTRA_READERS, (
        "`get_tool_destructive` 的生产读者多了一处没登记的地方：%r\n"
        "  （`bus/registry.py` 是定义处，不算。**加读者要说清它问的是不是同一个问题**。）"
        % (registry_hits,))
    assert "core/agent_loop.py" in readers, (
        "`core/agent_loop.py` 那一处读者不见了 —— 兜底闸被摘掉了？%r" % (registry_hits,))


# 5. 数值棘轮

_BOOTED = False


def _boot_once():
    """装载注册表 —— 必须，否则 `_registry` 是空的，量到的数是 0，
    而 0 看起来像一个结论（探针的输入源必须先核对生产从哪里读）。
    """
    global _BOOTED
    if not _BOOTED:
        from scripts.measure_request import boot_registry
        boot_registry()
        _BOOTED = True


#: 迁移期的数值棘轮 —— `(未声明总数, 其中非 read 的)`，只许降。
#:
#: 动工前实测（`boot_registry()` 之后当场数）：`(132, 89)`；
#: 落地当天到 `(0, 0)` —— 每个工具都声明，包括 43 个 `read` 工具
#: （它们的 `destructive` 恒为 `false`，
#: 而那一行是"有人看过了"的凭证，不是"这条会不会危险"的信息）。
#: 与第一根轴那条 `_UNDECLARED_BASELINE` 同规矩：它只挡回退，不替你往前。
_UNDECLARED_DESTRUCTIVE_BASELINE = (0, 0)


def test_the_undeclared_count_only_goes_down():
    """验收行：还有多少个工具「没人给过 `destructive` 声明」。

    判据读的是 `tool_destructive_sources`（注册那一刻算出来的），
    不是 `_declared` —— 后者只是输入，一个被 `skip_existing` 跳过的工具
    会让它比注册表新，拿它当判据会得到"绿了但其实没测到"
    （同 `test_permission.py::test_no_tool_is_left_to_the_prefix_guess`）。
    """
    _boot_once()
    import bus.registry as R
    sources = R._registry["tool_destructive_sources"]
    assert sources, "注册表是空的 —— boot 没跑成，下面的数会是假的 0"
    undeclared = [n for n, s in sources.items() if TP.is_undecided(s)]
    nonread = [n for n in undeclared
               if R.get_tool_permission(n) != TP.PERM_READ]
    assert (len(undeclared), len(nonread)) <= _UNDECLARED_DESTRUCTIVE_BASELINE, (
        "没人给过 destructive 声明的工具变多了：%r > 基线 %r\n  前 10 个: %r"
        % ((len(undeclared), len(nonread)), _UNDECLARED_DESTRUCTIVE_BASELINE,
           undeclared[:10]))


# runner

def _main() -> int:
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
