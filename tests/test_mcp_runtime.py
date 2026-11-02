"""「运行环境缺失」不许被说成「不兼容 / 已屏蔽」。

## 它治的病

没装 Node 的机器上，npx 型 MCP 连不上：

    ① `_connect_stdio` 抛 `FileNotFoundError [WinError 2]`
    ② `mcpx/manager.py` 抓住 → 记成 `mcp_connect_failed`
    ③ 界面 `PluginNotice.jsx` 弹「兼容性警告 → 不支持 → 已被 Crannia 屏蔽」
    ④ 而 `noticeText` 只显示名字和路径，不显示 reason

用户被告知「这个插件不兼容」，真相是这台机器少了个程序，装上就好。
两件事的做法完全相反：一个去装东西，一个去找作者（标签在说谎）。

## 四条判据

1. 判定：`missing_runtime()` 只对 stdio 有意义；带路径的写法判文件、裸命令走 PATH。
2. 分类：缺环境用自己的码（`mcp_missing_runtime`），不复用 `mcp_connect_failed`。
3. 文案：`missing_runtime_reason()` 里不许出现「不兼容 / 不支持 / 已屏蔽」，
   而且要说清装什么（`npx` → Node.js）。
4. 修好就没救的死结：连上了要把旧账划掉 —— 账本是持久的、闸只看账本，
   用户装好 Node 再重启时若账还在，服务器连上了、工具却被全摘掉，且不再弹窗。

用法::

    cd <仓库根>
    .venv/Scripts/python.exe tests/test_mcp_runtime.py

ASCII-only 输出。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import mcpx.manager as MGR                       # noqa: E402


# ----------------------------------------------------------------------
# 1. 判定
# ----------------------------------------------------------------------

def test_only_stdio_is_judged():
    """`sse` / `streamable_http` 没有本地可执行文件 —— 不许判成"缺环境"。"""
    assert MGR.missing_runtime({"transport": "sse", "command": "npx"}) == ""
    assert MGR.missing_runtime({"transport": "streamable_http", "url": "http://x"}) == ""
    print("  [ok] 非 stdio 的不判")


def test_a_missing_command_is_named():
    """找不到就把那个名字还回去（调用方要拿它去拼文案）。"""
    ghost = "crannia-no-such-binary-w269"
    assert MGR.missing_runtime({"command": ghost}) == ghost
    print("  [ok] 缺的命令名被原样还回来")


def test_an_existing_command_passes():
    """反方向：真有的命令不许误报（写错一边这条护栏就只会恒红或恒绿）。"""
    assert MGR.missing_runtime({"command": sys.executable}) == ""
    print("  [ok] 存在的命令不误报")


def test_a_path_form_is_judged_as_a_file():
    """带路径的写法判文件在不在，不走 PATH（`which()` 对带路径的输入平台相关）。"""
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "fake-tool.exe"
        exe.write_text("", encoding="utf-8")
        assert MGR.missing_runtime({"command": str(exe)}) == ""
        gone = str(Path(tmp) / "nope.exe")
        assert MGR.missing_runtime({"command": gone}) == gone
    print("  [ok] 带路径的按文件判")


def test_an_empty_command_is_not_a_missing_runtime():
    """缺 `command` 是配置写坏了，不是"你没装东西" —— 交给真实的连接去报。"""
    assert MGR.missing_runtime({"transport": "stdio"}) == ""
    assert MGR.missing_runtime({"command": "   "}) == ""
    print("  [ok] 空 command 不冒充缺环境")


# ----------------------------------------------------------------------
# 2. 文案
# ----------------------------------------------------------------------

def test_the_reason_never_says_incompatible():
    """这一条的存在理由：这句话不许再把人往"插件坏了"上带。"""
    reason = MGR.missing_runtime_reason("files", "npx")
    for bad in ("不兼容", "不支持", "已屏蔽", "已被 Crannia 屏蔽"):
        assert bad not in reason, "文案里出现了 %r：%s" % (bad, reason)
    assert "npx" in reason, "没点名缺的是哪个命令：%s" % reason
    assert "Node.js" in reason, "没说清该装什么：%s" % reason
    print("  [ok] 文案点名缺什么、装什么，且不说不兼容")


def test_the_reason_names_the_server():
    """一个批次里可能有好几条，文案必须认得出是哪一条。"""
    reason = MGR.missing_runtime_reason("my-files", "uvx")
    assert "my-files" in reason and "uvx" in reason, reason
    assert "uv" in reason, "没提 uv（装什么说不清）：%s" % reason
    print("  [ok] 文案带服务器名")


# ----------------------------------------------------------------------
# 3. 分类：缺环境有自己的码
# ----------------------------------------------------------------------

def test_the_code_is_its_own():
    """复用 `mcp_connect_failed`，界面分不出「没装 Node」与「服务器挂了」。"""
    assert MGR.MCP_MISSING_RUNTIME == "mcp_missing_runtime"
    assert MGR.MCP_MISSING_RUNTIME != "mcp_connect_failed"
    print("  [ok] 分类码独立")


def test_connect_external_skips_and_records(monkeypatch=None):
    """缺命令时不连、记的是缺环境那个码，而且 `detail` 是纯 ASCII。

    把 `quarantine` 整个换掉：真账本是持久的，测试往里写就是污染生产
    （`B14` 的形状）。这里只验"记了什么"，不验"记到哪"。
    """
    import bus.quarantine as Q

    seen = []
    real = Q.quarantine
    Q.quarantine = lambda owner, **kw: (seen.append((owner, kw)) or {"owner": owner, **kw})
    try:
        mgr = MGR.MCPManager()
        n = mgr.connect_external([{
            "name": "files", "transport": "stdio",
            "command": "crannia-no-such-binary-w269",
        }])
    finally:
        Q.quarantine = real

    assert n == 0, "缺环境时不该注册任何工具，实得 %r" % n
    assert len(seen) == 1, "应当记一条账，实得 %r" % (seen,)
    owner, kw = seen[0]
    assert owner == "mcp:files", owner
    assert kw.get("code") == MGR.MCP_MISSING_RUNTIME, kw
    assert kw.get("detail") == "command=crannia-no-such-binary-w269", kw.get("detail")
    # `detail` 必须纯 ASCII：界面从这一格抠命令名，再拼当前语言的句子。
    assert kw.get("detail", "").isascii(), "detail 里混进了非 ASCII：%r" % kw.get("detail")
    print("  [ok] 缺环境 -> 不连接、记自己的码、detail 是 ASCII")


def test_a_successful_connect_clears_the_old_entry():
    """修好就没救的死结：连上了必须把旧账划掉。

    账本是持久的、闸只看账本，用户装好 Node 再重启时若账还在，
    服务器连上了、工具却被全摘掉，而且不会再弹窗（`hits` 已经不是 1）。
    """
    import bus.quarantine as Q

    calls = []
    real = Q.release
    Q.release = lambda owner: calls.append(owner)
    try:
        MGR.MCPManager._clear("files")
    finally:
        Q.release = real
    # 今天账本里没有 `mcp:files`，不该无差别 release（那会把别人的账也划掉）
    assert calls == [], "没有这条账时不许 release，实得 %r" % (calls,)

    # 有账，必须 release 掉那一个
    real_entries = Q.all_entries
    Q.all_entries = lambda: {"mcp:files": {"owner": "mcp:files"}}
    calls.clear()
    Q.release = lambda owner: calls.append(owner)
    try:
        MGR.MCPManager._clear("files")
    finally:
        Q.all_entries = real_entries
        Q.release = real
    assert calls == ["mcp:files"], "连上了却没解封，实得 %r" % (calls,)
    print("  [ok] 连上 -> 划掉那一格旧账（没有就不动）")


# ----------------------------------------------------------------------
# 4. `/mcp` 命令同样分类
# ----------------------------------------------------------------------

def test_the_mcp_command_separates_the_two_cases():
    """`/mcp` 里未连接分两种：缺运行环境 vs 真连不上。

    把"你没装 Node"显示成"未连接"，用户会去查服务器、而不是去装东西。
    """
    import mcpx.manager as MM

    class _Fake:
        _external_servers = {}
        _tools_registered = 0

    real_get, real_cfg = MM.get_manager, MM._load_external_config
    MM.get_manager = lambda: _Fake()
    MM._load_external_config = lambda: [
        {"name": "files", "transport": "stdio",
         "command": "crannia-no-such-binary-w269"},
    ]
    try:
        from core.pipeline.stages.pre_process import _handle_mcp_command
        text = _handle_mcp_command()
    finally:
        MM.get_manager, MM._load_external_config = real_get, real_cfg

    assert "运行环境缺失" in text, "没分类成缺环境：\n%s" % text
    assert "crannia-no-such-binary-w269" in text, "没点名缺哪个命令：\n%s" % text
    assert "不兼容" not in text, "又说起不兼容了：\n%s" % text
    assert "已被" not in text, "又说起屏蔽了：\n%s" % text
    print("  [ok] /mcp 把「缺环境」与「连不上」分开说")


def _main():
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
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))
    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
