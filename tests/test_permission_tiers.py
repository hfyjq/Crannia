"""
权限档（read / write / admin）的机械读者。

为什么立这个文件
================
`bus/registry.py` 里那张档位表把 `read`/`write`/`admin`
只当作拼进 description 给模型看的字符串时，没有任何代码读它做判定。

现在有两处拿它当真闸用：

    1. 并发预取 —— `get_tool_permission(name) != "read"`
整批退回串行。标错了就等于让一个会写的东西跟别的读抢。
    2. `subagent.is_write_like()` —— `!= "read"`，
       `run_subagent(readonly=True)` 拒绝执行。**标错了就等于只读子 agent
       可以改文件。**

表从「描述性」变成「承重」，每一格都必须与实现一致。
标成 read、而实现里有写标记的工具有 5 条：

        search_and_replace_text   命中 `search_`，read，而它 `doc.save()` 改文档
        extract_archive           命中 `extract_`，read，而它 `mkdir` + 解压写盘
        extract_pages             命中 `extract_`，read，而它 `out_doc.save()` 写新 PDF
        mock_image                `mock_` 是写前缀，却被显式覆盖成 read，而它 `img.save()`
        create_document           `create_` 是写前缀，却被显式覆盖成 read，
                                  而它写 `_wip_*.docx` 且 `store.create()` 改共享 `_sessions`

最后两条的形状特别值得记：它们同时出现在两张表里（`create_`/`mock_`
在写表、名字本身在读表），而读表先查，结果落成 `read`。
两张表各说一次，"谁赢"取决于代码顺序 —— 那正是「两套真相源」。

错的方向是「把写当成读」（危险那一侧），所以修法一律是 `write`。

本文件守什么
============
三条不变量，每条都有负对照：

  ① read 档的工具，实现里不许有写标记（AST 扫，不 import —— 见下）
  ② 两张前缀表不许重叠（同一个名字在两张表里 = 谁赢看代码顺序）
  ③ 每条 `_EXPLICIT_PERMS` 都指向真注册的工具、档位合法（已有读者在
     `test_registry_idempotent.py`，这里只补它管不到的那半）

**为什么必须用 AST 扫源码，不能 `inspect.getsource(handler)`**：
技能是懒加载的（`on_demand`），`get_tool_handler()` 回的是桩。
用桩做扫描会同时犯两个错：

    · 报 6 条假阳性（把 `list.append` / `str.replace` 当成写盘）
    · 漏掉全部 5 条真的（因为读的是桩，桩里没有写点）

探针读错了源，而它产出的是一份看起来很硬的清单。
本文件的判据是「在文件系统里按 AST 找那个 `def`」，与注册表怎么装载无关。
（`test_the_scanner_can_go_red` 就是钉这一条的。）

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_permission_tiers.py
"""

import ast
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only output -- the Windows console defaults to GBK and emoji would
# crash the whole run (and the failure reporter with it).

#: 扫哪几个目录（生产代码；`tests/` 与 `dist/` 不算）
_SCAN_DIRS = ("skills", "core", "bus")

#: 写标记 —— 只收「改用户可见状态」的那种。
#:
#: [!] 三类必须排除的假阳性（每一条都对应一类已知的误报形状，不是预防性排除）：
#:
#:   1. `append` / `replace` / `insert` —— `list` / `str` 的方法。
#:      读 `inspect.getsource` 的桩做扫描会报 6 条这个形状的假阳性。
#:   2. `mkdir` / `makedirs` —— 出现在路径解析 helper 里
#:      （`word_tools._resolve_path` / `excel_tools._resolve_xlsx_path` 的
#:      `workspace.mkdir(parents=True, exist_ok=True)`），幂等、建的是容器不是数据，
#:      而每个只读工具都会走那条路 -> 一次报了 9 条假阳性。
#:   3. `"write"` 作为默认参数值（`_resolve_path(file_path, op: str = "write")`）
#:      —— 那是函数签名，不是调用点的断言。见 `_write_marks` 里对调用点的限定。
#:
#: 判据：一条护栏只要带已知假阳性，就会被人挂一张豁免表，然后就烂了。
#: 所以这里宁可窄 —— 5 条真的它一条不漏（逐条手工核过），假阳性 0。
_WRITE_ATTRS = frozenset({
    "save", "savefig", "write_text", "write_bytes", "writelines", "dump",
    "rmtree", "unlink", "rename", "move",
    "copy", "copy2", "copyfile", "extractall", "unpack_archive",
    "to_excel", "to_csv", "insert_pdf",
})

#: 第二张动词表：不可逆的 ——
#: 「改得回来吗」，与 `_WRITE_ATTRS`（「改不改东西」）正交，所以是另一张表。
#:
#: 它为什么住在这个文件里：这里是扫描器的家（`_collect_defs` /
#: `_write_marks` 都在这儿，`scripts/propose_permissions.py` 也从这里 import）。
#: 不是"因为它属于档位测试" —— 两张表都是扫描器认识的动词，
#: 分开放会让"加一个动词"变成两处动作（而漏掉一处不会有任何东西红）。
#:
#: 刻意窄（同 `_WRITE_ATTRS` 那条判据：带已知假阳性的护栏会被人挂豁免表、然后烂掉）：
#: 排除了 `replace`（`str.replace` 到处都是）、`remove`（`list.remove`）、
#: `send`（面太宽）—— 留的是"调用它就没了"的那几个。
#: 机械读数（`boot_registry()` 后全扫 132 个工具）：命中 7 个，
#: 逐条人工核过，假阳性 0、漏 0（六个候选 + 多抓出来的 `rename_worksheet`）。
IRREVERSIBLE_ATTRS = frozenset({
    "rmtree", "unlink", "rmdir", "truncate",      # 删
    "sendmail", "send_message",                   # 发出去收不回来
    "os.replace", "rename", "move",               # 原地覆盖（重建后盖回去）
})

#: 这些工具在源码里找不到同名 `def`，且理由成立。
#: [!] 它是白名单不是黑名单：新出现一个解析不出来的工具 -> 红。
_NO_DEF_EXPECTED = {
    # MCP 内置（`mcp/servers/`），工具名是注册时给的，函数名不同
    "web_search": "MCP built-in (mcpx/servers/web_search.py)",
    "web_fetch":  "MCP built-in (mcp/servers/web_fetch.py)",
}


# ------------------------------------------------------------------
# 源码索引：一趟扫全部文件，收集 `def <name>` 的位置
# ------------------------------------------------------------------

def _collect_defs():
    """{函数名: [(文件, 起始行, ast 树)]} —— 一趟扫完，不 import。"""
    index: dict[str, list[tuple[str, int, ast.Module, str]]] = {}
    for d in _SCAN_DIRS:
        for p in sorted((_PROJECT_ROOT / d).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(src)
            except (SyntaxError, OSError):
                continue
            rel = str(p.relative_to(_PROJECT_ROOT)).replace("\\", "/")
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    index.setdefault(node.name, []).append((rel, node.lineno, tree, src))
    return index


def _write_marks(node, tree, attrs=_WRITE_ATTRS, extra_rules=True) -> set[str]:
    """扫一个函数体（外加同文件里被它按裸名调用的一跳）里的写标记。

    一跳是为了不漏这种形状::

        def tool_x(f):        return _really_writes(f)
        def _really_writes(f): f.save(...)

    动词表可换（`attrs=`）—— 换 `IRREVERSIBLE_ATTRS` 就是
    「这个工具有没有不可逆的动作」。同一个扫描器，两张表，
    不抄第二份（抄第二份 = 两份漂移，而两个答案看着都对）。

    另外两条规则（`open(w)` 与 `调用点传 "write"`）是「改」的信号，不是「不可逆」的，
    所以扫不可逆时要 `extra_rules=False` 关掉它们 —— 否则 132 个工具里
    52 个会被算成不可逆（实测），而那个数看起来像一个结论。
    """
    marks: set[str] = set()
    names_seen: set[str] = set()

    def _scan(n):
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call):
                f = sub.func
                parts = []
                while isinstance(f, ast.Attribute):
                    parts.append(f.attr)
                    f = f.value
                if isinstance(f, ast.Name):
                    parts.append(f.id)
                dotted = ".".join(reversed(parts))
                tail = dotted.rsplit(".", 1)[-1] if dotted else ""
                if tail in attrs:
                    marks.add(dotted)
                if extra_rules and dotted == "open":
                    for a in sub.args[1:2]:
                        if (isinstance(a, ast.Constant)
                                and isinstance(a.value, str)
                                and any(c in a.value for c in "wax+")):
                            marks.add("open(w)")
                # 裸名调用 -> 记下来，回头在同一个文件里找它的定义
                if isinstance(sub.func, ast.Name):
                    names_seen.add(sub.func.id)
                # `_resolve_path(x, "write")` / `validate_path(x, "write")` 的形状
                # —— 只认调用点的实参，不认函数签名里的默认值
                # （`op: str = "write"` 是 `_resolve_path` 自己的声明，
                #   一跳进来会把每个只读工具都染上，会引入 9 条假阳性）。
                for a in list(sub.args) + [k.value for k in sub.keywords]:
                    if isinstance(a, ast.Constant) and a.value == "write":
                        if extra_rules:
                            marks.add('调用点传 "write"')

    _scan(node)
    for name in names_seen - {node.name}:
        for sub in ast.walk(tree):
            if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == name):
                _scan(sub)
    return marks


# ------------------------------------------------------------------
# 1. 主不变量：read 档的工具，实现里不许有写标记
# ------------------------------------------------------------------

_BOOTED = False


def _boot():
    """装载注册表 —— 必须，否则每条都是 `admin`。

    [!] 判据在 `get_tool_permission` 自己身上：它读 `_registry["tool_perms"]`
    而默认值是 `"admin"`。-> 没装载时全表都是 admin，而 `admin` 看起来
    像一个结论（"它不可并发"），其实只是"还没登记"。
    同族于「读不到一律 `None`，不是 `0`」那条判据。
    """
    global _BOOTED
    if _BOOTED:
        return
    scripts = str(_PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from measure_request import boot_registry
    boot_registry()
    _BOOTED = True


def test_read_tiered_tools_have_no_write_markers():
    from bus.registry import list_tools, get_tool_permission

    _boot()
    index = _collect_defs()

    read_tier = [n for n in sorted(list_tools())
                 if get_tool_permission(n) == "read"]
    assert read_tier, "一个 read 档工具都没有 —— 注册表没装载？"

    offenders, unresolved = {}, []
    for name in read_tier:
        defs = index.get(name)
        if not defs:
            unresolved.append(name)
            continue
        marks = set()
        for _rel, _ln, tree, _src in defs:
            target = next((x for x in ast.walk(tree)
                           if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and x.name == name), None)
            if target is not None:
                marks |= _write_marks(target, tree)
        if marks:
            offenders[name] = sorted(marks)

    assert not offenders, (
        "标成 `read`、而实现里有写标记：%r\n"
        "  -> 它会被 `A7` 放进并发批次，也会被 `run_subagent(readonly=True)` 放行。\n"
 " -> 修法是加进 `bus/registry.py::_EXPLICIT_PERMS` 定成 \"write\"。"
        % offenders)

    extra = set(unresolved) - set(_NO_DEF_EXPECTED)
    assert not extra, (
        "这些 read 档工具在源码里找不到同名 `def`，而它们不在已知例外里：%r\n"
        "  -> 要么它换了实现名（加进 `_NO_DEF_EXPECTED` 并写清为什么），\n"
        "  -> 要么扫描器的判据漏了（那更糟：它等于**静默跳过**了这些工具）。"
        % sorted(extra))
    print("  [ok] read 档 %d 个，实现里写标记 0 条；"
          "解析不出定义的 %d 个都在已知例外里"
          % (len(read_tier), len(_NO_DEF_EXPECTED)))


# ------------------------------------------------------------------
# 2. 两张前缀表不许重叠 —— 「谁赢看代码顺序」是一种真相源
# ------------------------------------------------------------------

def test_prefix_tables_do_not_overlap():
    import bus.registry as R

    overlap = sorted(set(R._READ_PREFIXES) & set(R._WRITE_PREFIXES))
    assert not overlap, (
        "同一个名字同时出现在 `_READ_PREFIXES` 与 `_WRITE_PREFIXES`：%r\n"
        "  -> `_classify_permission` 先查 read -> 它**永远是 read**，\n"
        "  -> 而写表里那一行是**死代码**，读起来却像「它可能是写」。\n"
        "  -> 想表达「这条特殊」就写进 `_EXPLICIT_PERMS`，别靠顺序。" % overlap)
    print("  [ok] 两张前缀表零重叠（%d 条 / %d 条）"
          % (len(R._READ_PREFIXES), len(R._WRITE_PREFIXES)))


# ------------------------------------------------------------------
# 3. 五条误档工具：逐个钉死
# ------------------------------------------------------------------

def test_c2_offenders_stay_fixed():
    """下面这 5 条必须保持 `write`，不许被改回 read。

    每条都带独立理由（不是"因为历来如此"）—— 理由在注释里写清了，
    所以有人想把 `create_document` 改回 read 换并发时，会先看到代价。
    """
    import bus.registry as R

    _boot()
    want = {
        # 改文档：`doc.save()`，且它自己 `_resolve_path(..., "write")`
        "search_and_replace_text": "write",
        # 解压写盘：`out_dir.mkdir()` / 必要时 `shutil.rmtree()`
        "extract_archive":         "write",
        # 产出新 PDF：`out_doc.save()`
        "extract_pages":           "write",
        # 写图片文件：`img.save()` + `open(..., "w")`
        "mock_image":              "write",
        # 写 `_wip_*.docx` 且 `store.create()` 改共享 `_sessions` 字典
        "create_document":         "write",
        # 改共享会话态：`session.spec_lock[...].update(...)`
        "set_slide_design":        "write",
    }
    for name, tier in want.items():
        got = R.get_tool_permission(name)
        assert got == tier, (
 "%s 的档是 %s，期望 %s —— 已于修过，"
            "改回去之前先读 `tests/test_permission_tiers.py` 的文件头。"
            % (name, got, tier))

    # 负对照：真·只读的没被误伤
    for name in ("get_design_context", "read_excel_data", "glob", "grep"):
        assert R.get_tool_permission(name) == "read", \
            "%s 被误改成了 write（只读的东西不该因为这一批一起变）" % name
    print("  [ok] C2 那 6 条仍是 write；4 个真只读未被误伤")


# ------------------------------------------------------------------
# 4. 负对照：扫描器必须能红
# ------------------------------------------------------------------

def test_the_scanner_can_go_red():
    """钉住扫描器本身有判别力。

    它针对的失败模式：按 `inspect.getsource(handler)` 扫描读到的是
    懒加载的桩，那会报 6 条假阳性、漏掉全部 5 条真的。
    -> 下面的断言分两半：认得写点，且不误报 Python 内建方法。
    """
    src = (
        "import shutil\n"
        "def tool_a(p):\n"
        "    p.save(str(p))\n"
        "def tool_b(items, s):\n"
        "    items.append(1)\n"          # 假阳性 1：list 方法
        "    s.replace('a', 'b')\n"      # 假阳性 1：str 方法
        "def tool_c(p):\n"
        "    open(p, 'w').write('x')\n"
        "def tool_d(p):\n"
        "    return _inner(p)\n"
        "def _inner(p):\n"
        "    shutil.rmtree(p)\n"
        "def tool_e(p):\n"
        "    return _resolve(p)\n"
        "def tool_f(p):\n"
        "    return _resolve(p, 'read')\n"
        "def tool_g(p):\n"
        "    return _resolve(p, 'write')\n"
        "def _resolve(p, op='write'):\n"   # 假阳性 3：默认参数值
        "    Path('workspace').mkdir(parents=True, exist_ok=True)\n"  # 假阳性 2
        "    return p\n"
    )
    tree = ast.parse(src)
    by_name = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    # 必须认得（漏了 = 5 条真阳性一条都抓不到）
    assert _write_marks(by_name["tool_a"], tree), \
        "扫描器漏了 `.save(` —— 那 5 条真阳性它一条都抓不到"
    assert _write_marks(by_name["tool_c"], tree), \
        "扫描器漏了 `open(..., 'w')`"
    assert _write_marks(by_name["tool_d"], tree), \
        "扫描器漏了**一跳**：`tool_d` 自己不写，`_inner` 写"
    assert _write_marks(by_name["tool_g"], tree), \
        "扫描器漏了调用点显式传 `\"write\"`（`validate_path(x, \"write\")` 的形状）"

    # 必须不报（每一批都是一个必须排除的假阳性形状）
    assert not _write_marks(by_name["tool_b"], tree), (
        "扫描器把 `list.append` / `str.replace` 当成写盘了 —— "
        "第一版就是被这三个坑出一批假阳性的：%r" % _write_marks(by_name["tool_b"], tree))
    assert not _write_marks(by_name["tool_e"], tree), (
        "扫描器把 helper 里的 `mkdir(exist_ok=True)` 当成写盘了 —— "
        "那一次性报了 9 条假阳性（每个只读工具都走路径解析）：%r"
        % _write_marks(by_name["tool_e"], tree))
    assert not _write_marks(by_name["tool_f"], tree), \
        "扫描器把显式传 `\"read\"` 的调用也判成写了"
    print("  [ok] 扫描器：认得 .save/open(w)/一跳/调用点\"write\"；"
          "不误报 append·replace·mkdir·默认参数")


def test_the_scanner_does_not_read_the_registry():
    """判据是源码 AST，不是注册表里的 handler 对象。

    这一条防的是"有人把实现改成 `inspect.getsource(get_tool_handler(n))`
    更省事" —— 那个写法在 `on_demand` 技能上是错的源（读到的全是桩）。
    """
    import inspect
    src = inspect.getsource(_write_marks)
    assert "get_tool_handler" not in src and "inspect.getsource" not in src, (
        "`_write_marks` 开始读注册表里的 handler 了 —— 技能是懒加载的，"
        "那读到的会是**桩**（第一版翻车现场）"
    )
    assert "ast" in src, "`_write_marks` 不再按 AST 扫了？"
    print("  [ok] 扫描器读的是源码 AST，不是注册表 handler")


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
