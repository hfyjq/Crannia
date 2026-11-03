#@exclusive: timing
# 这个文件按真实毫秒判超时（500ms / 1000ms 的档），
#   16 路并行时会被别的文件抢 CPU，偶发红（实测一次 10/11）。
#独占串行跑。判据在脚本里（`run_tests.py::_exclusive_group`）。
"""子作业的不变量：超时降级到后台，不再杀。

## 它治的是什么

若超时就直接 `_kill_tree()`，五秒的 `pip install`、三分钟的构建全部白干，
而模型只拿到一句"已被强制结束"，它会再跑一遍。

现在的行为：超时，不杀，挂到作业表上，用同一个工具带 `job_id` 回来查。

## 钉住的六件事

  1. 超时不再杀进程：`status="running"` + 有 `job_id`，且进程还活着
     （过一会儿能查到它跑完了，且 stdout 里有它打的那行）
  2. 正常完成的命令不进表（反向断言）—— 谁把"每条命令都建档"写进去，
     这一条会红（那会让作业表无界增长，且每条命令都要留一个进程句柄）
  3. 寿限兜底：到点真的杀（把寿限调成很小来测）—— 没有它，
     一条 `while true` 能活到重启
  4. 查一个不存在的 id，说清"没有这个作业"（≠"在跑"），并列出已知的
  5. 两个参数都没给，当场拒（不靠 schema 的 `required` —— 派发层不执行它，
     空命令会一路走到审批、白弹一次浮窗）
  6. `/clear` 清表（`purge` 是不变量，不是一串地方）
  7. 但清表 ≠ 停活，有活着的就出声（清了记账、活还在，
     而它的输出再没有人会来收 —— 不许静默）

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_jobs.py
"""

#@exclusive: prod-fingerprint
# ↑ 它会往 `data/runtime/tasks/` 里真写 —— 而 `test_ice_ui` 会在全路径上拍产物指纹，
#   并行时它看得到别人的中间态，假红（实测一次：报了
#   `data/runtime/tasks/test_jobs_probe/…`，而那个目录本来就会被自己清掉）。


import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only 输出 —— 控制台默认 GBK，emoji 会让整个运行崩掉。

_MARK = "JOBMARK_7f3a"
_SESS = "sess-jobs"


#: 审计日志落临时目录 —— 不许碰生产 `logs/`
#: （判据：测试自己进临时目录，不是"记得清"。
#: 照抄一份"从来没被创建过"的路径会侥幸干净；而这里真的会写
#: （跑的是真命令），必须指向临时目录。）
_TMP = None


def _real_guard_and_wm():
    """真 `SecurityGuard` + 真 `WorkspaceManager`（与 `test_shell` 同一套夹具）。"""
    global _TMP
    import tempfile
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager

    if _TMP is None:
        _TMP = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    guard = SecurityGuard(
        allowed_dirs=["./workspace"],
        command_blacklist=[],
        audit_log_path=str(Path(_TMP.name) / "audit_jobs_test.log"),
    )
    return guard, WorkspaceManager(guard, {})


def _install_wm(wm):
    import bus.registry as R
    old = R.get_workspace_manager()
    R.register_workspace_manager(wm)
    return lambda: R.register_workspace_manager(old) if old else None


def _granted_run(command, timeout_ms, wm):
    """预授权后真跑 —— 与 `test_shell.test_grant_then_execute` 同一条路。"""
    from core.shell import run_command
    wm._approval_scope = _SESS
    wm.grant_target_once("command", command, owner=_SESS)
    return run_command(command, timeout_ms=timeout_ms)


def _wait_status(job_id, want, seconds=15.0):
    """等作业到某个状态（带超时：等不到就断言失败，不许挂住测试）。"""
    from core import jobs
    deadline = time.monotonic() + seconds
    snap = None
    while time.monotonic() < deadline:
        snap = jobs.poll(job_id)
        if snap and snap["status"] == want:
            return snap
        time.sleep(0.2)
    return snap


# ------------------------------------------------------------------
# 1. 超时不杀，转后台
# ------------------------------------------------------------------

def test_timeout_demotes_to_background_instead_of_killing():
    """超时，转后台、进程还活着、最终跑完并留下输出。

    这一条是整块的核心：相反的形状是 进程被杀 + 输出丢掉 + 模型重跑一遍。
    """
    from core import jobs

    jobs.clear()
    _g, wm = _real_guard_and_wm()
    restore = _install_wm(wm)
    try:
        cmd = f'echo {_MARK}-start && sleep 3 && echo {_MARK}-done'
        r = _granted_run(cmd, timeout_ms=1000, wm=wm)
        assert r.get("timed_out") is True, f"没走超时那条路：{r}"
        assert r.get("status") == "running", f"超时后没转后台：{r}"
        job_id = r.get("job_id")
        assert job_id, f"没给 job_id：{r}"
        assert "job_id" in (r.get("note") or ""), (
            f"回执没告诉模型怎么回来查：{r.get('note')!r}")

        snap = jobs.poll(job_id)
        assert snap and snap["status"] == "running", (
            f"降级那一刻它就结束了？那这条测试没验到「后台」：{snap}")

        done = _wait_status(job_id, "done", seconds=20)
        assert done is not None and done["status"] == "done", (
            f"后台作业没跑完（或没被登记）：{done}")
        assert f"{_MARK}-done" in done.get("stdout", ""), (
            f"后台作业的输出丢了 —— 那降级就没意义了：{done.get('stdout')!r}")
        assert done.get("exit_code") == 0, f"exit_code 不对：{done}"
        print(f"  [ok] 超时转后台、进程活着、跑完留下输出（{done['elapsed_ms']}ms）")
    finally:
        restore()


def test_successful_commands_do_not_enter_the_job_table():
    """反向断言：正常完成的命令不进表 —— 防止"每条命令都建档"。"""
    from core import jobs

    jobs.clear()
    _g, wm = _real_guard_and_wm()
    restore = _install_wm(wm)
    try:
        r = _granted_run(f"echo {_MARK}-quick", timeout_ms=60000, wm=wm)
        assert r.get("success") is True and not r.get("timed_out"), f"正常命令没成功：{r}"
        assert jobs.list_jobs() == [], (
            f"正常完成的命令进了作业表 —— 表会无界增长：{jobs.list_jobs()}")
        assert "job_id" not in r, f"正常路径带上了 job_id：{r}"
        print("  [ok] 正常完成的命令不进表（反向断言）")
    finally:
        restore()


# ------------------------------------------------------------------
# 2. 寿限 / 查询 / 参数
# ------------------------------------------------------------------

def test_lifetime_cap_really_kills():
    """寿限兜底：到点真的杀（把寿限调到很小来测）。

    没有它，一条 `while true` 会活到框架重启 —— 而"框架持有执行体"买了
    "别白干"，没买"永远活着"。
    """
    from core import jobs

    jobs.clear()
    saved = jobs.JOB_MAX_LIFETIME_MS
    jobs.JOB_MAX_LIFETIME_MS = 1500          # 1.5 秒寿限
    _g, wm = _real_guard_and_wm()
    restore = _install_wm(wm)
    try:
        cmd = f"sleep 30 && echo {_MARK}-never"
        r = _granted_run(cmd, timeout_ms=500, wm=wm)
        job_id = r.get("job_id")
        assert job_id, f"没转后台：{r}"
        snap = _wait_status(job_id, "timeout", seconds=20)
        assert snap is not None and snap["status"] == "timeout", (
            f"寿限没生效（作业还活着？）：{snap}")
        assert f"{_MARK}-never" not in (snap.get("stdout") or ""), (
            "被杀掉的作业居然留下了它后面的输出")
        print(f"  [ok] 寿限到点真的杀（{snap['elapsed_ms']}ms 时结束）")
    finally:
        jobs.JOB_MAX_LIFETIME_MS = saved
        restore()


def test_unknown_job_id_says_so_and_lists_known():
    """查一个不存在的 id，说清没有（≠"在跑"），并列出已知的。"""
    from core import jobs
    from core.shell import run_command

    jobs.clear()
    r = run_command(job_id="job_deadbeef")
    assert r.get("success") is False, f"不存在的作业却报成功：{r}"
    assert "没有这个任务" in (r.get("error") or ""), f"没说清：{r}"
    assert "known_jobs" in r, f"没列出已知作业：{r}"
    print("  [ok] 不存在的 job_id 明说（且列出已知作业）")


def test_no_args_is_refused_locally_not_by_schema():
    """两个都没给，工具自己拒（不靠 schema 的 `required`：派发层不执行它）。"""
    from core.shell import run_command
    r = run_command()
    assert r.get("success") is False and "job_id" in (r.get("error") or ""), (
        f"空调用没被拒：{r}")
    print("  [ok] 空调用当场拒（不靠 schema）")


def test_clear_empties_the_table():
    """`/clear` 清表（`purge` 是不变量，不是一串地方）。"""
    from core import jobs
    from core.shell import COMMAND_TOOL_SCHEMA as S

    assert "job_id" in S["inputSchema"]["properties"], "schema 里没有 job_id"
    jobs.clear()
    assert jobs.list_jobs() == []
    print("  [ok] 清表可用（且 schema 上有 job_id）")


def test_clear_kills_background_tasks_and_says_so():
    """`/clear` 杀后台任务（不再"清账不杀"）。

    判据："清空"必须真的清空 —— 留一个看不见的活在后台跑，
    用户会以为干净了（「标签在说谎」同族）；杀了要说出来。
    """
    from core import jobs

    jobs.clear()
    assert jobs.running_count() == 0, "空表应当是 0"

    j = jobs.Job(id="probe-running", command="probe", pid=1,
                 started=time.monotonic(), out=jobs._Sink(), err=jobs._Sink(),
                 session_key="sess-a")
    jobs._register(j)
    assert jobs.running_count() == 1, "有一个 running 却没数出来"
    assert jobs.running_for("sess-a"), "按会话列不出来"
    print("  [ok] 在跑的会被数出来 / 按会话列得出来")

    n = jobs.kill_all("sess-a")
    assert n == 1, f"kill_all 报了 {n} 条（应当是 1）"
    assert jobs.get("probe-running").status == "killed", "杀完状态没变"
    assert jobs.running_for("sess-a") == [], "杀完还在 running 列表里"
    print("  [ok] kill_all 按会话杀 + 标 killed")

    jobs.clear()

    # 接线：`/clear` 那条路上真的调它，且真的把那句话说出来
    src = (Path(__file__).parent.parent / "core" / "pipeline" / "stages"
           / "pre_process.py").read_text(encoding="utf-8")
    assert "kill_all(" in src, "`/clear` 没有杀后台任务"
    assert "已停止" in src, "杀了却没有出声"
    print("  [ok] `/clear` 接线：kill_all + 出声")


def test_background_true_returns_a_task_and_writes_output():
    """`background=true`，立刻回一条回执（task id + 输出路径），完成时落盘。"""
    from core import jobs
    from core.shell import _run

    jobs.clear()
    r = _run("echo hello-bg; sleep 0.3; echo done-bg", Path.cwd(), 5000,
             background=True, description="probe",
             storage_id="test_jobs_probe")
    tid = r.get("task_id") or ""
    assert r.get("success") is True and r.get("status") == "running", r
    assert tid.startswith("c"), f"命令任务的 id 前缀不是 c：{tid!r}"

    snap = None
    for _ in range(50):
        snap = jobs.poll(tid)
        if snap and snap["status"] != "running":
            break
        time.sleep(0.1)
    assert snap and snap["status"] == "done", snap
    text = Path(r["output_file"]).read_text(encoding="utf-8")
    assert "hello-bg" in text and "done-bg" in text, text
    assert "CRANNIA_CWD" not in text, f"框架内部标记泄进了输出：{text!r}"
    jobs.clear()
    # 测试自己收拾：任务的输出目录是按 storage_id 分的，只删自己那一格。
    import shutil
    shutil.rmtree(Path("data/runtime/tasks/test_jobs_probe"), ignore_errors=True)
    print("  [ok] background=true：回执 · 落盘 · 摘掉 cwd 标记")


def test_request_background_targets_one_proc():
    """用户 Ctrl+B 的事件挂在具体 proc 上；没有前台进程时返回 0。"""
    from core import jobs

    class _P:
        pass

    p = _P()
    ev = jobs.note_foreground("sess-bg", p)
    assert ev is not None and not ev.is_set()
    assert jobs.request_background("sess-bg") == 1
    assert ev.is_set()
    jobs.clear_foreground("sess-bg", p)
    assert jobs.request_background("sess-bg") == 0, "摘掉前台登记后还能置事件"
    print("  [ok] request_background 只打当前那条前台进程")


def test_cancel_and_kill_all_by_session():
    """按任务取消 / 按会话杀；命令被取消时抑制完成通知。"""
    from core import jobs

    jobs.clear()
    a = jobs.Job(id="c-a", command="a", pid=1, started=time.monotonic(),
                 out=jobs._Sink(), err=jobs._Sink(), session_key="s1")
    b = jobs.Job(id="c-b", command="b", pid=1, started=time.monotonic(),
                 out=jobs._Sink(), err=jobs._Sink(), session_key="s2")
    jobs._register(a)
    jobs._register(b)

    assert jobs.cancel("c-a") is True
    assert a.status == "killed" and a.notified is True, (a.status, a.notified)
    assert jobs.cancel("c-a") is False, "已结束的任务还能再取消一次"
    assert jobs.kill_all("s2") == 1 and b.status == "killed", b.status
    assert jobs.running_for() == []
    jobs.clear()
    print("  [ok] cancel / kill_all：状态、抑制通知、按会话")


def test_sink_decodes_non_utf8_and_keeps_split_chars():
    """输出编码：分块不切坏字符；非 UTF-8 按本地代码页解，并报出编码名。"""
    from core import jobs

    s = jobs._Sink()
    payload = "中文输出\n".encode("utf-8")
    for i in range(0, len(payload), 2):          # 故意把多字节字符切开
        s.write(payload[i:i + 2])
    s.finish()
    assert s.text() == "中文输出\n", repr(s.text())
    assert chr(0xFFFD) not in s.text(), "分块解码产生了替换字符"
    assert s.encoding == "utf-8", s.encoding

    #: 这一档是"本地代码页"：钉住它，别钉机器。CI 的机器是 cp1252、本机是 cp936
    #: —— 直接喂 GBK 字节，结果会随机器变，而产品行为是对的。
    import locale as _locale
    _old_preferred = _locale.getpreferredencoding
    g = jobs._Sink()
    try:
        _locale.getpreferredencoding = lambda do_setlocale=True: "gbk"
        g.write("中文输出\n".encode("gbk"))
        g.finish()
    finally:
        _locale.getpreferredencoding = _old_preferred
    assert "中文输出" in g.text(), repr(g.text())
    assert g.encoding and g.encoding != "utf-8", g.encoding

    u = jobs._Sink()
    u.write(b"\xff\xfe" + "hello 中文".encode("utf-16-le"))
    u.finish()
    assert u.text().strip() == "hello 中文", repr(u.text())

    t = jobs._Sink()
    t.write(b"abc")                              # 不足嗅探长度、也没有换行
    t.finish()
    assert t.text() == "abc", repr(t.text())
    print("  [ok] 编码判定 + 增量解码 + 收尾（UTF-8 分块 / GBK / UTF-16LE BOM）")


# ------------------------------------------------------------------

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
