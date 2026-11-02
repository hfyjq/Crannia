"""文件原语五件套 —— 机械读者。

    read_file · write_file · edit_file · glob · grep

## 这个文件钉住的六条不变量

1. 行尾（本仓特有）：树是混排的（`core/agent_loop.py` 是 CRLF、
   `core/todos.py` 是 LF），而 `core.autocrlf` 会在 checkout 时把 LF 变成 CRLF。
照着 CRLF 文件抄出来的 `old_string` 如果是 `\\n`，逐字节匹配会 0 命中。
   判据是「规范化之后仍然精确」＋「写回只 splice 那一处，别处的行尾一个字节不动」。
2. 写前必读（CAS）：没读过 / 读过之后被外部改过，拒；新建不要求；
   写成功之后 CAS 要写回（否则"写完紧接着再改一次"会被自己拒掉）。
3. edit 的三种拒绝：0 命中 / >1 命中（列出行号）/ 跨度超限 ——
   三种都拒，且文件一个字节都不动。`replace_all=true` 才允许改全部。
4. 只读子 agent 是"执行层拒"，不是"摘 `tools=`"：在两个派发口上拒、
   永不挂起、且父那层照常能跑（正对照）。
5. 循环闸的逐工具覆盖：`read_file` 的 64k 不被档表那 6000 切掉，
   而别的工具照旧被切（负对照）。
6. 跳过规则只有一处定义：`_wip_` / `.git` / `$x` 在索引与新 walker 里
   都被跳过（一条断言两处）—— 抄第二份的代价是两份会漂移。

外加四条结构读者：隔离名单（按注册表反查）、权限档、常驻集、两条路锁步（AST）。

会话号一律经 `_session()` —— 五个 handler 都没有 session_id 形参
（签名是 `handler(tool_args)`），会话只能从请求快照**上取。

全程 `sandbox()`（测试不许碰生产目录）。`agent_loop` 的制品目录
`_TOOL_RESULTS_DIR` 是相对路径，所以 chdir 一进去它就落在 tmp 里。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_file_tools.py
"""

import contextlib
import sys
import tempfile
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import file_tools as FT  # noqa: E402

SID = "filetools:private:probe"

#: 一行 `%6d\t` 前缀 + 内容 —— 断言行号时用得上
_LINE_PREFIX = "     1\t"


# 夹具

@contextlib.contextmanager
def _session(session_id=SID):
    """把"当前请求"设成 *session_id* —— 读态表的会话号只能从这里来。"""
    from core import request_snapshot

    request_snapshot.set_current(request_snapshot.capture(
        [], [], "", model="probe", session_id=session_id))
    try:
        yield
    finally:
        request_snapshot.clear_current()


@contextlib.contextmanager
def _lab():
    """一个隔离的沙箱 + 一套真的 `SecurityGuard` / `WorkspaceManager`。

    三个区都在 tmp 里，不碰任何真实路径：

        <tmp>/workspace/   READ_WRITE
        <tmp>/fakehome/    READ_ONLY（把 `user_home` 指过去 —— 比用真的家目录
                           快得多，而且"如果拒绝逻辑坏了就会去走真的家目录"
                           这种最坏情况根本不会发生）
        <tmp>/outside.txt  READ_ONLY（不在工作区、也不在 fakehome 下）
                           兜底是 READ_ONLY（见
                           `core/security.py::DEFAULT_FORBIDDEN_PATHS` 的注释）。
                           写仍然拒，所以下面那两条断言一个字没动。

    顺序：先 `chdir`、后构造 guard（`SecurityGuard` 在构造时就 `resolve()`
    —— `sandbox()` 已经保证了这个顺序）。
    """
    from _sandbox import sandbox

    with sandbox() as tmp:
        (tmp / "fakehome").mkdir(exist_ok=True)
        (tmp / "outside.txt").write_text("OUTSIDE-SECRET\n", encoding="utf-8")

        from bus.registry import register_workspace_manager, get_workspace_manager
        from core.security import SecurityGuard
        from core.workspace import WorkspaceManager

        guard = SecurityGuard(
            allowed_dirs=["./workspace"],
            command_blacklist=[],
            audit_log_path=str(tmp / "audit_ft.log"),
            user_home=str(tmp / "fakehome"),
        )
        wm = WorkspaceManager(guard, {})
        old = get_workspace_manager()
        register_workspace_manager(wm)
        FT.forget_reads(SID)
        try:
            yield tmp, guard, wm
        finally:
            register_workspace_manager(old)
            FT.forget_reads(SID)


def _ws(tmp) -> Path:
    return tmp / "workspace"


def _boot_measure():
    """装载完整的注册表（不抄第二份装载 —— 装载只有一处）。

    必须在 sandbox 外面调：`boot_registry` 是从 `Path("skills")` 现扫的，
    而 `sandbox()` 已经把 cwd 换到 tmp 了，在里面调会一个技能都找不到。

    也别依赖"别的测试先跑过它" —— 测试是按名字字母序跑的，
    依赖顺序的夹具正是最难查的那种红。
    """
    if str(_PROJECT_ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
    from measure_request import boot_registry

    return boot_registry()


def _text(path: Path) -> str:
    """读回文本，行尾归一。

    必须归一：`Path.write_text()` 在 Windows 上走文本模式，夹具里写的
    `"hello\\n"` 落盘是 `b"hello\\r\\n"`。断言行尾本身的地方用 `read_bytes()`
    （本文件里 `test_only_the_matched_span_is_spliced` 就是那样做的）——
    这个函数管的是内容。
    """
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


# 1. 写前必读（CAS）

def test_write_requires_a_prior_read():
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "a.txt"
        f.write_text("hello\n", encoding="utf-8")

        with _session():
            r = FT.write_file(str(f), "new\n")
            assert r.get("success") is False, r
            assert "读过" in r.get("error", ""), r
            assert "read_file" in (r.get("hint") or ""), (
                "拒绝没带路线 —— 模型只会换个写法再试一遍：%r" % (r,))
            assert _text(f) == "hello\n", "被拒的写**动了文件**"

            # 读完再写 -> 过
            FT.read_file(str(f))
            r = FT.write_file(str(f), "new\n")
            assert r.get("success") is True, r
            assert _text(f) == "new\n", r

        # 换个会话 -> 又变成"没读过"（读态是按会话键的）
        with _session("somebody-else"):
            r = FT.edit_file(str(f), "new", "x")
            assert r.get("success") is False, (
                "另一个会话没读过就能改 —— 读态没按会话隔离：%r" % (r,))
    print("  [ok] 没读过就写/改 -> 拒 + 带路线；读完再写 -> 过；别的会话不受益")


def test_svg_is_read_as_text_and_unlocks_the_write_guard():
    """SVG 按文本读，不是图片。

    `ppt_svg` 把每页写进 `svg_sessions/<doc>/slide_*.svg`；若 `read_file`
    把它当图片，「只收 PNG/JPEG/WebP/GIF」，`write_file` 的「写前必读」
    也过不去，模型只能写临时文件 + `run_command mv` 绕道。

    `looks_like_image` 认出 `image/svg+xml` 时不走图片管线，
    落到文本路径（于是也 `_note_read` 记账）。
    """
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "slide_1.svg"
        f.write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<svg xmlns="http://www.w3.org/2000/svg" width="10" '
                     'height="10"><rect width="10" height="10"/></svg>\n',
                     encoding="utf-8")

        with _session():
            # 负：没读过时写仍然被拒（闸没坏）
            r0 = FT.write_file(str(f), "<svg/>\n")
            assert r0.get("success") is False, r0
            assert "读过" in r0.get("error", ""), r0

            # 正：读得回文本（带行号），不是图片块、不是错误串
            out = FT.read_file(str(f))
            assert isinstance(out, str), ("SVG 被当成图片了：%r" % (out,))
            assert "<svg" in out and "rect" in out, out[:200]
            assert "只收" not in out and "图片" not in out, out[:200]

            # 读完之后写 -> 过（`_note_read` 真的记了账）
            r = FT.write_file(
                str(f), '<svg xmlns="http://www.w3.org/2000/svg">'
                        '<circle r="1"/></svg>\n')
            assert r.get("success") is True, r
            assert "circle" in _text(f), _text(f)
    print("  [ok] SVG 按文本读；读之前写仍拒；读完再写通过")


def test_write_backs_the_new_stat_so_the_next_edit_passes():
    """CAS 必须写回：否则"写完紧接着再改一次"会被自己的规则拒掉。

    写入的字节数必须和原来不同（这里 4 → 11）。CAS 的键是
    `(mtime_ns, size)`，而 Windows 的墙钟粒度约 15 ms —— 同一毫秒里两次写
    极可能拿到逐纳秒相同的 mtime。只靠 mtime 的话，这条测试在
    "写回没做"时会假绿（负对照实测：4 → 5 字节时变异不会红）。
    """
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "b.txt"
        f.write_text("one\n", encoding="utf-8")            # 4 字节（+CRLF）
        with _session():
            FT.read_file(str(f))
            assert FT.write_file(str(f), "two-longer\n").get("success") is True
            r = FT.edit_file(str(f), "two-longer", "three")
            assert r.get("success") is True, (
                "写完紧接着 edit 被拒了 —— 新 stat 没记回去：%r" % (r,))
            assert _text(f) == "three\n", r
    print("  [ok] 写成功之后 CAS 写回了新 stat（紧接着的 edit 不被自己拒）")


def test_external_change_after_read_is_refused():
    """工作区里挂着 watcher（微信目录那类会往里面同步文件），外部改动是真实的。"""
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "c.txt"
        f.write_text("v1\n", encoding="utf-8")
        with _session():
            FT.read_file(str(f))
            time.sleep(0.02)
            f.write_text("v2-changed-outside\n", encoding="utf-8")   # 大小也变了
            r = FT.edit_file(str(f), "v2", "v3")
            assert r.get("success") is False, r
            assert "改过" in r.get("error", ""), r
            assert _text(f) == "v2-changed-outside\n", "被拒的改**动了文件**"
    print("  [ok] 读完之后被外部改过 -> 拒（且文件没被动）")


def test_new_file_does_not_require_a_prior_read():
    with _lab() as (tmp, _g, _w):
        with _session():
            r = FT.write_file(str(_ws(tmp) / "brand_new.txt"), "x\n")
            assert r.get("success") is True, r
            assert r.get("created") is True, r
    print("  [ok] 新建文件不要求先读（没得读）")


# 2. 行尾（本仓特有）

def test_crlf_file_accepts_an_lf_old_string():
    """本仓的树是混排的，而模型抄来的 `old_string` 大概率是 `\\n`。

    不做规范化的话，每一次 edit 都会 0 命中，而症状看起来像"模型抄错了原文"。
    """
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "crlf.txt"
        f.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

        with _session():
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "beta\ngamma", "BETA\nGAMMA")
            assert r.get("success") is True, (
                "LF 的 old_string 在 CRLF 文件里没匹配上 —— 规范化没生效：%r" % (r,))
            body = f.read_bytes()
            assert body == b"alpha\r\nBETA\r\nGAMMA\r\n", body
    print("  [ok] LF 的 old_string 能改 CRLF 文件（规范化后仍是精确匹配）")


def test_only_the_matched_span_is_spliced():
    """写回只 splice 命中那一段 —— 混排文件不会被顺手洗成一套行尾。"""
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "mixed.txt"
        # 前两行 CRLF、最后一行 LF（真实世界里 git 转换到一半就是这个样子）
        f.write_bytes(b"one\r\ntwo\r\nthree\n")

        with _session():
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "two", "TWO")
            assert r.get("success") is True, r
            body = f.read_bytes()
            assert body == b"one\r\nTWO\r\nthree\n", (
                "splice 之外的行尾被改掉了（整篇重写的形状）：%r" % (body,))
    print("  [ok] 只替换命中那一段，别处的行尾一个字节不动")


def test_write_file_keeps_the_targets_line_endings():
    """覆盖已有文件时沿用它的行尾；新建文件原样写（那是调用方的选择）。"""
    with _lab() as (tmp, _g, _w):
        crlf = _ws(tmp) / "keep_crlf.txt"
        crlf.write_bytes(b"a\r\nb\r\n")
        with _session():
            FT.read_file(str(crlf))
            FT.write_file(str(crlf), "x\ny\n")
            assert crlf.read_bytes() == b"x\r\ny\r\n", (
                "覆盖 CRLF 文件之后变成了 LF：%r" % (crlf.read_bytes(),))

            fresh = _ws(tmp) / "fresh_lf.txt"
            FT.write_file(str(fresh), "x\ny\n")
            assert fresh.read_bytes() == b"x\ny\n", "新建文件被改了口径"
    print("  [ok] 覆盖沿用原行尾；新建原样写")


def test_mixed_file_fallback_says_so_out_loud():
    """混排文件里那一处用的是另一种行尾，允许命中，但要说出来。

    静默换口径就是另一种撒谎（同族：`None != 0` —— 两种结果不许同形）。
    """
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "mixed2.txt"
        # 主行尾 CRLF（两行），但被改的那一段是 LF
        f.write_bytes(b"h1\r\nh2\r\nTARGET\nTAIL\nTAIL2\r\n")

        with _session():
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "TARGET\nTAIL", "TARGET\nTAIL-EDITED")
            assert r.get("success") is True, r
            assert "note" in r and "行尾" in r["note"], (
                "换了口径却没说 —— 静默降级：%r" % (r,))
            assert r["note"].count("LF") >= 1, r
    print("  [ok] 混排兜底命中时会出声（换口径不静默）")


# 3. edit 的三种拒绝

def test_edit_refuses_zero_hit_two_hits_and_oversized_span():
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "d.txt"
        f.write_text("dup\ndup\n", encoding="utf-8")
        before = f.read_bytes()

        with _session():
            FT.read_file(str(f))

            r = FT.edit_file(str(f), "nope-nope", "x")
            assert r.get("success") is False, r
            assert "找不到" in r.get("error", ""), r

            r = FT.edit_file(str(f), "dup", "x")
            assert r.get("success") is False, r
            assert "2 次" in r.get("error", ""), r
            assert "行 1" in r.get("error", "") and "行 2" in r.get("error", ""), (
                ">1 命中时没列出行号 —— 模型没法知道该带多少上下文：%r" % (r,))
            assert "replace_all" in (r.get("hint") or ""), r

            r = FT.edit_file(str(f), "x" * (FT._MAX_EDIT_SPAN + 1), "y")
            assert r.get("success") is False, r
            assert "write_file" in (r.get("hint") or ""), (
                "跨度超限没给「改用 write_file」这条路：%r" % (r,))

            assert f.read_bytes() == before, "三种拒绝里有一种**动了文件**"

            # replace_all 是留口：传了才改全部
            r = FT.edit_file(str(f), "dup", "x", replace_all=True)
            assert r.get("success") is True, r
            assert r.get("replaced") == 2, r
            assert _text(f) == "x\nx\n", r
    print("  [ok] 0 命中 / 2 命中（列行号）/ 跨度超限 三种都拒且文件没动；"
          "replace_all 才改全部")


def test_edit_matches_quote_variants_but_not_whitespace():
    """引号排版变体：逐字节 0 命中后再试一次；空白/缩进仍然不容错。"""
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "q.txt"
        f.write_text("他说“你好”就走了\n", encoding="utf-8")

        with _session():
            FT.read_file(str(f))

            r = FT.edit_file(str(f), '他说"你好"就走了', "他说“再见”就走了")
            assert r.get("success") is True, r
            assert r.get("replaced") == 1, r
            assert "引号" in (r.get("note") or ""), (
                "走了引号归一却没说出来 —— 静默换口径：%r" % (r,))
            assert _text(f) == "他说“再见”就走了\n", repr(_text(f))

            # 反方向：文件是直引号、模型给弯引号
            f.write_text('name = "x"\n', encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "name = “x”", 'name = "y"')
            assert r.get("success") is True, r
            assert _text(f) == 'name = "y"\n', repr(_text(f))

            # 全角引号也算同一种排版
            f.write_text("标题＇甲＇完\n", encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "标题'甲'完", "标题'乙'完")
            assert r.get("success") is True, r
            assert _text(f) == "标题'乙'完\n", repr(_text(f))

            # 文件里有两处同形，仍然拒（消歧不能被"归一"顺手绕过）
            f.write_text("“a”\n“a”\n", encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), '"a"', '"b"')
            assert r.get("success") is False, r
            assert "候选" in r.get("error", ""), r

            # 空白差异仍然 0 命中
            f.write_text("a  b\n", encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "a b", "a c")
            assert r.get("success") is False, r
            assert "找不到" in r.get("error", ""), r
    print("  [ok] 引号排版变体归一（两个方向 + 全角）；仍要求唯一；空白不容错")


def test_edit_strips_rendered_prefixes_and_visible_escapes():
    """行号前缀与字面转义：模型抄的是渲染结果，不是文件本身。"""
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "rendered.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")

        with _session():
            FT.read_file(str(f))
            # 连 read_file 的行号一起抄了
            r = FT.edit_file(str(f), "     1\talpha\n     2\tbeta",
                             "     1\tgamma\n     2\tbeta")
            assert r.get("success") is True, r
            assert _text(f) == "gamma\nbeta\n", repr(_text(f))
            assert "行号前缀" in (r.get("note") or ""), r

            # 字面量 `\n` 当成了两个字符
            f.write_text("one\ntwo\n", encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "one\\ntwo", "three")
            assert r.get("success") is True, r
            assert _text(f) == "three\n", repr(_text(f))
            assert "转义" in (r.get("note") or ""), r

            # 仍然要求唯一
            f.write_text("x\ny\nx\ny\n", encoding="utf-8")
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "     1\tx\n     2\ty",
                             "     1\tz\n     2\ty")
            assert r.get("success") is False, r
            assert "候选" in r.get("error", ""), r
    print("  [ok] 行号前缀 / 字面转义都能匹配（且仍要求唯一）")


def test_edit_refuses_identical_old_and_new():
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "e.txt"
        f.write_text("same\n", encoding="utf-8")
        with _session():
            FT.read_file(str(f))
            r = FT.edit_file(str(f), "same", "same")
            assert r.get("success") is False, r
    print("  [ok] old == new 直接拒（那是一次什么都不改的编辑）")


# 4. 路径纪律

def test_directory_as_file_is_reported_clearly():
    with _lab() as (tmp, _g, _w):
        with _session():
            out = FT.read_file(str(_ws(tmp)))
            assert "目录" in out, out
            assert "glob" in out, "拒绝没给路线：%r" % out
            r = FT.write_file(str(_ws(tmp)), "x")
            assert r.get("success") is False and "目录" in r.get("error", ""), r
    print("  [ok] 把目录当文件读/写 -> 明说是目录 + 给路线")


def test_escape_and_readonly_are_refused_but_the_workspace_is_not():
    """`..` 逃逸与只读区都拒；正对照：工作区里同一件事跑得起来。"""
    with _lab() as (tmp, _g, _w):
        with _session():
            # FORBIDDEN（不在工作区、也不在 fakehome 下）
            r = FT.write_file(str(tmp / "outside.txt"), "pwned\n")
            assert r.get("success") is False, (
                "写到工作区外面去了：%r" % (r,))
            assert _text(tmp / "outside.txt") == "OUTSIDE-SECRET\n"

            # 相对逃逸
            r = FT.write_file("../outside.txt", "pwned\n")
            assert r.get("success") is False, r
            assert _text(tmp / "outside.txt") == "OUTSIDE-SECRET\n"

            # 正对照：工作区里同一件事必须跑得起来（证明不是恒拒）
            r = FT.write_file(str(_ws(tmp) / "inside.txt"), "ok\n")
            assert r.get("success") is True, r

            # 只读区（fakehome）里的文件能读，不能写
            ro = tmp / "fakehome" / "note.txt"
            ro.write_text("readable\n", encoding="utf-8")
            out = FT.read_file(str(ro))
            assert "readable" in out, (
                "只读区里的**具体文件**应该能读（纪律 ⑤ 只封扫描）：%r" % out)
            r = FT.write_file(str(ro), "x\n")
            assert r.get("success") is False, r
            assert _text(ro) == "readable\n"
    print("  [ok] 逃逸/只读写 都拒；工作区正对照跑得起来；只读区的单文件可读")


def test_scan_root_refuses_readonly_and_forbidden():
    """纪律 ⑤：扫描的根只许 READ_WRITE —— 一个模式扫描是批量读。"""
    with _lab() as (tmp, _g, _w):
        with _session():
            r = FT.glob("*", root=str(tmp / "fakehome"))
            assert r.get("success") is False, (
                "只读区被扫描了 —— 跨区审批被绕开：%r" % (r,))
            assert "request_cross_zone_search" in (r.get("hint") or ""), (
                "拒绝没指向跨区那条路：%r" % (r,))

            r = FT.glob("*", root=str(_PROJECT_ROOT.parent))
            assert r.get("success") is False, r

            # 靶子必须是"真的禁止区"。`_PROJECT_ROOT.parent` 现在是只读区
            # （兜底是 READ_ONLY）。而规避清单是相对 cwd 解析的、`_lab()`
            # 已经 chdir 进 tmp，在沙箱里造一个。
            forbidden_root = tmp / "logs"      # `DEFAULT_FORBIDDEN_PATHS` 里的一条
            forbidden_root.mkdir(exist_ok=True)
            assert "禁止" in FT.grep("x", root=str(forbidden_root)), (
                "grep 的禁止区没报清楚")

            # 正对照：工作区里同一个模式必须扫得动
            (_ws(tmp) / "hit.txt").write_text("needle-here\n", encoding="utf-8")
            r = FT.glob("*.txt")
            assert r.get("success") is True and r["count"] >= 1, r
            assert "needle-here" in FT.grep("needle-here"), "正对照的 grep 没搜到"
    print("  [ok] 只读区/禁止区当根 -> 拒 + 带路线；工作区正对照扫得动")


# 5. 扫描语义（glob / grep）

def test_glob_pattern_semantics():
    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        (ws / "sub").mkdir(exist_ok=True)
        (ws / "top.py").write_text("x", encoding="utf-8")
        (ws / "sub" / "deep.py").write_text("x", encoding="utf-8")
        (ws / "sub" / "note.md").write_text("x", encoding="utf-8")

        with _session():
            r = FT.glob("*.py")
            names = [p.rsplit("/", 1)[-1] for p in r["files"]]
            assert names == ["deep.py", "top.py"], (
                "没有斜杠的模式应当匹配**任意层级**的文件名（一条规则）：%r" % r)

            r = FT.glob("sub/*.py")
            assert [p.rsplit("/", 1)[-1] for p in r["files"]] == ["deep.py"], (
                "有斜杠的模式应当**锚定**在相对路径上：%r" % r)

            r = FT.glob("**/*.md")
            assert [p.rsplit("/", 1)[-1] for p in r["files"]] == ["note.md"], r

            r = FT.glob("sub/**")
            assert r["count"] == 2, r
    print("  [ok] glob：无斜杠=按文件名（任意层级）/ 有斜杠=锚定相对路径 / `**`")


def test_grep_says_the_count_and_skips_binary():
    """`grep` 走随包的 ripgrep（`core/rg.py`）之后的三条判据。

    行为变化如实记：旧实现自己嗅 NUL、会在回执里写「跳过了 N 个二进制文件」；
      换成 rg 之后它不报这个数（`--stats` 只有"搜了多少"），所以那句没有了。
这里钉的是新的真话：二进制文件里的内容不会出现在结果里，
        而回执不许声称跳过了什么它数不出来的东西。
    """
    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        (ws / "many.txt").write_text(
            "\n".join("hit %d" % i for i in range(50)) + "\n", encoding="utf-8")
        (ws / "bin.dat").write_bytes(b"hit\x00hit\x00")

        with _session():
            out = FT.grep("hit", limit=5)
            assert "limit=5" in out, "超限没说条数（四家一致的做法）：%r" % out[-200:]
            assert "bin.dat" not in out, (
                "二进制文件里的命中漏进结果了：%r" % out[-200:])
            assert "跳过了" not in out, (
                "回执声称跳过了多少 —— rg 不报那个数，这就是假话：%r" % out[-200:])

            out = FT.grep("definitely-not-there")
            assert "没有匹配" in out, out

            out = FT.grep("(unclosed")
            assert "正则" in out, out
    print("  [ok] grep：超限说条数 / 二进制不进结果 / 零命中 / 坏正则明说")


def test_an_ancestor_gitignore_does_not_hide_the_workspace():
    """祖先目录的 `.gitignore` 不算数 —— 工作区对我们自己必须是可见的。

    现场反馈：`workspace/改动测试/big.txt` 里明明有 1200 处，`grep` 却回
    「没有匹配」。根因是仓根那份 `.gitignore` 写着 `workspace/*`
    （那句的本意是"别把运行数据提交进 git"），而 rg 默认会一路往上读，
    把它当成了搜索排除规则。
    """
    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        (tmp / ".gitignore").write_text("workspace/*\n", encoding="utf-8")
        sub = ws / "sub"
        sub.mkdir()
        (sub / "a.txt").write_text("needle-alpha\n", encoding="utf-8")
        (ws / "b.txt").write_text("needle-beta\n", encoding="utf-8")

        with _session():
            out = FT.grep("needle-alpha")
            assert "a.txt" in out, "工作区被祖先的 .gitignore 藏起来了：%r" % out[:200]
            #: 搜索根以内的 `.gitignore` 照旧算数（那是用户自己写的规则）。
            (ws / ".gitignore").write_text("b.txt\n", encoding="utf-8")
            out2 = FT.grep("needle-beta")
            assert "没有匹配" in out2, "搜索根以内的 .gitignore 不该被一起关掉：%r" % out2[:200]
    print("  [ok] 祖先的 .gitignore 不挡工作区，树内的照旧算数")


def test_grep_output_modes():
    """`output_mode` 三种形状：content（默认）· files_with_matches · count。

    两半都要断言：默认逐字不变（老调用一个字节都不该变）+ 另外两种真的换形状，
    且 `limit` 在那两种下数的是文件（不是行）。`context` 与它们互斥 ——
    拒 + 带路线，不静默丢。
    """
    def _rows(out):
        #: 回执里的路径是绝对路径（`_posix(p)`）—— 这里只比 basename，
        #:   免得断言被临时目录名绑住（同 `test_grep_says_the_count…` 的写法）。
        return [r.rsplit("/", 1)[-1] for r in out.split("\n")
                if r and r[0] not in "（·"]

    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        (ws / "a.py").write_text("hit one\nx\nhit two\nhit three\n", encoding="utf-8")
        (ws / "b.py").write_text("hit four\n", encoding="utf-8")
        (ws / "c.txt").write_text("nothing\n", encoding="utf-8")

        with _session():
            # ① 默认 = content，逐字老形状
            base = FT.grep("hit")
            assert "a.py:1:hit one" in base, base
            assert "2 个文件命中 4 行" in base, base

            # ② 只列文件：没有内容、没有行号
            only = FT.grep("hit", output_mode="files_with_matches")
            assert sorted(_rows(only)) == ["a.py", "b.py"], _rows(only)
            assert "2 个文件命中" in only and "处命中" not in only, only

            # ③ 计数：每行 `路径:N`，N 是该文件的全部命中
            counted = FT.grep("hit", output_mode="count")
            assert sorted(_rows(counted)) == ["a.py:3", "b.py:1"], _rows(counted)
            assert "2 个文件，共 4 处命中" in counted, counted

            # ④ `limit` 在非 content 下数的是文件；计数本身不被截
            #: 用 `glob` 钉死只看 a.py —— 否则"列出的那一个文件"取决于走树顺序，
            #:   下面那条"计数没被截"的断言之靶就飘了（变异验证时踩到过）。
            one = FT.grep("hit", output_mode="count", limit=1, glob="a.py")
            assert len(_rows(one)) == 1, _rows(one)
            assert _rows(one)[0].endswith(":3"), "计数被 limit 截掉了：%r" % _rows(one)
            assert "只列到 limit=1 个文件" in one, one

            # ⑤ `context` 与它们互斥，拒 + 路线
            bad = FT.grep("hit", output_mode="files_with_matches", context=2)
            assert "context" in bad and "content" in bad, bad

            # ⑥ 负对照：content + context 仍然工作（证明 ⑤ 拒的是组合）
            good = FT.grep("hit", context=1)
            assert "a.py-2-x" in good, good

            # ⑦ 坏值当场拒，并把三个选项报出来
            bad2 = FT.grep("hit", output_mode="files")
            assert "files_with_matches" in bad2 and "count" in bad2, bad2
    print("  [ok] grep output_mode：默认不变 / 只列文件 / 计数 / limit 数文件 /"
          " context 拒 / 坏值拒")


def test_symlinks_are_not_followed():
    """纪律 ④：不跟符号链接。根是符号链接时按目标分类，不从根上逃逸。"""
    import os

    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        target = tmp / "fakehome"           # READ_ONLY
        link = ws / "link_to_ro"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            print("  [skip] 这个环境造不出符号链接（Windows 需要开发者模式）")
            return

        with _session():
            r = FT.glob("**/*.txt", root=str(ws))
            assert r.get("success") is True, r
            assert not any("fakehome" in p or "link_to_ro" in p for p in r["files"]), (
                "顺着符号链接走出了工作区：%r" % (r["files"],))
    print("  [ok] 符号链接不被跟随（walker 里 is_dir/is_file 都不 follow）")


# 6. 跳过规则：一处定义，两处读者

def test_shared_skip_rule_one_definition_two_readers():
    """索引与新 walker 共用 `skip_walk_entry` —— 一条断言两处。

    抄第二份的代价不是多写五行，是两份会漂移：索引里看不见的文件 `grep`
    却搜得到（或反过来），而两个答案看着都对。
    """
    from core.workspace import PathIndex, skip_walk_entry

    names = (".git", "$RECYCLE.BIN", "_wip_deadbeef.docx", "_foo.docx",
             "normal.docx", ".gitignore")
    # `.gitignore` 也在跳过之列（`.` 前缀那一条）—— 这是共用的代价，
    # 也是它的意义：索引看不见的东西，`glob` 也看不见。两者一致才有意义。
    # （要看它用显式路径照样读得到：`read_file(".gitignore")` 不受这条管辖，
    #   那是 `SecurityGuard` 的活。）
    want_skipped = {".git", "$RECYCLE.BIN", "_wip_deadbeef.docx", ".gitignore"}

    got = {n for n in names if skip_walk_entry(n)}
    assert got == want_skipped, (got, want_skipped)

    # ② 索引那一侧：真的建一份索引，看谁没进来
    with _lab() as (tmp, _g, _w):
        ws = _ws(tmp)
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (ws / "$RECYCLE.BIN").mkdir()
        (ws / "$RECYCLE.BIN" / "junk").write_text("j", encoding="utf-8")
        (ws / "_wip_deadbeef.docx").write_text("w", encoding="utf-8")
        (ws / "_foo.docx").write_text("u", encoding="utf-8")
        (ws / "normal.txt").write_text("n", encoding="utf-8")

        idx = PathIndex()
        idx.build([ws], [])
        indexed = {e.name for e in idx.search("*", max_results=100)}
        assert "_foo.docx" in indexed, (
            "负对照失败：下划线开头的**用户**文件不该被隐藏")
        assert "normal.txt" in indexed, indexed
        assert "_wip_deadbeef.docx" not in indexed, indexed
        assert len([k for k in idx._index if ".git" in k]) == 0, (
            "索引把 .git 收进去了")

        # ③ 新 walker 那一侧：同一批名字，同样的结论
        with _session():
            r = FT.glob("**/*")
            rels = [p.rsplit("/workspace/", 1)[-1] for p in r["files"]]
            for n in want_skipped:
                assert n not in rels, ("新 walker 没跳过 %s：%r" % (n, rels))
            assert "_foo.docx" in rels, rels
            assert "normal.txt" in rels, rels
    print("  [ok] 三前缀在**两处**都被跳过，而 `_foo.docx` 两处都在（负对照）")


def test_the_walker_really_passes_follow_symlinks_false():
    """符号链接那条的替代读者。

    真的造一个符号链接要开发者模式（这台机器上造不出来，那条测试会跳过），
    而"会自己跳过的护栏等于没有护栏"。所以另立一条能红的机械读者：
    walker 里 `is_dir` / `is_file` 的每一次调用都必须带 `follow_symlinks=False`
    —— 少一个，符号链接就从那个口子走出去。
    """
    import ast

    src = (_PROJECT_ROOT / "core" / "file_tools.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seg = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_iter_files":
            seg = ast.get_source_segment(src, node) or ""
    assert seg, "setup: 找不到 `_iter_files`"

    calls = []
    for node in ast.walk(ast.parse(seg)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("is_dir", "is_file"):
                calls.append(node)
    assert len(calls) >= 2, (
        "`_iter_files` 里找不到 is_dir/is_file 的调用 —— 检查器自己看错了：%r"
        % [c.func.attr for c in calls])
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        got = kw.get("follow_symlinks")
        assert isinstance(got, ast.Constant) and got.value is False, (
            "`%s()` 没有显式 `follow_symlinks=False` —— 符号链接会被跟着走"
            % c.func.attr)
    print("  [ok] walker 的 %d 处 is_dir/is_file 都显式不跟符号链接（AST）" % len(calls))


# 7. 预算：逐工具覆盖

def test_read_file_budget_constants_are_ordered():
    """两把刀方向相反：handler 必须裁到严格小于循环闸。"""
    from core import agent_loop as AL

    assert FT.READ_FILE_CHAR_CAP < FT.READ_FILE_LOOP_BUDGET, (
        "handler 的裁切上限不小于循环闸 —— 循环那把刀会再切一次，"
        "而它保的是**头部**（方向正好相反）")
    assert AL._result_budget("read_file", 6000) == FT.READ_FILE_LOOP_BUDGET
    assert AL._result_budget("create_document", 6000) == 6000, (
        "逐工具覆盖漏到别的工具上了 —— 档表那一格会被静默顶掉")
    print("  [ok] CAP(%d) < BUDGET(%d)，且覆盖只对 read_file 生效"
          % (FT.READ_FILE_CHAR_CAP, FT.READ_FILE_LOOP_BUDGET))


def test_read_file_result_is_not_cut_by_the_loop_gate():
    """端到端：一个 100 KB 的文件读出来不该被档表那 6000 切掉。"""
    with _lab() as (tmp, _g, _w):
        big = _ws(tmp) / "big.txt"
        big.write_text("\n".join("line %05d %s" % (i, "x" * 40)
                                 for i in range(1, 4001)) + "\n",
                       encoding="utf-8")
        assert big.stat().st_size > 100_000, big.stat().st_size

        body = _run_loop_reading(big)
        assert "<persistent_output>" not in body, (
            "被循环闸切了 —— 逐工具预算没接上：前 200 字符 %r" % body[:200])
        assert len(body) > 20_000, "读出来的太少（%d 字符）" % len(body)
        assert "offset=" in body, (
            "超了 handler 自己的上限却**没给下一个 offset** —— 模型没法接着读")
    print("  [ok] 100 KB 的读没被 6000 那把刀切，且给了 offset 路线")


def _run_loop_reading(path: Path) -> str:
    """真的跑一次 `agent_loop`，让模型调一次 `read_file`，回那条 tool 消息。"""
    import json

    import bus.registry as R
    from core import agent_loop as AL

    R.register_file_tools()

    class _Resp:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls
            self.prompt_tokens = 10
            self.completion_tokens = 5
            self.reasoning_content = None

        @property
        def total_tokens(self):
            return self.prompt_tokens + self.completion_tokens

    class _Provider:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, system=None, thinking=None):
            self.n += 1
            if self.n == 1:
                return _Resp(tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "read_file",
                                 "arguments": json.dumps({"file_path": str(path)})},
                }])
            return _Resp(content="done")

    with _session():
        _out, msgs, _c, _t = AL.agent_loop(
            "read it", _Provider(), messages=[{"role": "user", "content": "read it"}],
            max_turns=4, session_id=SID)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "setup: read_file 没被执行 —— 下面全是空跑"
    return tool_msgs[-1]["content"]


# 8. 只读子 agent（执行层拒）

def test_readonly_guard_scopes_and_restores():
    from core import subagent as SA

    assert SA.readonly() is False, "setup: 一开始就该是关的"
    with SA._ReadonlyGuard():
        assert SA.readonly() is True
        with SA._ReadonlyGuard():
            assert SA.readonly() is True
        assert SA.readonly() is True, "内层退出把外层的也关了"
    assert SA.readonly() is False, "出作用域没还原（`H4g` 那条）"

    try:
        with SA._ReadonlyGuard():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert SA.readonly() is False, "★ 抛异常也必须还原"
    print("  [ok] 只读档：作用域内为真、嵌套正确、**抛异常也还原**")


def test_is_write_like_is_fail_closed():
    from core import subagent as SA

    # 它读的是注册表里的权限档，必须先真的注册。
    # 不注册时一切都回 `admin`，连 `read_file` 都是"写类" —— 那正是
    # fail-closed 该有的样子（下面最后一条就是钉这个的）。
    _boot_measure()
    for name in ("write_file", "edit_file", "delete_files", "deliver_file",
                 "create_document", "add_block", "run_command", "todo_write"):
        assert SA.is_write_like(name) is True, "%s 该被判成写类" % name
    for name in ("read_file", "glob", "grep", "parse_docx", "get_document_info"):
        assert SA.is_write_like(name) is False, "%s 是纯读" % name
    assert SA.is_write_like("some_tool_nobody_classified") is True, (
        "★ fail-closed：谁都没分类过的名字必须默认**拒**，不是默认放行")
    print("  [ok] 写/读分类对，未分类的 fail-closed")


def test_readonly_child_refuses_writes_and_does_not_hang():
    """两个派发口都要拒，且永不挂起；正对照：父那层照常能跑。"""
    import json

    import bus.registry as R
    from core import agent_loop as AL
    from core import subagent as SA

    with _lab() as (tmp, _g, _w):
        target = _ws(tmp) / "child_target.txt"
        target.write_text("original\n", encoding="utf-8")
        R.register_file_tools()

        args = json.dumps({"file_path": str(target), "content": "CHILD WROTE\n"})

        # 正对照：父那层（没有只读档）照常跑
        body = _run_loop_raw("write_file", args, pre_read=target)
        assert '"success": true' in body, (
            "正对照失败：父这层也写不了 —— 下面那条断言就没有意义了：%r" % body[:300])
        assert _text(target) == "CHILD WROTE\n"

        # 只读那一层：拒 + 带路线 + 不挂起
        target.write_text("original\n", encoding="utf-8")
        box = {}

        def child():
            with SA._ReadonlyGuard():
                box["body"] = _run_loop_raw("write_file", args, pre_read=target)

        t = threading.Thread(target=child, daemon=True)
        t.start()
        t.join(30)
        assert not t.is_alive(), (
            "★ 只读子的拒绝**挂起了** —— 子的循环跑在父的一次 tool_call 里面，"
 "它没有可以提问的那一轮")
        assert "read-only" in box["body"] or "只读" in box["body"], (
            "只读子里没拒：%r" % box["body"][:300])
        assert _text(target) == "original\n", "★ 拒绝了但**文件还是被写了**"
    print("  [ok] 只读子里写工具被拒（不挂起、文件没动）；父那层正对照照常跑")


def test_readonly_branch_exists_on_both_paths_ast():
    """锁步：只读拒绝 / 纯文本回执 / 逐工具预算，两条路逐字同一句。"""
    import ast

    src = (_PROJECT_ROOT / "core" / "agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    text_of = {}
    # `agent_loop_stream` 是 `async def`（`AsyncFunctionDef`）——
    # 只收 `FunctionDef` 的话，"流式那条没锁步"会是检查器自己看错了。
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            text_of[node.name] = ast.get_source_segment(src, node) or ""

    # 回执形状转换收进了 `_dispatch_generic_tool` ——
    #   若有一条分支只有 `pass`，工具就没派发（`UnboundLocalError`）。
    #判据是"两条路都只经那一个派发器"：
    #   · `_tool_result_of(result, tool_name)` 全文件恰好 1 次，在 helper 里；
    #   · `agent_loop` / `agent_loop_stream` 各自都调 helper（通用那一支
    #     + 完全权限已放行那一支，每边至少 2 处）。
    need = {
        "子 agent 只读拒绝": "subagent.readonly() and subagent.is_write_like(tool_name)",
        "逐工具预算": "max_chars=_result_budget(tool_name, tool_result_max_chars)",
    }
    for label, needle in need.items():
        total = src.count(needle)
        assert total == 2, (
            "`%s` 在 core/agent_loop.py 里出现了 %d 次（要 2 次：sync + stream）"
            " —— 另一条路漏了就是**打开流式才发作**的洞" % (label, total))
        for fn in ("agent_loop", "agent_loop_stream"):
            assert needle in text_of.get(fn, ""), (
                "`%s` 不在 %s 里 —— 两条路没锁步" % (label, fn))

    # 回执形状转换恰好两处，且两处都点得出名字：
    #   ① `_dispatch_generic_tool` —— 正常派发；
    #   ② `_permission_hook_decide` —— `PermissionRequest` 钩子替人放行之后
    #      就地补跑的那一次。它有意不走①：派发器会再问一遍钩子
    #同一条钩子说 allow、派发器又来问、又补跑 …… 死循环。
    #   判据写成"这两处，别的都不许"—— 第三个造型点照样红。
    _shape_sites = ("_dispatch_generic_tool", "_permission_hook_decide")
    _shape_n = src.count("_tool_result_of(result, tool_name)")
    assert _shape_n == len(_shape_sites), (
        "回执形状转换出现了 %d 次（只许 %d 处：%s）—— 多一处 = 又有人绕开"
        " 派发器自己造型" % (_shape_n, len(_shape_sites), " / ".join(_shape_sites)))
    for site in _shape_sites:
        assert "_tool_result_of(result, tool_name)" in text_of.get(site, ""), (
            "`_tool_result_of` 不在 `%s` 里 —— 这一处的回执形状换了口径" % site)
    for fn in ("agent_loop", "agent_loop_stream"):
        n = text_of.get(fn, "").count("_dispatch_generic_tool(")
        assert n >= 2, (
            "%s 只调 `_dispatch_generic_tool` %d 次（至少 2：通用那一支 + "
            "完全权限已放行那一支）—— 少一处就是**那条路没派发**" % (fn, n))
    print("  [ok] 只读拒绝 / 逐工具预算 两条都在两条路上；回执造型只有一处（AST）")


def test_dispatch_subagent_maps_the_readonly_argument():
    """`readonly` 三种形状：认得出就认，认不出就拒（不静默降级）。"""
    from core import agent_loop as AL
    from core import request_snapshot, subagent as SA

    captured = {}
    orig = SA.run_child

    def fake(**kw):
        captured.clear()
        captured.update(kw)
        return {"success": True, "output": "x", "tokens": 0}

    SA.run_child = fake
    try:
        with _session():
            snap = request_snapshot.capture([], [], "", model="m", session_id=SID)
            request_snapshot.set_current(snap)
            asst = {"role": "assistant", "content": "", "tool_calls": []}

            # 每次调用一支新的位子表 —— `_SubagentSlots` 是取即减的，
            # 复用同一支跑到第 4 次就被"位子用完了"挡下来了，
            # 而那与 `readonly` 一点关系都没有。
            def call(args):
                return AL._dispatch_subagent(
                    "run_subagent", args, None, SID, asst, AL._SubagentSlots(3))

            out = call({"task": "t", "readonly": True})
            assert captured.get("readonly_mode") is True, (out, captured)

            call({"task": "t"})
            assert captured.get("readonly_mode") is False, captured

            call({"task": "t", "readonly": "false"})
            assert captured.get("readonly_mode") is False, captured

            call({"task": "t", "readonly": False})
            assert captured.get("readonly_mode") is False, captured

            before = dict(captured)
            out = call({"task": "t", "readonly": "yes please"})
            assert captured == before, (
 "★ 认不出的 readonly 被静默降级成'可以写'了 —— 那条"
                "「参数坏了不许跑」的形状：%r" % (out,))
            assert "readonly" in out, out
    finally:
        SA.run_child = orig
    print("  [ok] readonly 参数：true/省略/\"false\" 三种都对；坏值**拒**（不静默降级）")


def _run_loop(tool_name: str, args: str):
    """跑一次 agent_loop，返回那条 tool 消息的 JSON。"""
    import json

    return json.loads(_run_loop_raw(tool_name, args))


def _run_loop_raw(tool_name: str, args: str, pre_read=None) -> str:
    import bus.registry as R
    from core import agent_loop as AL

    R.register_file_tools()
    if pre_read is not None:
        # 写前必读那道闸要先满足 —— 否则测的是"没读过"，不是"只读档"。
        # 走同一个会话号，所以读态落在同一格里。
        with _session():
            FT.read_file(str(pre_read))

    class _Resp:
        def __init__(self, content=None, tool_calls=None):
            self.content = content
            self.tool_calls = tool_calls
            self.prompt_tokens = 10
            self.completion_tokens = 5
            self.reasoning_content = None

        @property
        def total_tokens(self):
            return self.prompt_tokens + self.completion_tokens

    class _Provider:
        def __init__(self):
            self.n = 0

        def chat(self, messages, tools=None, system=None, thinking=None):
            self.n += 1
            if self.n == 1:
                return _Resp(tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": tool_name, "arguments": args},
                }])
            return _Resp(content="done")

    with _session():
        _out, msgs, _c, _t = AL.agent_loop(
            "go", _Provider(), messages=[{"role": "user", "content": "go"}],
            max_turns=4, session_id=SID)
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "setup: %s 没被执行 —— 下面全是空跑" % tool_name
    return tool_msgs[-1]["content"]


# 9. 纯文本回执

def test_as_text_keeps_plain_text_but_jsonifies_everything_else():
    from core.file_tools import PLAIN_TEXT_TOOLS, as_text

    raw = "     1\tline one\n     2\tline two"
    assert as_text(raw, "read_file") is raw, "read_file 的回执被套了 JSON 信封"
    assert as_text(raw, "grep") is raw, "grep 的回执被套了 JSON 信封"
    assert as_text(raw, "memory_read") is raw, "memory_read 的回执被套了 JSON 信封"
    assert as_text("x", "glob").startswith('"'), "glob 应当照旧走 json.dumps"
    # 精确等于断言是刻意的（防"顺手塞一个进来"）。
    # 记忆两件读工具、查历史同理 —— 同一条判据（截断后仍是可读正文）。
    assert PLAIN_TEXT_TOOLS == frozenset(
        {"read_file", "grep", "code_intel", "memory_read", "memory_search",
         "history_search"}), PLAIN_TEXT_TOOLS
    assert as_text("a.py:2:1", "code_intel") == "a.py:2:1", (
        "符号查询的回执被套了 JSON 信封")

    # 认的是名单不是类型：一个恰好返回 str、但不在名单里的工具行为不变
    import json
    assert as_text("surprise", "some_other_tool") == json.dumps("surprise"), (
        "认类型了 —— 将来某个返回 str 的工具会被静默换掉 wire 形状")
    print("  [ok] as_text 认名单不认类型（read_file/grep 纯文本，其余照旧）")


def test_read_file_output_is_line_numbered_text():
    with _lab() as (tmp, _g, _w):
        f = _ws(tmp) / "numbered.txt"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        with _session():
            out = FT.read_file(str(f))
            assert out.startswith("     1\talpha"), repr(out[:40])
            assert "\n     2\tbeta" in out, repr(out)
            assert not out.lstrip().startswith("{"), "回执套了 JSON 信封"
    print("  [ok] read_file 回的是 `行号\\t内容` 的纯文本")


# 10. 隔离分类 / 权限档 / 常驻集

def test_isolation_lists_by_registry():
    """按注册表反查，不抄名单。"""
    import bus.registry as R

    from core import agent_loop as AL

    _boot_measure()
    registered = {td["function"]["name"] for td in R.get_tool_schemas()}

    for n in ("read_file", "grep"):
        assert n in AL._ISOLATED_DOC_TOOLS, (
            "%s 回的是一整个/一片**第三人可写的正文**，必须隔离" % n)
        assert n in registered, "%s 在名单里但没注册（死名）" % n
    # `glob` 回的是路径，不是正文 —— 不进隔离（它会多付一层包裹的钱）
    assert "glob" not in AL._ISOLATED_DOC_TOOLS
    print("  [ok] read_file / grep 在隔离名单里；glob 不在（它只回路径）")


def test_permission_tiers_and_the_read_excel_change():
    import bus.registry as R

    _boot_measure()          # 权限档是从注册表现算的，先装载
    want = {"read_file": "read", "glob": "read", "grep": "read",
            "write_file": "write", "edit_file": "write"}
    for name, tier in want.items():
        assert R.get_tool_permission(name) == tier, (
            "%s 的档是 %s，期望 %s" % (name, R.get_tool_permission(name), tier))
    # 有意的行为变化：`read_` 进了 `_READ_PREFIXES`，它从 admin 变 read
    assert R.get_tool_permission("read_excel_data") == "read", (
        "`read_` 前缀没生效 —— 那 `read_file` 会挂 admin 档"
        "（不可并发 + 每次一行 [audit] ADMIN）")
    # 负对照：没命中任何前缀的仍然是 admin（fail-closed 还在）
    assert R._classify_permission("totally_unknown_tool") == "admin"
    print("  [ok] 五个档对；read_excel_data 有意从 admin 变 read；未分类仍 admin")


def test_all_five_are_core_tools():
    import bus.registry as R

    for n in ("read_file", "write_file", "edit_file", "glob", "grep"):
        assert n in R._CORE_TOOLS, (
 "%s 不常驻 —— 模型会继续用 run_command 干同样的事（决策 5）" % n)
    print("  [ok] 五件都在常驻集里")


def test_init_bus_and_measure_request_both_register_them():
    """源码读者：两处接线都在（"写了"不等于"会调用"）。"""
    for rel, needle in (("bus/core.py", "register_file_tools()"),
                        ("scripts/measure_request.py", "R.register_file_tools()")):
        src = (_PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert needle in src, "%s 里没有 %s —— 这条能力不存在" % (rel, needle)
    print("  [ok] init_bus 与 measure_request 都真的注册了它们")


def test_activity_verbs_cover_all_five():
    """活动行：五件各自一行（同一家族合并成一行就分不出在做什么）。"""
    from core.activity import _VERBS, describe_tool

    for n in ("read_file", "write_file", "edit_file", "glob", "grep"):
        assert n in _VERBS, "%s 会掉到英文 _fallback" % n
    lines = {describe_tool(n) for n in ("read_file", "write_file", "edit_file")}
    assert len(lines) == 3, "三个工具渲染成了同一行：%r" % lines
    print("  [ok] 五件都有中文活动行，且读/写/改不是同一行")


# 11. /clear 清读态（走真的 MemoryManager.new_session）


def _make_mgr(tmp_dir):
    import core.memory.manager as manager_mod

    cfg = {"memory": {
        "storage_path": str(Path(tmp_dir) / "memory"),
        "short_term": {"max_history": 100},
        "compression": {"token_threshold": 0.85, "keep_recent_turns": 3},
        "session_memory": {"sessions_dir": str(Path(tmp_dir) / "sessions")},
    }}
    return manager_mod.MemoryManager(cfg, provider=None)


def test_clear_purges_read_state_and_leaves_others_alone():
    """走真的 `/clear`（`MemoryManager.new_session`），不是只调 `forget_reads`。

    只调函数测的是"函数能用"，不是"接线接上了" —— 零生产调用者
    正是这一条要防的形状。
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home:
        mgr = _make_mgr(home)
        sid = mgr.short_term.init_session()
        other = mgr.short_term.init_session()

        with _lab() as (tmp, _g, _w):
            f = _ws(tmp) / "clearme.txt"
            f.write_text("v1\n", encoding="utf-8")
            g = _ws(tmp) / "other.txt"
            g.write_text("v1\n", encoding="utf-8")

            with _session(sid):
                FT.read_file(str(f))
            with _session(other):
                FT.read_file(str(g))
            assert FT._reads.get(sid), "夹具没写进去 —— 下面两条断言会变成空跑"

            # `/clear` 铸新 id + 重绑通道键，而读态按通道键清
            #在这里显式给出它（生产是 `SessionPool` 给的）。
            new_sid, _msgs = mgr.new_session(sid, channel_key=sid)
            assert new_sid != sid, (
                "`S3`: /clear 给一条新的存储 id（实得 %r）" % new_sid)
            assert not FT._reads.get(sid), (
                "/clear 之后读态还在 —— 下一段对话**看起来**已经读过"
                "上一段的文件，CAS 会放行一次它从没做过的写前读")
            assert FT._reads.get(other), (
                "清一个会话顺手把别的会话也清了 —— 六个通道共用一个 scheduler")
            FT.forget_reads(other)
    print("  [ok] /clear 清本会话的读态，别的会话不动（走真的 new_session）")


def test_workspace_manager_streams_through_the_shared_rule():
    """索引的 `_scan_dir` 真的调了 `skip_walk_entry`（不是各写各的）。"""
    import ast

    src = (_PROJECT_ROOT / "core" / "workspace.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hit = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_scan_dir":
            seg = ast.get_source_segment(src, node) or ""
            assert "skip_walk_entry(" in seg, (
                "`_scan_dir` 没走共享判据 —— 两份跳过规则已经开始漂移了")
            hit = True
    assert hit, "setup: 找不到 `_scan_dir`"
    print("  [ok] `PathIndex._scan_dir` 调的是共享判据（AST 取证）")


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
