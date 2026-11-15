# -*- coding: utf-8 -*-
"""`core/code_intel.py` 的机械读者 —— 协议层用真的子进程验。

钉住：

1. 握手 + 分帧：真的拉起一个 stdio LSP 服务器，发 initialize / didOpen /
   请求，能收回结果（不是对着假对象调）。
2. 位置换算：行、列都从 1 起；列在内部换成 UTF-16 偏移。
3. 五种操作的输出形状：位置列表 / 悬停 / 文件符号（含子节点缩进）/ 全局符号。
4. 失败带路线：没有服务器、扩展名没配、文件不存在 —— 都是文本，
   不抛给循环。
5. 注册条件：配置了才注册；没配置时 schema 一个字节都不发。
6. 分类：在纯文本名单里，也在隔离名单里（源码片段是第三人写的文字）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_code_intel.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import code_intel as CI  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


class _Lab:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.fake = str(_PROJECT_ROOT / "tests" / "_fake_lsp_server.py")

    def spec(self, exts=(".py",)):
        return [{"name": "fake", "command": [sys.executable, self.fake],
                 "extensions": list(exts)}]

    def __enter__(self):
        self._old_root = CI._workspace_root
        self._old_global = CI.GLOBAL_CONFIG
        # 目录探测必须在本机没装任何服务器时才为空 —— 不钉这一条，
        #   谁装一个 pyright，「没配置」那几条就全红（而产品行为是对的）。
        #   夹具自己把"装了什么"清空，断言才只反映配置。
        self._old_find = CI._find_binary
        CI._find_binary = lambda name, root=None: ""
        CI._workspace_root = lambda: self.root
        CI.GLOBAL_CONFIG = self.root / "no-such-global.json"
        (self.root / "a.py").write_text(
            "中文x = 1\ndef alpha():\n    pass\n\nbeta = alpha\n",
            encoding="utf-8")
        (self.root / "b.txt").write_text("hello\n", encoding="utf-8")
        return self

    def __exit__(self, *a):
        CI.reset()
        CI._workspace_root = self._old_root
        CI.GLOBAL_CONFIG = self._old_global
        CI._find_binary = self._old_find
        self.tmp.cleanup()


def _allow_project(root: Path) -> None:
    """打开工作区配置的信任门（`data/settings.json` 的 `lsp_allow_project`）。

    那份设置是相对进程 cwd 读的（与 `core/hooks.py` 同一条规矩）
调它的测试必须先把 cwd 挪到 `root`，否则读到的是仓库自己的那份。
    """
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text('{"lsp_allow_project": true}',
                                     encoding="utf-8")


def test_project_config_needs_trust():
    """工作区里的 `lsp.json` 默认不读 —— 它配的是任意命令。

    缺口：钩子那一半有信任门（`hooks_allow_project`），
    LSP 这一半没有，而两边的东西是同一类（跟着仓库走的任意命令）。
    判据三条：默认不读 · 打开后读得到 · 只认真布尔 `true`。
    """
    with _Lab() as lab:
        (lab.root / "lsp.json").write_text(json.dumps({"servers": {
            "evil": {"command": ["python", "-c", "print(1)"],
                     "extensionToLanguage": {".py": "python"}},
        }}), encoding="utf-8")
        old_cwd = Path.cwd()
        try:
            os.chdir(lab.root)                # 设置与"工作区"都落在 lab.root 下
            CI.reset()
            got = CI.load_servers(lab.root)
            check("★★ 默认不读工作区 lsp.json",
                  all(sp["id"] != "evil" for sp in got),
                  str([sp["id"] for sp in got]))

            _allow_project(lab.root)
            CI.reset()
            got = CI.load_servers(lab.root)
            check("★ 打开信任门之后读得到",
                  any(sp["id"] == "evil" for sp in got),
                  str([sp["id"] for sp in got]))

            (lab.root / "data" / "settings.json").write_text(
                '{"lsp_allow_project": "true"}', encoding="utf-8")
            CI.reset()
            got = CI.load_servers(lab.root)
            check('★ 字符串 "true" 不算（只认严格布尔）',
                  all(sp["id"] != "evil" for sp in got),
                  str([sp["id"] for sp in got]))
        finally:
            os.chdir(old_cwd)
            CI.reset()


def test_no_config_says_so():
    with _Lab() as lab:
        out = CI.run_operation("definition", "a.py", 2, 1)
        check("没配置时给的是路线，不是异常",
              out.startswith("没有配置语言服务器") and "lsp.json" in out, out)
        check("config_present 为假", CI.config_present() is False)


def test_definition_and_references():
    with _Lab() as lab:
        CI.configure(lab.spec())
        check("配置后 config_present 为真", CI.config_present() is True)

        out = CI.run_operation("definition", "a.py", 2, 5)
        check("定义：相对路径 + 1 起的行号", out == "a.py:2:1", repr(out))

        out = CI.run_operation("references", "a.py", 2, 5)
        check("引用：两条、按顺序", out.splitlines()[:2] ==
              ["a.py:2:1", "a.py:5:3"], repr(out))

        out = CI.run_operation("hover", "a.py", 1, 3)
        check("★ 列换算成 UTF-16 偏移（中文不按字节算）",
              out == "pos 0:2", repr(out))

        out = CI.run_operation("symbols", "a.py", 1, 1)
        check("文件符号：名字 + 种类",
              "alpha [function]" in out and "Beta [class]" in out, out)
        check("★ 符号回执带行号列号（模型据此才调得动 definition）",
              "alpha [function]  1:1" in out, out)
        check("★ 子节点按层级缩进",
              any(line.startswith("  gamma [method]") for line in out.splitlines()),
              out)

        out = CI.run_operation("implementation", "a.py", 2, 1)
        check("实现：位置列表", out == "a.py:3:5", repr(out))

        out = CI.run_operation("prepare_call_hierarchy", "a.py", 2, 1)
        check("调用层级条目：名字 + 位置",
              "alpha [function]" in out and "a.py:2:1" in out, out)

        out = CI.run_operation("incoming_calls", "a.py", 2, 1)
        check("谁在调它", "caller [function]" in out, out)

        out = CI.run_operation("outgoing_calls", "a.py", 2, 1)
        check("它在调谁", "callee [function]" in out, out)

        out = CI.run_operation("workspace_symbols", "a.py", query="alpha")
        check("全局符号带路径", "alpha" in out and "a.py:2:1" in out, out)


def test_config_change_takes_effect_without_restart():
    with _Lab() as lab:
        old_ttl = CI._PRESENT_TTL_S
        old_cwd = Path.cwd()
        CI._PRESENT_TTL_S = 0.0
        try:
            os.chdir(lab.root)                # 信任门那份设置相对 cwd
            check("一开始没有配置", CI.config_present(lab.root) is False)
            _allow_project(lab.root)          # 工作区配置默认不读（信任门）
            (lab.root / "lsp.json").write_text(
                '{"servers":[{"name":"x","command":"true","extensions":[".py"]}]}',
                encoding="utf-8")
            check("★ 加了 lsp.json => 下一次就能看见（不用重启）",
                  CI.config_present(lab.root) is True)
            (lab.root / "lsp.json").unlink()
            check("★ 删掉 => 又看不见", CI.config_present(lab.root) is False)
        finally:
            os.chdir(old_cwd)
            CI._PRESENT_TTL_S = old_ttl


def test_errors_are_text_with_routes():
    with _Lab() as lab:
        CI.configure(lab.spec())
        out = CI.run_operation("definition", "b.txt", 1, 1)
        check("扩展名没配：说清已配置了谁",
              out.startswith("错误：没有为 `.txt` 配置") and "fake" in out, out)
        out = CI.run_operation("definition", "nope.py", 1, 1)
        check("文件不存在：给路径问题", out.startswith("错误：找不到"), out)
        out = CI.run_operation("bogus", "a.py", 1, 1)
        check("未知操作：列可用的", out.startswith("错误：operation"), out)
        out = CI.run_operation("hover", "a.py", 99, 1)
        check("行越界：说文件有多少行", out.startswith("错误：文件只有"), out)


def test_diagnostics_are_reported_and_attached():
    with _Lab() as lab:
        CI.configure(lab.spec())
        got = CI.diagnose(lab.root / "a.py")
        check("诊断：拿到报告", bool(got and got.get("reported")), repr(got))
        check("诊断：说清数量并带位置",
              "1 个错误" in got["text"] and "a.py:2:1" in got["text"],
              repr(got["text"]))
        check("扩展名没配 -> 不报告（不返回空结论）",
              CI.diagnose(lab.root / "b.txt") is None)

    # 编辑之后自动带上（走真的 write 路径）
    from _sandbox import sandbox
    import contextlib

    @contextlib.contextmanager
    def _ws_lab():
        with sandbox() as tmp:
            ws = tmp / "workspace"
            ws.mkdir(exist_ok=True)
            (tmp / "fakehome").mkdir(exist_ok=True)
            from bus.registry import (register_workspace_manager,
                                      get_workspace_manager)
            from core.security import SecurityGuard
            from core.workspace import WorkspaceManager
            guard = SecurityGuard(allowed_dirs=["./workspace"],
                                  command_blacklist=[],
                                  audit_log_path=str(tmp / "a.log"),
                                  user_home=str(tmp / "fakehome"))
            wm = WorkspaceManager(guard, {})
            old = get_workspace_manager()
            register_workspace_manager(wm)
            old_root = CI._workspace_root
            CI._workspace_root = lambda: ws.resolve()
            try:
                yield ws
            finally:
                CI.reset()
                CI._workspace_root = old_root
                register_workspace_manager(old)

    with _ws_lab() as ws:
        CI.configure([{"name": "fake", "command": [sys.executable, str(
            _PROJECT_ROOT / "tests" / "_fake_lsp_server.py")],
            "extensions": [".py"]}])
        from core.file_tools import edit_file, read_file
        from core import request_snapshot
        f = ws / "d.py"
        f.write_text("alpha\nbeta\n", encoding="utf-8")
        request_snapshot.set_current(request_snapshot.capture(
            [], [], "", model="probe", session_id="codeintel:probe"))
        try:
            read_file(str(f))
            out = edit_file(str(f), "beta", "gamma")
        finally:
            request_snapshot.clear_current()
        check("编辑成功", out.get("success") is True, out)
        check("★ 编辑回执自动带诊断",
              "1 个错误" in (out.get("diagnostics") or ""), out)


def test_new_config_shape_and_precedence():
    with _Lab() as lab:
        # 新形状：servers 是对象，键就是服务器名
        (lab.root / "lsp.json").write_text(json.dumps({"servers": {
            "python": {"command": "explicit-py", "args": ["--stdio"],
                       "extensionToLanguage": {".py": "python"}},
        }}), encoding="utf-8")
        # 技能声明：同 id 不应盖掉显式配置；另一个 id 应被并入
        skill = lab.root / "skills" / "foo"
        skill.mkdir(parents=True)
        (skill / "lsp.json").write_text(json.dumps({"servers": {
            "python": {"command": "skill-py",
                       "extensionToLanguage": {".py": "python"}},
            "ruby": {"command": "ruby-lsp",
                     "extensionToLanguage": {".rb": "ruby"}},
        }}), encoding="utf-8")

        old_cwd = Path.cwd()
        old_find = CI._find_binary
        try:
            os.chdir(lab.root)
            _allow_project(lab.root)          # 工作区配置默认不读（信任门）
            CI.reset()
            CI._find_binary = lambda name, root: ("/fake/gopls" if name == "gopls" else "")
            (lab.root / ".git").mkdir(exist_ok=True)
            specs = CI.load_servers(lab.root)
        finally:
            CI._find_binary = old_find
            os.chdir(old_cwd)
            CI.reset()

        ids = [sp["id"] for sp in specs]
        check("显式配置在最前", ids and ids[0] == "python", ids)
        check("★ 同 id：显式配置赢", specs[0]["command"] == ["explicit-py"], specs[0])
        check("技能声明被并入", "ruby" in ids, ids)
        check("★ 目录探测被并入", "go" in ids, ids)
        check("探测出来的命令是本机路径",
              [sp for sp in specs if sp["id"] == "go"][0]["command"] == ["/fake/gopls"])


def test_registration_only_when_configured():
    from bus import registry as R

    with _Lab() as lab:
        CI.configure(lab.spec())
        R.register_code_intel_tool()
        names = [td["function"]["name"] for td in R.get_tool_schemas()]
        check("配置了 -> 注册进表", "code_intel" in names, names[-5:])
        check("权限档是 read", R.get_tool_permission("code_intel") == "read")
        check("不算破坏性", R.get_tool_destructive("code_intel") is False)


def test_classification_lists():
    from core import agent_loop as AL
    from core import file_tools as FT
    check("回执按纯文本出去", "code_intel" in FT.PLAIN_TEXT_TOOLS)
    check("★ 在隔离名单里（源码片段是第三人写的文字）",
          "code_intel" in AL._ISOLATED_DOC_TOOLS)


def test_parsing_rejects_bad_specs():
    specs = CI._parse_specs([
        {"name": "ok", "command": "x", "extensions": ["py"]},
        {"name": "no-ext", "command": "x"},
        {"name": "bad-cmd", "command": 123, "extensions": [".py"]},
        "not-a-dict",
    ])
    check("坏条目跳过、好条目留下", len(specs) == 1, repr(specs))
    check("扩展名自动补点", specs[0]["ext_map"] == {".py": "python"},
          repr(specs[0]))


def test_workspace_symbols_needs_no_file():
    """按名字全局搜符号不该要一个文件 —— 实际使用中模型就是这么被卡的：
    它调 `workspace_symbols` 没给 `file_path`，收到"需要 file_path"，
    于是以为这个工具没配好、整条路放弃了。"""
    with _Lab() as lab:
        CI.configure(lab.spec())
        out = CI.run_operation("workspace_symbols", query="alpha")
        check("★ 不给 file_path 也能按名字搜", "alpha" in out, out)
        check("带文件路径照旧可用",
              "alpha" in CI.run_operation("workspace_symbols", "a.py", query="alpha"))


def test_each_write_gets_a_fresh_report():
    """同一个文件第二次写入也必须拿到自己的报告。

    现场：第一条编辑带回诊断，第二条编辑的回执里没有诊断字段 ——
    因为复用了一个已经 `set` 的 Event：`wait()` 立刻返回、手上还是上一份报告，
    被时间戳检查判掉。模型会把"没有字段"读成"没有错误"。
    """
    import threading
    import time as _time

    with _Lab() as lab:
        CI.configure(lab.spec())
        path = lab.root / "a.py"
        item = {"range": {"start": {"line": 1, "character": 0},
                          "end": {"line": 1, "character": 3}},
                "severity": 1, "message": "fake undefined name"}

        class Stub:
            caps: dict = {}
            _opened: dict = {}

            def __init__(self):
                self._diag_lock = threading.Lock()
                self._diag_events: dict = {}
                self._diag: dict = {}
                self.pushes = 0

            def start(self):
                pass

            def open_file(self, p):
                key = CI._uri_key(CI._uri(p))
                self.pushes += 1

                # 推送必须异步（真服务器就是异步的）：同步推的话，
                #   即使复用了已经 set 的 Event，读到的也恰好是新报告，
                #   这条断言就抓不住那个 bug（负对照验过）。
                def _later():
                    _time.sleep(0.05)
                    with self._diag_lock:
                        self._diag[key] = (_time.time(), [item])
                        ev = self._diag_events.get(key)
                    if ev is not None:
                        ev.set()

                threading.Thread(target=_later, daemon=True).start()

        stub = Stub()
        old = CI._client_for
        CI._client_for = lambda spec, root: stub
        try:
            first = CI.diagnose(path, timeout=0.5)
            second = CI.diagnose(path, timeout=0.5)
        finally:
            CI._client_for = old
        check("第一次拿到报告", bool(first and first.get("reported")), first)
        check("★ 第二次也拿到报告（不是上一份）",
              bool(second and second.get("reported")), second)
        check("两次都真的开了文件", stub.pushes == 2, stub.pushes)


def test_diagnose_keeps_a_report_that_arrives_first():
    """报告先于"记时间戳"到达时不许被当成旧的丢掉。

    反过来的顺序（先 `didOpen`、后记 `since`）在快服务器上会丢：并行跑全量时
    偶发"没有诊断"，而单跑几乎不复现。
    """
    import threading
    import time as _time

    with _Lab() as lab:
        CI.configure(lab.spec())
        path = lab.root / "a.py"
        item = {"range": {"start": {"line": 1, "character": 0},
                          "end": {"line": 1, "character": 3}},
                "severity": 1, "message": "fake undefined name"}

        class Stub:
            caps: dict = {}
            _opened: dict = {}

            def __init__(self):
                self._diag_lock = threading.Lock()
                self._diag_events: dict = {}
                self._diag: dict = {}
                self.registered_before_open = False

            def start(self):
                pass

            def open_file(self, p):
                key = CI._uri_key(CI._uri(p))
                with self._diag_lock:
                    # 判据不看时钟：开文件那一刻，事件必须已经登记好了。
                    # 反过来的话报告会落在"还没人听"的窗口里，只能靠 1.5 秒超时兜，
                    # 而超时之后那道时间戳检查又把它当成旧报告丢掉。
                    self.registered_before_open = key in self._diag_events
                    self._diag[key] = (_time.time(), [item])
                    ev = self._diag_events.get(key)
                if ev is not None:
                    ev.set()

        stub = Stub()
        old = CI._client_for
        CI._client_for = lambda spec, root: stub
        try:
            got = CI.diagnose(path, timeout=0.5)
        finally:
            CI._client_for = old
        check("★ 事件先登记、后开文件", stub.registered_before_open)
        check("★ 先到的那份报告没被丢掉",
              bool(got and got.get("reported")) and got.get("count") == 1, got)


def test_shown_path_prefers_workspace_then_cwd():
    """回执里的路径与 `read_file` 同一个写法：相对、不是一整条绝对路径。"""
    root = Path.cwd() / "workspace"
    check("工作区内：相对工作区",
          CI._shown_path(root / "a.py", root) == "a.py")
    check("工作区外、cwd 内：相对 cwd",
          CI._shown_path(Path.cwd() / "core" / "x.py", root) == "core/x.py",
          CI._shown_path(Path.cwd() / "core" / "x.py", root))
    far = Path(tempfile.gettempdir()) / "x.py"
    check("都不在：绝对路径", Path(CI._shown_path(far, root)).is_absolute(),
          CI._shown_path(far, root))


def test_empty_results_carry_a_route():
    """空结果必须带路线 —— 否则它读起来像"这个工具坏了"。"""
    empty = CI._locations_or_route([], Path("."))
    check("空结果带路线", "没有结果" in empty and "symbols" in empty, empty)
    one = [{"uri": "file:///proj/a.py",
            "range": {"start": {"line": 0, "character": 0}}}]
    filled = CI._locations_or_route(one, Path("."))
    check("有结果时不加路线", "symbols" not in filled, filled)

    empty_ws = CI._ws_symbols([], Path("workspace"))
    check("全局符号空结果说清搜索范围",
          "工作区根" in empty_ws and "symbols" in empty_ws, empty_ws)


def test_relative_path_resolution_matches_file_tools():
    """相对路径的顺序：先按原样（进程 cwd），再试工作区。

    反过来的话，模型刚 `read_file("core/x.py")` 读到的那个路径，交给本工具
    就变成"找不到" —— 而两句话指着同一个文件。
    """
    with _Lab() as lab:
        cwd_file = Path("_probe_rel_target.py")
        cwd_file.write_text("x = 1\n", encoding="utf-8")
        try:
            got = CI._resolve_file("_probe_rel_target.py", lab.root)
            check("存在的相对路径：按 cwd 解析", got == cwd_file.resolve(), got)
            got = CI._resolve_file("a.py", lab.root)
            check("不存在于 cwd 的：落到工作区", got == (lab.root / "a.py").resolve(), got)
            abs_path = (lab.root / "a.py").resolve()
            check("绝对路径原样",
                  CI._resolve_file(str(abs_path), lab.root) == abs_path)
        finally:
            cwd_file.unlink(missing_ok=True)


def test_uri_key_normalizes_drive_letter():
    """盘符大小写不许影响诊断对号。

    现场反馈：我们发 `file:///D%3A/...`，服务器回来 `file:///d%3A/...`
按原文找永远找不到，症状是"诊断没报告"，与"没配服务器"同形。
    """
    a = CI._uri_key("file:///D%3A/OPD/x/use.py")
    b = CI._uri_key("file:///d%3A/OPD/x/use.py")
    check("★ 盘符大小写归一", a == b, (a, b))
    check("盘符未编码也归一",
          CI._uri_key("file:///D:/x.py") == CI._uri_key("file:///d:/x.py"))
    check("路径本身不动（别的系统大小写敏感）",
          CI._uri_key("file:///d%3A/Opd/X.py") == "file:///d%3A/Opd/X.py",
          CI._uri_key("file:///d%3A/Opd/X.py"))
    check("非 file URI 原样", CI._uri_key("untitled:1") == "untitled:1")


def main():
    for fn in (test_no_config_says_so,
               test_project_config_needs_trust,
               test_definition_and_references,
               test_new_config_shape_and_precedence,
               test_config_change_takes_effect_without_restart,
               test_diagnostics_are_reported_and_attached,
               test_errors_are_text_with_routes,
               test_registration_only_when_configured,
               test_classification_lists,
               test_workspace_symbols_needs_no_file,
               test_each_write_gets_a_fresh_report,
               test_diagnose_keeps_a_report_that_arrives_first,
               test_shown_path_prefers_workspace_then_cwd,
               test_empty_results_carry_a_route,
               test_relative_path_resolution_matches_file_tools,
               test_uri_key_normalizes_drive_letter,
               test_parsing_rejects_bad_specs):
        print("\n-- %s --" % fn.__name__)
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
