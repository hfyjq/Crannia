"""记忆巩固的机械读者 —— 闸序 / 互斥与状态 / 巩固模式 / 事件。

钉住落地时的每一条承重件：

  · 闸序（开关 → 在飞 → 时间闸 → 扫描节流 → 会话闸 → 取互斥），每道一条用例；
    扫描节流卡在时间闸与会话闸之间：时间闸过了而会话闸没过时，"上次巩固时刻"
    不前进，时间闸每轮都会通过，不节流就等于每轮全扫一遍会话目录。
    反向也要钉住：时间闸没过时不该消耗节流。
  · 会话闸排除当前会话（它的 mtime 必然很新）；
  · 互斥与状态是两格：互斥是目录（`mkdir` 原子），状态是一格显式字段。
    陈旧线 1 小时（PID 活着也算陈旧，PID 复用护栏）· 死 PID / 读不懂的 holder 回收 ·
    取到之后重读校验 · 刚建还没写 holder 时让开 · 还互斥不推时间；
  · 巩固模式：同一对只读工具在 fork 期间多认 `logs/…` 与
    `sessions/…`，且越界就地拒；模式关掉时行为一个字节不变；
  · fork 形状：`_tool_allowlist` 执行侧拒 · `tools=` 表不摘（缓存判据）·
    占并发名额（巩固是第 4 个会跑的后台活）；
  · 失败：出声 + 还互斥 + 不盖"上次巩固时刻"（下一轮自然重攒 24 小时）；
  · 事件：`memory.organized` 与 `memory.saved` 分开（`__all__` + `DURABLE_TYPES`）。

全程 `sandbox()`（chdir 临时目录），锁文件 / 会话笔记 / 日报 / settings 全落临时
目录，绝不碰生产的 `data/memory/`。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_organize.py
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from _sandbox import sandbox  # noqa: E402

import core.memory_organize as MO  # noqa: E402
from core import request_snapshot  # noqa: E402

_SESS = "probe:memorganize"

_ROOT = Path("data/memory")
_LOGS = _ROOT / "logs"
_SESSIONS = _ROOT / "sessions"


# ----------------------------------------------------------------------
# 夹具
# ----------------------------------------------------------------------

class _FakeProvider:
    """脚本化的同步 provider（形状与 `test_memory_extract._FakeProvider` 一致）。"""

    _model = "probe-model"

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.calls = []

    def chat(self, messages, tools, system=None, thinking=None):
        self.calls.append({"messages": list(messages), "tools": tools,
                           "system": system})
        step = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if isinstance(step, Exception):
            raise step
        return types.SimpleNamespace(
            content=step.get("content", ""),
            tool_calls=step.get("tool_calls"),
            total_tokens=step.get("tokens", 5),
            reasoning_content=None,
        )


class _SpawnRecorder:
    def __init__(self, run=False):
        self.calls = []
        self.run = run

    def __call__(self, *, target, kwargs):
        self.calls.append(kwargs)
        if self.run:
            target(**kwargs)
        return None


class _Ctx:
    """把 `MO._spawn` 换成记录器 + 清进程级两格（跨用例串味会把断言变成假绿）。"""

    def __init__(self, run=False):
        self.rec = _SpawnRecorder(run=run)

    def __enter__(self):
        self._old = MO._spawn
        MO._spawn = self.rec
        MO._state["last_scan_at"] = 0.0
        MO._state["in_progress"] = False
        from core.cancellation import clear
        clear(_SESS)
        return self.rec

    def __exit__(self, *exc):
        MO._spawn = self._old
        return False


def _write_settings(cfg: dict) -> None:
    p = Path("data/settings.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _write_state(*, age_s: float = 0.0) -> None:
    """造一份"上次成功巩固的时刻"：`last_round = now - age_s`。"""
    root = MO.state_root(_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    (root / MO.STATE_FILE).write_text(
        json.dumps({"last_round": time.time() - age_s}), encoding="utf-8")


def _hold(*, age_s: float = 0.0, pid=None) -> Path:
    """造一把被别人持有的互斥：holder = `<pid> <since>`，since = now - age_s。"""
    d = MO.lock_path(_ROOT)
    d.mkdir(parents=True, exist_ok=True)
    (d / MO.HOLDER_FILE).write_text(
        "%d %.6f" % (os.getpid() if pid is None else pid, time.time() - age_s),
        encoding="utf-8")
    return d


def _make_sessions(n: int, skip=()) -> None:
    _SESSIONS.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        sid = "sess-%02d" % i
        if sid in skip:
            continue
        (_SESSIONS / (sid + ".md")).write_text("笔记", encoding="utf-8")


def _make_logs(n: int = 3) -> None:
    _LOGS.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (_LOGS / ("2026-09-%02d.md" % (i + 1))).write_text("一轮一段", encoding="utf-8")


def _snapshot(session_id=_SESS, tools=None):
    snap = request_snapshot.capture(
        payload=[{"role": "user", "content": "hi"}],
        tools=tools if tools is not None else [{"name": "memory_write"}],
        system="PROBE-SYSTEM", model="probe-model", session_id=session_id)
    request_snapshot.set_current(snap)
    return snap


def _msgs():
    return [{"role": "user", "content": "聊点什么"},
            {"role": "assistant", "content": "好的"}]


def _mw_call(name, cid="call_1"):
    return {"id": cid, "type": "function", "function": {
        "name": "memory_write",
        "arguments": json.dumps(
            {"name": name, "description": "一句钩子", "content": "正文"},
            ensure_ascii=False)}}


def _write_current_note() -> None:
    """当前会话的笔记 —— 走真的写者（`SessionMemory.save`），因为文件名会被
    洗（`:` → `_`）。手工写 `probe:memorganize.md` 在 Windows 上根本不是
    一个普通文件（冒号 = NTFS 备用数据流，目录里只出现 `probe`，
    `glob("*.md")` 看不见它）—— 那样写的夹具是假的。"""
    from core.memory.session_memory import SessionMemory
    SessionMemory(sessions_dir=str(_SESSIONS)).save(_SESS, "当前会话的笔记")


def _ready(*, age_s=25 * 3600, n_sessions=5, logs=3):
    """把"除被测那道闸之外全部就绪"的条件摆好。"""
    _write_settings({"memory_write": True, "memory_organize": True})
    _make_sessions(n_sessions)
    _write_current_note()
    _make_logs(logs)
    if age_s is not None:
        _write_state(age_s=age_s)
    _snapshot()


def _fire(rec, **kw):
    args = dict(session_id=_SESS, messages=_msgs(), provider=object(),
                memory_root=_ROOT, logs_dir=_LOGS, sessions_dir=_SESSIONS)
    args.update(kw)
    MO.maybe_organize(**args)
    return rec.calls


def _unthrottle():
    """把扫描节流让开 —— 同一用例里连跑两次时要显式做一次。

    这不是"绕过被测闸"：节流本身有它自己的用例
    （`test_gate_scan_throttle_after_time_gate`）；其余用例调它是为了隔离，
    否则第二次调用会被第一次留下的节流挡住，断言的其实是节流。
    """
    MO._state["last_scan_at"] = 0.0


# ----------------------------------------------------------------------
# 1. 闸序 —— 每道一条
# ----------------------------------------------------------------------

def test_gate_switch_off_stops_everything():
    """① 整理开关关，什么都不发生。"""
    with sandbox(), _Ctx() as rec:
        _ready()
        _write_settings({"memory_organize": False})
        assert _fire(rec) == [], "整理开关关着还去 fork 了"


def test_gate_write_switch_off_also_stops_it():
    """①b 写记忆总闸关，整理也没意义（判据：总闸关时直接返回 false）。

    它写不进任何一个字节（`memory_store.write_entry` 执行侧拒），跑了纯烧钱。
    """
    with sandbox(), _Ctx() as rec:
        _ready()
        _write_settings({"memory_organize": True, "memory_write": False})
        assert _fire(rec) == [], "写记忆关着还跑了整理"


def test_gate_in_flight_skips():
    """② 在飞，直接返回。"""
    with sandbox(), _Ctx() as rec:
        _ready()
        MO._state["in_progress"] = True
        assert _fire(rec) == []
        assert MO._state["last_scan_at"] == 0.0, "在飞跳过不该消耗扫描节流"


def test_gate_time_gate_blocks_within_min_hours():
    """③ 距上次整理不足 24 小时，不跑。"""
    with sandbox(), _Ctx() as rec:
        _ready(age_s=23 * 3600)
        assert _fire(rec) == [], "时间闸没拦住"
        _fire(rec)  # 再来一次也一样
        assert rec.calls == []


def test_time_gate_failure_does_not_consume_the_scan_throttle():
    """③↔④ 的方向：时间闸没过时不该碰扫描节流。

    反了的话，一个 24 小时里没到点的会话每轮都把节流消耗掉 —— 等真到点时
    反而被自己刚写下的节流挡住（把节流放在时间闸之后正是这个理由）。
    """
    with sandbox(), _Ctx() as rec:
        _ready(age_s=3600)          # 只过了 1 小时
        _fire(rec)
        assert MO._state["last_scan_at"] == 0.0, \
            "时间闸没过却消耗了扫描节流（顺序反了）"


def test_gate_scan_throttle_after_time_gate():
    """④ 时间闸过了但十分钟内扫过，跳过（否则每轮都全扫一遍会话目录）。"""
    with sandbox(), _Ctx() as rec:
        _ready()
        MO._state["last_scan_at"] = time.time()
        assert _fire(rec) == [], "扫描节流没拦住"
        # 节流过了十年，同样的条件就该跑
        MO._state["last_scan_at"] = time.time() - MO.SCAN_INTERVAL_MS / 1000.0 - 1
        assert len(_fire(rec)) == 1, "节流过期后仍不跑"


def test_gate_session_count_needs_five():
    """⑤ 会话数不足 5，不跑；够了，跑。"""
    with sandbox(), _Ctx() as rec:
        _ready(n_sessions=4)
        assert _fire(rec) == [], "4 个会话就跑了"
        _make_sessions(5)
        _unthrottle()
        assert len(_fire(rec)) == 1


def test_gate_excludes_current_session():
    """⑤b 排除当前会话（它的 mtime 必然很新）。

    形状：4 个别的会话 + 当前会话自己的笔记（`_ready` 会写），只数到 4，不跑。
    """
    with sandbox(), _Ctx() as rec:
        _ready(n_sessions=4)
        assert _fire(rec) == [], "把当前会话也数进会话闸了"
        # 反证：当前会话被排除之后，正好第 5 个"别的会话"就该放行
        (_SESSIONS / "sess-99.md").write_text("另一个", encoding="utf-8")
        _unthrottle()
        assert len(_fire(rec)) == 1


def test_sessions_to_review_normalizes_the_current_session():
    """比的必须是归一化之后的名字。

    会话笔记的文件名是洗过的（`workspace:private` → `workspace_private.md`），
    而 `session_id` 是原始的，拿原始值去比磁盘上的 stem，永远不相等，
    于是当前会话永远排除不掉（而它恰恰是最容易被算进去的那个 —— 它的笔记刚写）。
    """
    from core.memory.session_memory import SessionMemory
    with sandbox():
        _write_current_note()
        stem = SessionMemory.note_stem(_SESS)
        assert stem != _SESS, "夹具坏了：这个会话号没被洗过（用例在失去靶子）"
        assert (_SESSIONS / (stem + ".md")).is_file(), "当前会话的笔记没落盘"
        assert MO.sessions_to_review(_SESSIONS, 0.0, _SESS) == [], \
            "当前会话没被排除（拿原始 session_id 比磁盘 stem 的经典翻车）"
        _make_sessions(2)
        assert MO.sessions_to_review(_SESSIONS, 0.0, _SESS) == ["sess-00", "sess-01"]


def test_gate_lock_held_by_live_pid_blocks():
    """⑥ 互斥被活着的 PID 占着（且没陈旧），不跑。"""
    with sandbox(), _Ctx() as rec:
        _ready()
        _hold(age_s=0, pid=os.getpid())
        assert _fire(rec) == [], "互斥被占还跑了"


def test_gate_no_snapshot_returns_the_mutex():
    """⑥b 拿不到请求快照：跳过，把互斥还回去，而且不盖"上次巩固时刻"。

    不还互斥的话，下一次要等陈旧线；盖了时刻的话，这一次"缺输入"
    会变成 24 小时的静默 —— 两件都不对。
    """
    with sandbox(), _Ctx() as rec:
        _ready(age_s=0)
        before = MO.last_round_at(_ROOT)
        request_snapshot.clear_current()
        assert _fire(rec) == []
        assert not MO.lock_path(_ROOT).exists(), "互斥没还回去"
        assert MO.last_round_at(_ROOT) == before, "没跑成却盖了上次巩固时刻"


# ----------------------------------------------------------------------
# 2. 互斥与状态（纯文件操作）
# ----------------------------------------------------------------------

def test_last_round_at_absent_is_zero():
    with sandbox():
        assert MO.last_round_at(_ROOT) == 0.0


def test_hold_and_release_semantics():
    """取到之后 holder 里是本进程；第二次拿不到；还回去之后又能取。"""
    with sandbox():
        assert MO.hold_round(_ROOT) is True
        d = MO.lock_path(_ROOT)
        parts = (d / MO.HOLDER_FILE).read_text(encoding="utf-8").split()
        assert parts[0] == str(os.getpid()), "holder 里不是本进程"
        assert MO.hold_round(_ROOT) is False, "同一进程第二次也拿到了（互斥失效）"
        MO.release_round(_ROOT)
        assert not d.exists(), "还回去之后目录还在"
        assert MO.hold_round(_ROOT) is True, "还回去之后取不回来"
        MO.release_round(_ROOT)


def test_hold_reclaims_stale_even_with_live_pid():
    """陈旧线是绝对的：PID 活着但 since 超过 1 小时，回收（PID 复用护栏）。"""
    with sandbox():
        _hold(age_s=MO.STALE_MS / 1000.0 + 60, pid=os.getpid())
        assert MO.hold_round(_ROOT) is True, \
            "陈旧互斥没被回收（PID 复用护栏失效 -> 巩固可能永远跑不起来）"
        MO.release_round(_ROOT)


def test_hold_reclaims_dead_pid():
    """holder 是个死 PID，回收（哪怕刚建 —— 进程崩了的那条路）。"""
    with sandbox():
        old = MO._pid_running
        MO._pid_running = lambda pid: False       # 「这个 PID 死了」
        try:
            _hold(age_s=0, pid=424242)
            assert MO.hold_round(_ROOT) is True, "死 PID 的互斥没被回收"
            MO.release_round(_ROOT)
        finally:
            MO._pid_running = old


def test_hold_reclaims_unparseable_holder():
    """holder 读不懂、而这个目录又不像"刚建还没写" ，所以 当没有活持有者。"""
    with sandbox():
        d = _hold(age_s=0)
        (d / MO.HOLDER_FILE).write_text("这不是个 PID", encoding="utf-8")
        stale = time.time() - 600
        os.utime(d, (stale, stale))       # 假装它已经建了一会儿
        assert MO.hold_round(_ROOT) is True, "读不懂的 holder 该当'没有活持有者'"
        MO.release_round(_ROOT)


def test_fresh_lockdir_without_holder_backs_off():
    """别人刚 mkdir 完、还没写 holder ，所以 让开，不去抢那两秒的窗口。"""
    with sandbox():
        MO.lock_path(_ROOT).mkdir(parents=True, exist_ok=True)
        assert MO.hold_round(_ROOT) is False, "把别人刚建的互斥抢走了"


def test_release_does_not_touch_the_round_marker():
    """还互斥 ≠ 推时间 —— 这两个数分家，是这一版能去掉"回滚时间戳"的原因。"""
    with sandbox():
        MO.mark_round_done(_ROOT)
        first = MO.last_round_at(_ROOT)
        assert first > 0
        assert MO.hold_round(_ROOT) is True
        MO.release_round(_ROOT)
        assert MO.last_round_at(_ROOT) == first, "还互斥的时候把时刻也动了"


def test_source_never_uses_os_kill_zero():
    """Windows 地雷：`os.kill(pid, 0)` 在 Windows 上等于 `TerminateProcess`
    （Python 文档逐字），会把别人的进程杀掉。这条是源级读者。"""
    src = (_PROJECT_ROOT / "core" / "memory_organize.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "kill" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
            raise AssertionError("core/memory_organize.py 里出现了 os.kill（Windows 上会杀进程）")


# ----------------------------------------------------------------------
# 3. 巩固模式 —— 同一对工具、只读放宽
# ----------------------------------------------------------------------

def test_organize_mode_reads_logs_and_sessions_only_inside_the_mode():
    from core import memory_store as MS
    with sandbox():
        _make_logs(1)
        (_LOGS / "2026-09-01.md").write_text("一天的事", encoding="utf-8")
        _make_sessions(1)
        # 关着：那两个名字不是记忆条目（走到 facts 分支）
        out = MS.memory_read("logs/2026-09-01")
        assert "没有" in out and "日报" not in out.split("——")[0], \
            "模式关着时 logs/… 也被当素材读了（主线行为被改了）"
        with MS.organize_read_mode(_LOGS, _SESSIONS):
            assert "一天的事" in MS.memory_read("logs/2026-09-01")
            assert "一天的事" in MS.memory_read("logs/2026-09-01.md"), "带 .md 后缀也该认"
            assert "笔记" in MS.memory_read("sessions/sess-00")
        # 出来之后回到主线行为
        assert "一天的事" not in MS.memory_read("logs/2026-09-01")


def test_organize_mode_rejects_traversal():
    """越界判据是解析出来的结果、不是名字里有没有 `..`。

    两组靶子打两条不同的路：
      · 带分隔符的（`logs/../secret`）—— 被正则挡住（段内不许有 `/` `\\`）；
      · 带盘符的（`logs/C:.md`）—— 正则看不出来（`:` 不是分隔符），而 `pathlib`
        里带盘符的右操作数会整个替掉左操作数，只有"解析后仍在根下"拦得住。
        它的落点是 `C:\\.md`（驱动器当前目录，不是进程 cwd）—— 测试里
        造不出那个靶文件，所以这一条直接断言判据本身（返回 `None` = 不归它管）。
    """
    from core import memory_store as MS
    with sandbox():
        (_ROOT.parent / "secret.md").write_text("SECRET", encoding="utf-8")
        with MS.organize_read_mode(_LOGS, _SESSIONS):
            for bad in ("logs/../secret", "logs/..", "sessions/../../etc/passwd",
                        "logs/a/b", "../secret", "C:/secret"):
                out = MS.memory_read(bad)
                assert "SECRET" not in out, "越界名字读出去了：%r" % (bad,)
            # 靶子存在的证明：带盘符的右操作数真的会把左操作数整个换掉
            if os.name == "nt":
                assert str(_LOGS / "C:.md") == "C:.md", (
                    "夹具坏了：带盘符的右操作数没有替掉左操作数（用例在失去靶子）")
                assert MS._read_organize_source("logs/C:.md") is None, (
                    "带盘符的名字没被拦 —— 它会 resolve 到 C:\\.md（驱动器根）")
            # 反向：合法的名字在同一个模式下必须仍然读得到（不是"一律拒"）
            _LOGS.mkdir(parents=True, exist_ok=True)
            (_LOGS / "2026-09-01.md").write_text("一天的事", encoding="utf-8")
            assert "一天的事" in MS._read_organize_source("logs/2026-09-01")


def test_organize_mode_empty_search_lists_the_index():
    from core import memory_store as MS
    with sandbox():
        MS.write_entry("咖啡", "用户爱喝手冲", "手冲。")
        assert "没有给 query" in MS.memory_search("")
        with MS.organize_read_mode(_LOGS, _SESSIONS):
            out = MS.memory_search("")
            assert "咖啡" in out and "巩固模式" in out, "巩固模式的空查询该当 ls 用"


# ----------------------------------------------------------------------
# 4. 后台本体：fork 形状 / 失败回滚 / 事件 / 并发名额
# ----------------------------------------------------------------------

def _run_probe(provider, msgs=None, last_at=0.0, logs_dir=None):
    """把 `run_organize` 直接跑一遍（不走线程）。返回收到的 memory.organized 事件。"""
    from core import events as E
    got = []
    bus = E.EventBus()
    old = E._bus
    E.set_event_bus(bus)
    sub = bus.subscribe()
    try:
        MO.run_organize(session_id=_SESS,
                        messages=msgs if msgs is not None else _msgs(),
                        provider=provider, tools=[{"name": "memory_write"}],
                        system="SYS", memory_root=_ROOT,
                        logs_dir=logs_dir if logs_dir is not None else _LOGS,
                        sessions_dir=_SESSIONS, last_at=last_at)
        while True:
            try:
                ev = sub.q.get_nowait()
            except Exception:  # noqa: BLE001 —— 队列空了
                break
            if ev.type == E.EVT_MEMORY_ORGANIZED:
                got.append(ev)
    finally:
        E.set_event_bus(old)
        bus.close()
    return got


def test_run_happy_path_emits_event_and_marks_the_round():
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        _make_sessions(5)
        _make_logs(2)
        assert MO.hold_round(_ROOT) is True        # 互斥已取
        prov = _FakeProvider([{"tool_calls": [_mw_call("整理改过的")]},
                              {"content": "没什么可改的"}])
        got = _run_probe(prov, last_at=0.0)
        from core import memory_store as MS
        assert (MS.FACTS_ROOT / "workspace" / "整理改过的.md").is_file(), \
            "整理 fork 没真的写进记忆库"
        assert len(got) == 1 and got[0].data.get("count") == 1, got
        assert MO.last_round_at(_ROOT) > 0, \
            "成功了却没盖上次巩固时刻（下一轮会立刻再跑一次，白烧钱）"
        assert not MO.lock_path(_ROOT).exists(), "成功了却没还互斥（下次要等一小时）"
        assert MO._state["in_progress"] is False


def test_run_failure_releases_and_does_not_mark():
    with sandbox():
        _make_sessions(5)
        _make_logs(2)
        assert MO.hold_round(_ROOT) is True
        lines = []
        prov = _FakeProvider([RuntimeError("上游炸了")])
        MO.run_organize(session_id=_SESS, messages=_msgs(), provider=prov,
                        tools=[], system="SYS", memory_root=_ROOT,
                        logs_dir=_LOGS, sessions_dir=_SESSIONS,
                        last_at=0.0, status=lines.append)
        assert not MO.lock_path(_ROOT).exists(), "失败没还互斥（下次要等一小时）"
        assert MO.last_round_at(_ROOT) == 0.0, \
            "失败却盖了上次巩固时刻 —— 下一次要再等 24 小时"
        assert any("失败" in ln for ln in lines), "失败没有出声"
        assert MO._state["in_progress"] is False, "失败也必须清在飞标志"


def test_run_with_no_change_still_emits_the_event():
    """跑了、改了 0 条 也必须出声。

    不能反过来说"没改就不许发" —— 那条期望正是要治的病：只有 `changed`
    非空才发事件，「跑了没改」与「压根没跑」在 UI 上完全同形。
    实测：整理真跑了 27.6 秒、锁也取到了、改了 0 条，用户侧一片空白。

判据："有变化"是结果，"跑过了"是事实 —— 两件事都得说。
    事件一律发，`count=0` 照发（前端那条回执块渲染成「记忆巩固：改动 0 条」，
    而那条回执本身就是"它跑过"的证据）。
    """
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        _make_sessions(5)
        _make_logs(1)
        prov = _FakeProvider([{"content": "记忆已经很紧凑"}])
        got = _run_probe(prov)
        assert len(got) == 1, (
            "跑了却没出声 —— 「跑了没改」与「没跑」又同形了：%r" % (got,))
        assert got[0].data.get("count") == 0, (
            "没改动却报了个非零的改动数：%r" % (got[0].data,))


def test_run_uses_fork_shape():
    """fork 形状 = 同前缀三条：`tools`/`system` 取自快照、`messages` 是副本、
    白名单执行侧（`tools=` 照发父的整表 —— 摘表断缓存，不许摘）。

    这条走完整的 `maybe_organize`（同步化的 `_spawn`），因为 `tools`/`system`
    只在那一层才真的从快照取 —— 直接调 `run_organize` 的话是我自己喂进去的，
    断言会变成"我喂的东西等于我喂的东西"。
    """
    with sandbox():
        import bus.registry as R
        R.reset()
        R.register_memory_tools()
        # 一个不在白名单里的工具：装了、也"发现"了，但执行侧必须拒
        called = []
        R.register_tool("read_probe_a", lambda **kw: called.append(kw) or "ok",
                        {"description": "probe", "inputSchema": {"type": "object"}})
        R.note_discovered(_SESS, ["read_probe_a"])
        snap_tools = [{"name": "read_probe_a"}, {"name": "memory_write"}]
        prov = _FakeProvider([
            {"tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "read_probe_a", "arguments": "{}"}}]},
            {"content": "收尾"},
        ])
        with _Ctx(run=True) as rec:
            _write_settings({"memory_write": True, "memory_organize": True})
            _make_sessions(5)
            _make_logs(1)
            _write_state(age_s=25 * 3600)
            _snapshot(tools=snap_tools)
            MO.maybe_organize(session_id=_SESS, messages=_msgs(), provider=prov,
                              memory_root=_ROOT, logs_dir=_LOGS, sessions_dir=_SESSIONS)

        assert len(rec.calls) == 1, "整理没被调度（前面的闸坏了）"
        assert prov.calls, "fork 根本没发请求"
        assert prov.calls[0]["tools"] == snap_tools, \
            "tools= 不是快照那一份（摘/换表就在 tools 段断掉与父的缓存前缀）"
        assert prov.calls[0]["system"] == "PROBE-SYSTEM", "system 不是快照那份"
        assert prov.calls[0]["messages"][0]["content"] == "聊点什么", \
            "fork 的 messages 不是父对话的副本"
        assert called == [], "白名单外的工具被真的执行了"
        # 拒绝回执说清了原因（不是静默跳过 —— 静默 = 模型拿同样的调用再试一遍）
        assert "only these tools may run" in json.dumps(prov.calls[1]["messages"],
                                                        ensure_ascii=False)


def test_run_holds_a_concurrency_slot():
    """整理是第 4 个会跑的后台活，它要占并发上限 3 里的一个名额。"""
    with sandbox():
        import bus.registry as R
        from core.pipeline import scheduler as S
        R.reset()
        R.register_memory_tools()
        _make_sessions(5)
        before = S._turn_slots._value
        prov = _FakeProvider([{"content": "无改动"}])
        _run_probe(prov)
        assert S._turn_slots._value == before, \
            "整理跑完没把并发名额还回去（名额泄漏 -> 上限越来越小）"


def test_fork_max_turns_is_ours():
    """`max_turns` 是本仓库自己的护栏 —— 改它要同时改这里的期望值。"""
    assert MO.ORGANIZE_MAX_TURNS >= 5, "太小的话第一阶段的活都读不完"


# ----------------------------------------------------------------------
# 5. 素材清单 / 提示词 / 事件登记 / 挂点
# ----------------------------------------------------------------------

def test_list_sessions_and_logs():
    with sandbox():
        _make_sessions(3)
        assert sorted(MO.list_sessions_touched_since(_SESSIONS, 0.0)) == \
            ["sess-00", "sess-01", "sess-02"]
        assert MO.list_sessions_touched_since(_SESSIONS, time.time()) == [], \
            "since 在未来时不该有会话"
        assert MO.list_sessions_touched_since(Path("没有这个目录"), 0.0) == []
        _make_logs(3)
        got = MO.list_daily_logs(_LOGS)
        assert got == sorted(got, reverse=True), "日报该从新到旧"
        assert len(MO.list_daily_logs(Path("没有这个目录"))) == 0


def test_daily_log_list_is_capped():
    with sandbox():
        _LOGS.mkdir(parents=True, exist_ok=True)
        for i in range(MO.MAX_LOG_NAMES + 6):
            (_LOGS / ("2026-08-%02d.md" % (i + 1))).write_text("x", encoding="utf-8")
        assert len(MO.list_daily_logs(_LOGS)) == MO.MAX_LOG_NAMES, \
            "日报清单没有上界（列太多等于把'别通读'这句话作废）"


def test_prompt_carries_three_actions_and_our_two_divergences():
    """提示词按三类动作组织，且两处有意偏离必须在正文里说清。

    三类（搬运 / 合并 / 改写）是从我们的数据形状长出来的，不是阶段编号 ——
    有一条负对照钉着正文里不许再出现 `Phase` 这类分阶段标记。
    """
    p = MO.build_organize_prompt(sessions=["sess-00", "sess-01"],
                                 log_names=["2026-10-01"])
    for action in ("一、搬运", "二、合并", "三、改写"):
        assert action in p, "三类动作缺了 " + action
    assert "Phase" not in p, "提示词里还留着分阶段标记"
    assert "sessions/sess-00" in p and "logs/2026-10-01" in p, "素材清单没进提示词"
    # 两处有意偏离必须在正文里说清（不能让模型自己猜）
    assert "索引是从条目现算的" in p, "没说清我们的索引是派生的（修剪的落点）"
    assert "没有删除工具" in p, "没说清巩固没有删除工具"
    assert "什么都没改就直说" in p, "缺少「允许什么都没改」这句"
    # 空素材也不许崩
    assert MO.build_organize_prompt(sessions=[], log_names=[])


def test_event_registered_and_separate_from_saved():
    from core import events as E
    assert "EVT_MEMORY_ORGANIZED" in E.__all__
    assert E.EVT_MEMORY_ORGANIZED == "memory.organized"
    assert E.EVT_MEMORY_ORGANIZED in E.DURABLE_TYPES, \
        "memory.organized 不落盘 ->「整理真的跑过」没有可数的记录"
    assert E.EVT_MEMORY_ORGANIZED != E.EVT_MEMORY_SAVED, \
        "两个类型塌成一个之后、频率差两个数量级的两种动作就没法从日志里分开数了"


def test_switch_reads_settings_with_default_on():
    """读不到 / 缺键 / 坏类型，默认开（整理是默认开启的能力）。"""
    with sandbox():
        assert MO.is_organize_enabled() is True, "没有 settings 时该是默认开"
        _write_settings({"memory_organize": False})
        assert MO.is_organize_enabled() is False
        for bad in ("true", 1, 0, None, []):
            _write_settings({"memory_organize": bad})
            assert MO.is_organize_enabled() is True, "脏值 %r 没回默认" % (bad,)
        _write_settings({"memory_write": False})
        assert MO.is_organize_enabled() is False, "写记忆关了整理还开着"


def test_record_turn_schedules_organize_once():
    """`MemoryManager.record_turn` 里对 `maybe_organize` 的调用恰好一处。

    同 `maybe_extract` 的那条：这类"复制块"事故行为用例碰不到，
    只有源级读者钉得住 —— 按 AST 数调用点。
    """
    src = (_PROJECT_ROOT / "core" / "memory" / "manager.py").read_text(encoding="utf-8")
    hits = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "record_turn":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                        and sub.func.id == "maybe_organize":
                    hits.append(sub.lineno)
    assert len(hits) == 1, (
        "record_turn 里 maybe_organize 的调用点有 %d 处（行 %r）—— 每轮只许"
        "调度一次" % (len(hits), hits))


def test_state_is_registered_in_the_registry():
    """进程级两格必须登记（登记表那台机检会双查）。"""
    from core import state_registry as SR
    wheres = {e.where for e in SR.PROCESS_SHARED}
    assert "core/memory_organize.py::_state" in wheres, \
        "整理的进程级状态没进登记表（census 会红，这里先给一条更近的报错）"


# ----------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------

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
