"""skill 兼容性体检（`core/skill_doctor.py`）—— 机械读者。

## 这个文件钉住的三条最要紧的

1. 它自己不执行任何东西。探针模块最容易在"顺手 import 一下"里
   把第三人的代码跑起来 —— 那正是它要防的事。判据是源码 AST：
   不许出现 `import_module` / `exec` / `eval` / `compile` / `__import__` /
   `exec_module` / `spec_from_file_location`。
2. 它自己不改任何东西（只读）。判据同上 + 跑前后目录指纹一致。
3. 「没查」不许塌成「没问题」。
   两处会"没查"：调用方没给注册表（重名那一项）、`handler.py` 里有
   `from X import *`（工具在不在那一项）。
它们必须出现在 `Diagnosis.skipped` 里，且不许报 fail。

## 判断工具在不在：看命名空间，不看顶层 def

只看 `handler.py` 顶层的 `def` 时，`office_parser`（69 个工具，
用 `from .word_tools import *` 把名字带进来）会报 54 条假阳。
判据是「这个名字在模块命名空间里吗」（收 `def` + `import` + 赋值），
而 `import *` 静态不可判，登记成没查。
这条在本文件里有双向的读者：`test_star_import_is_reported_not_failed`
（不许报 fail）与 `test_named_import_counts`（不许漏报）。

## 真实目录

`test_real_skills_directory_is_examined` 跑真的 `skills/`，断言没有任何包不通过。
新加的包不通过说明护栏在工作；老包新坏要先查文件是否被误删。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_skill_doctor.py
"""
#@exclusive: skills-dir
# 这个文件会真的碰 `skills/`（建 / 删 / 扫），而别的测试也会在那个目录里
#   建又删（净变化为零），并联会互相撞车。
# → 机制与理由在 `scripts/run_tests.py::_EXCLUSIVE_RE`。

import ast
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import skill_doctor as SD  # noqa: E402

_MODULE_SRC = (_PROJECT_ROOT / "core" / "skill_doctor.py").read_text(encoding="utf-8")


# 夹具：在临时目录里造一个 skill 包

_GOOD_MANIFEST = """\
kind: skill
name: demo
version: "0.1.0"
draft: false
load_strategy: on_demand
description: a demo skill
tools:
  - name: demo_tool
    permission: read
    destructive: false
    description: does a thing
    inputSchema:
      $schema: "https://json-schema.org/draft/2020-12/schema"
      type: object
      properties:
        text:
          type: string
      required:
        - text
"""

_GOOD_HANDLER = """\

def init(security_guard=None):
    pass


def demo_tool(text=""):
    return {"success": True, "echo": text}
"""


def _make_skill(root: Path, name: str = "demo", *,
                manifest: str = _GOOD_MANIFEST,
                handler: str = _GOOD_HANDLER,
                skill_md: str = "---\nname: demo\ndescription: demo\n---\nbody\n",
                with_init: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (d / "manifest.yaml").write_text(manifest, encoding="utf-8")
    if handler is not None:
        (d / "handler.py").write_text(handler, encoding="utf-8")
    if skill_md is not None:
        (d / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if with_init:
        (d / "__init__.py").write_text("", encoding="utf-8")
    return d


def _codes(diag, severity=None):
    return sorted(f.code for f in diag.findings
                  if severity is None or f.severity == severity)


def _tree(root: Path) -> dict:
    """整个临时目录的指纹 —— "体检不许改任何东西"的判据。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


# 1. 它自己不执行、不改动

#: 出现即违规的调用名。只列真的会执行代码的那些 ——
#: `re.compile` 也含 `compile`，但它编的是正则不是代码，按裸名判就分得开。
_FORBIDDEN_EXEC = (
    "import_module", "exec_module", "spec_from_file_location", "load_module",
)
_FORBIDDEN_BARE = ("exec", "eval", "compile", "__import__")

#: 出现即违规的写调用（体检是只读的）
_FORBIDDEN_WRITE = (
    "write_text", "write_bytes", "mkdir", "unlink", "remove", "rmtree",
    "rename", "replace", "copy", "copy2", "move",
)


def test_this_module_executes_nothing():
    """判据是源码 AST，不是靠人读一遍。

    探针模块最容易在"顺手 import 一下"里把第三人的代码跑起来 ——
    而那正是它要防的事（`ast.parse` 与 `import` 的全部区别就在这）。
    """
    tree = ast.parse(_MODULE_SRC)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_BARE:
            bad.append("%s() 裸调用（第 %d 行）" % (fn.id, node.lineno))
        elif isinstance(fn, ast.Attribute) and fn.attr in _FORBIDDEN_EXEC:
            bad.append(".%s()（第 %d 行）" % (fn.attr, node.lineno))
    assert not bad, (
        "体检器里出现了会执行代码的东西 —— 它就不再是「不执行任何东西」了：\n  "
        + "\n  ".join(bad))
    # 反面自证：它确实用了 ast（不然上面那条可能是因为根本没解析）
    assert "ast.parse(" in _MODULE_SRC, (
        "它没在用 ast.parse —— 那它要么在 import、要么什么都没查")
    print("  [ok] 源码里没有 import_module/exec/eval/compile/__import__，"
          "且确实走 ast.parse")


def test_this_module_writes_nothing():
    tree = ast.parse(_MODULE_SRC)
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _FORBIDDEN_WRITE:
                bad.append(".%s()（第 %d 行）" % (node.func.attr, node.lineno))
    assert not bad, "体检器里有写操作 —— 它必须是**只读**的：\n  " + "\n  ".join(bad)
    print("  [ok] 源码里没有任何写操作（只读）")


def test_examine_leaves_the_directory_untouched():
    """源码读者之外的行为读者：跑一遍，目录指纹必须一模一样。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _make_skill(root)
        before = _tree(root)
        for _ in range(3):
            SD.examine(root / "demo")
            SD.examine_all(root)
        after = _tree(root)
        assert before == after, (
            "体检改了东西 —— 差异：%r" % (set(after.items()) ^ set(before.items()),))
    print("  [ok] 跑三次之后目录指纹逐字节一致")


# 2. 正常的包：通过

def test_a_good_skill_passes():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _make_skill(root)
        d = SD.examine(root / "demo")
        assert d.ok, "好包被判成不通过：%r" % (d.findings,)
        assert not d.by_severity(SD.SEV_FAIL), d.findings
        assert d.tools == ("demo_tool",), d.tools
        assert SD.log_line(d).startswith("[skill-doctor] OK"), SD.log_line(d)
    print("  [ok] 正常的包通过，日志行是 OK")


def test_compat_message_only_on_failure_and_carries_the_badge():
    """文案是两个通道：UI 那份带符号，日志那份一个符号都没有。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _make_skill(root, "bad", manifest="kind: skill\n")   # 没有 tools
        d = SD.examine(root / "bad")
        assert not d.ok

        msg = SD.compat_message(d)
        # 断言模块自己的常量，不在这里抄第二份 ——
        # 抄了的话：(a) 源码里多一个 GBK 编不出的字符（基线 60 不许增长，
        #   这条当场被 `check_gbk_safe.py` 抓到，60 -> 61），
        #   (b) 换符号要改两处。
        assert SD._UI_BADGE in msg, (
            "UI 文案里没有那个符号（用户给了稿，要保留）：%r" % msg[:40])
        assert "错误插件：bad" in msg, msg
        assert "对此结果有反馈？" in msg, msg
        assert "\n" in msg

        # 第二形态：控制台/日志那一份，一个会崩的符号都没有
        plain = SD.compat_message(d, ui=False)
        # 判据用真判据本身（能不能 GBK 编码），不用 Unicode 区间代理 ——
        # 手搓区间（例如 `0x2500 < c < 0x9FFF`）会把全角逗号 `，`（U+FF0C）
        # 误报，而它在 GBK 里完全合法。
        #选错了观测量。
        for ch in plain + SD.log_line(d) + SD.summary_line(d):
            try:
                ch.encode("gbk")
            except UnicodeEncodeError:
                raise AssertionError(
                    "控制台那一份里有 GBK 编不出的符号 %r（U+%04X）—— "
                    "`check_gbk_safe.py` 的基线 60 不许增长"
                    % (ch, ord(ch)))

        # 负对照：徽章是空的，上面第一条就变成恒真了。
        # 没有这行，「UI 文案带符号」可能因为哪里都没有符号而通过。
        assert SD._UI_BADGE, "`_UI_BADGE` 是空的 —— 第一条断言就是恒真的"
        assert SD._UI_BADGE not in plain, (
            "`ui=False` 那一份没换徽章 —— 它是要进 print 的")
    print("  [ok] 两个形态：UI 带符号、控制台不带（两个通道各自成形态）")


# 3. 各类不兼容

def test_manifest_level_failures():
    cases = [
        ("manifest 解不开", "kind: [skill\n", _GOOD_HANDLER, "manifest_unparsable"),
        ("kind 不对", "kind: plugin\nname: demo\ntools: []\n", _GOOD_HANDLER,
         "manifest_kind"),
        ("没有 tools", "kind: skill\nname: demo\n", _GOOD_HANDLER, "tools_missing"),
    ]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        for label, manifest, handler, code in cases:
            d = SD.examine(_make_skill(root, "s_" + code, manifest=manifest,
                                       handler=handler))
            assert code in _codes(d, SD.SEV_FAIL), (
                "%s 没被抓到（拿到了 %r）" % (label, _codes(d)))

        # "没 manifest"分两种，判得不一样：
        #   只有 `SKILL.md` 的目录开机会被点名列出来（`skill_md.py` 那一类
        #   是一等公民）—— 所以对它说「框架看不见这个包」是错的，`warn`。
        #   而两样都没有的那种仍然是 `fail`（那才是真的"看不见"）。
        #这一对必须成对断言：只留降级那半，等于把这条判据悄悄关掉。
        only_md = SD.examine(_make_skill(root, "s_only_md", manifest=None))
        assert "manifest_missing" in _codes(only_md, SD.SEV_WARN), (
            "有 SKILL.md 而没有 manifest -> 该是 warn（拿到了 %r）" % _codes(only_md))
        assert "manifest_missing" not in _codes(only_md, SD.SEV_FAIL), _codes(only_md)

        neither = SD.examine(_make_skill(root, "s_neither", manifest=None,
                                         skill_md=None))
        assert "manifest_missing" in _codes(neither, SD.SEV_FAIL), (
            "两样都没有 -> 仍然是 fail（拿到了 %r）" % _codes(neither))
        print("  [ok] manifest 层四种坏形状各抓到一条：%s"
              % ", ".join(c for _, _, _, c in cases))
        print("  [ok] 而「没 manifest」分两种：有 SKILL.md -> warn · 两样都没有 -> fail")


def test_draft_must_be_a_real_bool():
    """`draft: "false"`（带引号）在 YAML 里解出来是字符串，
    而字符串是真值，这个 skill 被当成草稿静默跳过。

    这个坑只有静态体检看得见 —— 运行时它表现为"这个 skill 不存在"，
    而作者会去查几十个别的地方。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        d = SD.examine(_make_skill(
            root, "quoted",
            manifest=_GOOD_MANIFEST.replace("draft: false", 'draft: "false"')))
        assert "draft_not_bool" in _codes(d, SD.SEV_FAIL), _codes(d)
        assert not d.ok

        # 负对照：真的布尔值必须不报
        d2 = SD.examine(_make_skill(root, "honest"))
        assert "draft_not_bool" not in _codes(d2), _codes(d2)
    print("  [ok] `draft: \"false\"` 被抓（字符串是真值）；真布尔不报")


def test_handler_level_failures():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # 缺 handler.py
        d = SD.examine(_make_skill(root, "noh", handler=None))
        assert "handler_missing" in _codes(d, SD.SEV_FAIL), _codes(d)

        # 语法不通
        d = SD.examine(_make_skill(root, "syn", handler="def broken(:\n"))
        assert "handler_syntax_error" in _codes(d, SD.SEV_FAIL), _codes(d)

        # 声明的工具既没有 def 也没有 import
        d = SD.examine(_make_skill(root, "ghost", handler="def other():\n    pass\n"))
        assert "handler_tool_not_found" in _codes(d, SD.SEV_FAIL), _codes(d)
    print("  [ok] handler 缺 / 语法错 / 声明的工具找不到 —— 三种都 fail")


def test_schema_and_duplicate_tools():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        bad_schema = _GOOD_MANIFEST.replace("type: object", "type: array", 1)
        d = SD.examine(_make_skill(root, "sc", manifest=bad_schema))
        assert "tool_schema_invalid" in _codes(d, SD.SEV_FAIL), _codes(d)

        # 这个锚点与 `_GOOD_MANIFEST` 逐字耦合 —— 往夹具里加一行字段，
        # 这里也得跟着加，否则 `replace` 静默什么都不做（`dup` 变成好包，
        # 而测试以为自己在测重名）。
        dup = _GOOD_MANIFEST.replace(
            "  - name: demo_tool\n    permission: read\n    destructive: false\n"
            "    description: does a thing",
            "  - name: demo_tool\n    permission: read\n    destructive: false\n"
            "    description: first\n"
            "  - name: demo_tool\n    permission: read\n    destructive: false\n"
            "    description: second", 1)
        d = SD.examine(_make_skill(root, "dup", manifest=dup))
        assert "tool_duplicate_in_manifest" in _codes(d, SD.SEV_FAIL), _codes(d)
    print("  [ok] inputSchema 非法 + manifest 内重名 —— 都 fail")


# 4. 「没查」不许塌成「没问题」

def test_name_collision_needs_the_registry_and_says_so_when_absent():
    """没给注册表，登记成没查，且不许因为没有注册表就报 fail。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _make_skill(root)

        d = SD.examine(root / "demo")                      # 没给注册表
        assert SD.SKIP_NO_REGISTRY in d.skipped, (
            "没给注册表却没说 —— 「没查」被塌成了「没问题」：%r" % (d.skipped,))
        assert "tool_name_collision" not in _codes(d, SD.SEV_FAIL), _codes(d)
        assert SD.SKIP_NO_REGISTRY in SD.log_line(d) or d.ok, SD.log_line(d)

        # 给了注册表、本包自己的工具在里面，不撞（它与自己重名不算撞）
        d = SD.examine(root / "demo", registered_names={"demo_tool"})
        assert d.ok and not d.skipped, (
            "包与自己撞名了（69 条假阳的形状）：%r / %r" % (d.findings, d.skipped))

        # 名字被别的 skill 包也声明了，撞 —— 与注册表无关，
        # 两个 manifest 都在磁盘上，静态就判得了
        # `str.replace` 默认全替换：必须带行尾 + `count=1`，
        # 否则会连 `name: demo_tool` 一起改掉（这坑今天踩了三次）。
        _make_skill(root, "rival",
                    manifest=_GOOD_MANIFEST.replace("name: demo\n", "name: rival\n", 1))
        d = SD.examine(root / "demo")          # 连注册表都不给
        assert "tool_name_collision" in _codes(d, SD.SEV_FAIL), (
            "另一个包也声明了 demo_tool，居然没报：%r" % _codes(d))

        # ③ 注册表里只有无关的工具，过，且不再说没查
        import shutil
        shutil.rmtree(root / "rival")
        d = SD.examine(root / "demo", registered_names={"add_block"})
        assert d.ok and not d.skipped, (d.findings, d.skipped)
    print("  [ok] 重名：没注册表 -> 登记未查；自己不算；抢别的包 -> fail")


def test_star_import_is_reported_not_failed():
    """只认顶层 def 的版本会在这里报 54 条假阳。

    `from X import *` 让"声明的工具在不在"静态上不可判 ——
    正确答案是「没查」，不是「找不到」。报假阳等于告诉用户
    "office_parser 不兼容"，而它是框架里最大的那个 skill。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        src = "from .word_tools import *\n\n\ndef init(g=None):\n    pass\n"
        d = SD.examine(_make_skill(root, "star", handler=src))
        assert "handler_tool_not_found" not in _codes(d, SD.SEV_FAIL), (
            "star import 又报了假阳：%r" % _codes(d))
        assert SD.SKIP_STAR_IMPORT in d.skipped, (
            "star import 让一项变得不可判，却没登记：%r" % (d.skipped,))
    print("  [ok] star import -> 登记「没查」，不报假阳")


def test_named_import_counts_as_present():
    """负方向：具名 import 带进来的名字算数（`getattr` 拿得到）。

    没有这条，上一条可能被"把检查整个删掉"骗过去。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        src = ("from .impl import demo_tool\n\n\ndef init(g=None):\n    pass\n")
        d = SD.examine(_make_skill(root, "named", handler=src))
        assert "handler_tool_not_found" not in _codes(d, SD.SEV_FAIL), _codes(d)
        assert SD.SKIP_STAR_IMPORT not in d.skipped, d.skipped

        # 反面自证：同一个包里不 import 它就会 fail（证明上一条不是恒真）
        src2 = "from .impl import something_else\n\n\ndef init(g=None):\n    pass\n"
        d2 = SD.examine(_make_skill(root, "named2", handler=src2))
        assert "handler_tool_not_found" in _codes(d2, SD.SEV_FAIL), _codes(d2)
    print("  [ok] 具名 import 算在命名空间里；换个名字就报（非恒真）")


def test_warn_vs_fail_severities():
    """`draft` 之外的两条 warn：name 与目录名不一致、load_strategy 不认。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # 必须带上行尾、且 `count=1` —— 否则会连 `name: demo_tool` 一起
        # 替换掉（`str.replace` 默认全替换），于是工具名变了、报的是
        # `handler_tool_not_found`，而这条测试以为自己在测 warn。
        m = _GOOD_MANIFEST.replace("name: demo\n", "name: another_name\n", 1)
        d = SD.examine(_make_skill(root, "mismatch", manifest=m))
        assert "manifest_name_mismatch" in _codes(d, SD.SEV_WARN), _codes(d)
        assert d.ok, "warn 不该让它不通过"

        # 工具名要一并换掉 —— 不然它与上面那个包真的重名了
        #（`_sibling_declared_names` 会抓它），而这条测试想测的是 warn。
        m = (_GOOD_MANIFEST
             .replace("load_strategy: on_demand", "load_strategy: whenever")
             .replace("demo_tool", "weird_tool"))
        d = SD.examine(_make_skill(
            root, "weird", manifest=m,
            handler="def init(g=None):\n    pass\n\n\ndef weird_tool():\n    pass\n"))
        assert "load_strategy_unknown" in _codes(d, SD.SEV_WARN), _codes(d)
        assert d.ok, "warn 不该让它不通过：%r" % (d.findings,)
    print("  [ok] 两条 warn 只 warn 不 fail")


def test_output_kind_is_required_for_read_family_tools():
    """读家族工具没声明 `output_kind`，WARN。

    严重度是 WARN 不是 FAIL，而这是刻意的、有理由的：
      · 装载不拒（拒了等于用户已有的工具凭空消失，而且看不见）
      · 创建拒绝（`core/skill_factory.py`，fail-closed —— "能不能落地"那一层）
      · 体检报警（这里 —— 人看得见）
所以本条既要断言"报了"，也要断言"没到 FAIL"（`d.ok` 仍为真）。
    真正阻断那条在 `tests/test_skill_factory.py`。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        # ① 读家族（`get_`）+ 没声明，WARN，且仍然通过
        m = (_GOOD_MANIFEST
             .replace("demo_tool", "get_demo_thing"))
        d = SD.examine(_make_skill(
            root, "nokind", manifest=m,
            handler="def init(g=None):\n    pass\n\n\ndef get_demo_thing():\n    pass\n"))
        assert "tool_output_kind_missing" in _codes(d, SD.SEV_WARN), _codes(d)
        assert d.ok, "它是 warn，不该让包不通过：%r" % (d.findings,)

        # ② 声明了合法值，这条不许再报（否则它会变成一行永久噪声）
        m2 = m.replace("  - name: get_demo_thing",
                       "  - name: get_demo_thing\n    output_kind: structured")
        d2 = SD.examine(_make_skill(
            root, "withkind", manifest=m2,
            handler="def init(g=None):\n    pass\n\n\ndef get_demo_thing():\n    pass\n"))
        assert "tool_output_kind_missing" not in _codes(d2), _codes(d2)

        # ③ 声明了非法值，FAIL（拼错不许被当成"声明过了"）
        m3 = m.replace("  - name: get_demo_thing",
                       "  - name: get_demo_thing\n    output_kind: structred")
        d3 = SD.examine(_make_skill(
            root, "badkind", manifest=m3,
            handler="def init(g=None):\n    pass\n\n\ndef get_demo_thing():\n    pass\n"))
        assert "tool_output_kind_invalid" in _codes(d3, SD.SEV_FAIL), _codes(d3)

        # ④ 非读家族不要求 —— 否则这条规矩会变成全仓的税
        m4 = _GOOD_MANIFEST.replace("demo_tool", "weird_tool_name")
        d4 = SD.examine(_make_skill(
            root, "notread", manifest=m4,
            handler="def init(g=None):\n    pass\n\n\ndef weird_tool_name():\n    pass\n"))
        assert "tool_output_kind_missing" not in _codes(d4), _codes(d4)
    print("  [ok] 读家族缺声明=warn · 合法值不报 · 非法值=fail · 非读家族不要求")


def test_permission_is_required_for_every_tool():
    """每个工具都要声明权限档 —— 而严重度是 FAIL 不是 WARN。

    与上面那条 `output_kind` 故意不同，判据是猜错的代价方向：
      · 缺 `output_kind`，兜到 `prose`（fail-closed，方向安全），WARN 就够；
      · 缺 `permission`，落到前缀猜，而一个名字像 `get_` / `list_` / `parse_` 的
        写工具会被猜成 `read`，并发跑 + 只读子 agent 放行它，不会有任何东西红。
猜错方向是坏的那一侧，不能只报 WARN。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)

        # ① 缺，FAIL（而且要能拦住 `/skill approve`）
        m = _GOOD_MANIFEST.replace("    permission: read\n", "", 1)
        d = SD.examine(_make_skill(root, "noperm", manifest=m))
        assert "tool_permission_missing" in _codes(d, SD.SEV_FAIL), _codes(d)
        assert not d.ok, "缺 permission 没拦住这个包：%r" % (d.findings,)

        # ② 拼错（`Read`），FAIL —— 非法值不许被当成"声明过了"
        m2 = _GOOD_MANIFEST.replace("permission: read", "permission: Read", 1)
        d2 = SD.examine(_make_skill(root, "badperm", manifest=m2))
        assert "tool_permission_invalid" in _codes(d2, SD.SEV_FAIL), _codes(d2)

        # ③ 写对了，两条都不许再报（否则它会变成一行永久噪声）
        m3 = _GOOD_MANIFEST.replace("demo_tool", "weird_perm_tool")
        d3 = SD.examine(_make_skill(
            root, "okperm", manifest=m3,
            handler="def init(g=None):\n    pass\n\n\ndef weird_perm_tool():\n    pass\n"))
        assert "tool_permission_missing" not in _codes(d3), _codes(d3)
        assert "tool_permission_invalid" not in _codes(d3), _codes(d3)
    print("  [ok] permission：缺 FAIL · 拼错 FAIL · 合法静默")


def test_destructive_is_required_for_every_tool():
    """每个工具都要声明 `destructive`，与 `permission` 同形。

    FAIL 而不是 WARN —— 理由同 `permission` 那条但更硬：
    `permission` 的兜底是 `admin`（fail-closed）；`destructive` 的兜底是
    `false`（fail-open），没声明的工具会静默地不算不可逆，
    兜底闸连问都不会问。猜错的方向就是坏的那一侧。

    只收布尔（`isinstance(v, bool)`）：`"true"`（字符串）与 `1`（整数）都不算。
    `yes` 会被 PyYAML 读成 `True`（是布尔），它不算非法值 ——
    那半条不在这个名单里（是"碰巧对"，不是保证）。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)

        # ① 缺，FAIL
        m = _GOOD_MANIFEST.replace("    destructive: false\n", "", 1)
        d = SD.examine(_make_skill(root, "nodest", manifest=m))
        assert "tool_destructive_missing" in _codes(d, SD.SEV_FAIL), _codes(d)
        assert not d.ok, "缺 destructive 没拦住这个包：%r" % (d.findings,)

        # ② 非布尔，FAIL
        for i, bad in enumerate(("\"true\"", "1")):
            m2 = _GOOD_MANIFEST.replace("destructive: false",
                                        "destructive: %s" % bad, 1)
            d2 = SD.examine(_make_skill(root, "baddest%d" % i, manifest=m2))
            assert "tool_destructive_invalid" in _codes(d2, SD.SEV_FAIL), _codes(d2)

        # ③ 写对了，两条都不许再报（否则它会变成一行永久噪声）
        m3 = _GOOD_MANIFEST.replace("demo_tool", "weird_dest_tool")
        d3 = SD.examine(_make_skill(
            root, "okdest", manifest=m3,
            handler="def init(g=None):\n    pass\n\n\ndef weird_dest_tool():\n    pass\n"))
        assert "tool_destructive_missing" not in _codes(d3), _codes(d3)
        assert "tool_destructive_invalid" not in _codes(d3), _codes(d3)
    print("  [ok] destructive：缺 FAIL · 非布尔 FAIL · 合法静默")


# 5. 真实目录（含那条活标本）

def test_real_skills_directory_is_examined():
    """跑真的 `skills/`：所有包都必须通过，且声明过的工具不能从 manifest 里消失。

    一个声明了却没有实现的工具，注册时（`_LazyStub`）看不出来：
    模型搜得到、调得动，第一次调用才抛
    `AttributeError: Lazy-loaded tool '...' not found in ...`。
    体检器要在这里把这种包装成 fail。

    若某条断言是注定要翻的，写下那一刻就说清"什么时候该翻" ——
    否则下一个人看到红，第一反应是"是不是我改坏了"。
    """
    diags = {d.skill: d for d in SD.examine_all(_PROJECT_ROOT / "skills")}
    assert len(diags) >= 6, "只扫到 %d 个包 —— 路径或 discover 坏了" % len(diags)
    for name in ("office_parser", "file_parser", "mail_parser", "entertainment"):
        assert name in diags, "少扫了 %s" % name

    # 判据：一条注定要翻的断言，要在写下的那一刻就说清"什么时候该翻" ——
    # 否则下一个人看到红，第一反应是"是不是我改坏了"。
    # 这里已经翻到"当前版本"：真实目录应当全部通过。
    bad = {n: _codes(d, SD.SEV_FAIL) for n, d in diags.items() if not d.ok}
    assert not bad, (
        "有包不通过：%r\n"
        "=> 若是**新加**的包，那是这条护栏在工作；\n"
        "=> 若是**老包**新坏，先查是不是文件被误删"
        "（`convert_to_pdf` 当年就是这么丢的）" % (bad,))
    assert "convert_to_pdf" in diags["office_parser"].tools, (
        "manifest 里那个声明没了 —— 那才是真丢东西")
    print("  [ok] 真实目录：%d 个包全部通过（convert_to_pdf 已补回）" % len(diags))


def test_fingerprint_is_cheap_and_changes_with_the_package():
    """`fingerprint` 是"变了才体检"的判据 —— 它必须只 stat、不读内容。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        d = _make_skill(root)
        fp1 = SD.fingerprint(root)
        assert "demo" in fp1, fp1

        fp2 = SD.fingerprint(root)
        assert fp1 == fp2, "什么都没改，指纹却变了：%r" % (fp1, fp2)

        (d / "handler.py").write_text(_GOOD_HANDLER + "\n\ndef extra():\n    pass\n",
                                      encoding="utf-8")
        fp3 = SD.fingerprint(root)
        assert fp3["demo"] != fp1["demo"], (
            "改了 handler.py，指纹没变 —— 「变了才体检」会永远不触发")
    print("  [ok] 指纹：没改就不变、改了就跟上")


def test_watch_only_examines_what_changed():
    """`watch` 是每轮调的，它的代价必须真的是「只处理变化」。

    判据：第二次调用必须是空的（什么都没改）。没有这条，
    "每轮全量体检"也能让别的断言全绿，而代价是 69 个工具 × 每轮。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        SD.reset_watch()

        assert SD.watch(root) == [], "空目录不该报任何东西"

        _make_skill(root, "broken", manifest="kind: skill\n")   # 没有 tools
        first = SD.watch(root)
        assert [d.skill for d in first] == ["broken"], first
        assert SD.watch(root) == [], "什么都没改，第二次却还在报（= 每次都在全量体检）"

        _make_skill(root, "broken")                              # 覆盖成好包
        assert SD.watch(root) == [], "改好之后还在报"

        # 包消失必须出声 —— 「skill 没了」与「skill 没问题」长得一模一样，
        # 而 `convert_to_pdf` 就是那么丢的（文件没了，而没有任何东西在看）。
        import shutil
        shutil.rmtree(root / "broken")
        gone = SD.watch(root)
        assert [d.skill for d in gone] == ["broken"], gone
        assert "skill_disappeared" in _codes(gone[0]), _codes(gone[0])
        assert SD.watch(root) == [], "消失只该报一次"
        SD.reset_watch()
    print("  [ok] watch：只处理变化；没改 -> 空；包消失**出声**（只报一次）")


def test_a_package_does_not_collide_with_itself():
    """体检一个已经生效的 skill 时，它自己的工具就在注册表里 ——
    不排除的话每一个工具都会与自己重名（`office_parser` 会报 69 条假阳）。

这条判据必须做在体检器内部：靠调用方传 `own_names`
    会把「体检器自己的事」推给三个调用点，漏一处就 69 条假阳。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        _make_skill(root)
        # 注册表里只有它自己的工具 —— 这正是"体检一个生效的包"的样子
        d = SD.examine(root / "demo", registered_names={"demo_tool"})
        assert "tool_name_collision" not in _codes(d, SD.SEV_FAIL), (
            "包与自己撞名了：%r" % _codes(d))
        assert d.ok, d.findings

        # 反面自证：另一个包抢同一个名字必须报（证明上面那条不是恒真）
        m2 = _GOOD_MANIFEST.replace("name: demo\n", "name: thief\n", 1)
        d3 = SD.examine(_make_skill(root, "thief", manifest=m2),
                        registered_names={"demo_tool"})
        assert "tool_name_collision" in _codes(d3, SD.SEV_FAIL), (
            "★ 另一个包声明了同一个工具名却没报 —— 上面那条就成了恒真：%r"
            % _codes(d3))
    print("  [ok] 包不与**自己**撞名；另一个包抢同一个名字照报（非恒真）")


# 6. 负对照：拆掉机制必须变红

def test_negative_control_substring_check_would_cry_wolf():
    """负对照：退回"只看 def"，`office_parser` 立刻出现一堆假阳。

    只认 def 的版本会这么错（54 条）。这条把它钉住：判据必须是"命名空间里有没有"。
    """
    tree = ast.parse(_MODULE_SRC)
    fn = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_module_names":
            fn = node
    assert fn is not None, "setup: 找不到 `_module_names`（改名了？）"

    # 模拟"只认 def"的版本
    def naive(source):
        t = ast.parse(source)
        return {n.name for n in t.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    src = "from .impl import demo_tool\n\n\ndef init(g=None):\n    pass\n"
    assert "demo_tool" not in naive(src), (
        "setup 错了：naive 版居然认得出来，那这条负对照测不到东西")
    assert "demo_tool" in SD._module_names(ast.parse(src))[0], (
        "★ 修好的版本认不出 import 带进来的名字 —— 假阳又回来了")
    print("  [ok] 负对照成立：只认 def 的版本会漏 import，修好的版本不会")


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
    print("Results: %d passed, %d failed" % (len(tests) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
