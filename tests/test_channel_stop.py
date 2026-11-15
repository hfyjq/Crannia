"""`POST /api/channel/stop` 与通道心跳读数（`core/channel_health`）的机械读者。

## 它钉的是哪一次现场

轮询循环停了，而 `/api/channel/status` 仍然报 `running: true`（那个 flag
只在 `_run_channel` 的 `finally` 里翻）。界面上没有停止按钮，用户唯一的
出路是把整个应用掐掉。

两条不变量：

  ① 停止必须能停住卡死的循环 —— 温和那段（投递 `terminate()`）对"卡在
     await 里"的循环是无效的，只有硬停（`loop.call_soon_threadsafe(loop.stop)`）
     救得了。所以第二条用例专门造一个永远不返回的 `run()`。
  ② 救不回来时不许假装停了 —— `status == "stuck"` 且 `running` 仍是 True：
     假装停了会让下一次 `start` 起第二条线程抢同一个 bot（Telegram 直接 409）。

ASCII-only 输出（Windows 控制台默认 GBK）。

用法：
    .venv/Scripts/python.exe tests/test_channel_stop.py
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import web.server as S  # noqa: E402


def _spawn(name: str, channel) -> threading.Thread:
    """照 `_run_channel` 的形状起一条线程（登记 loop / flag），便于 `_stop_channel` 找得到。"""
    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        S._channel_loops[name] = loop
        try:
            S._channel_running[name] = True
            loop.run_until_complete(channel.run())
        except Exception:                                        # noqa: BLE001
            pass                     # 硬停那一支就是靠 run_until_complete 抛出来的
        finally:
            S._channel_running[name] = False
            if S._channel_loops.get(name) is loop:
                S._channel_loops.pop(name, None)
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name=f"probe-{name}")
    t.start()
    for _ in range(100):
        if S._channel_loops.get(name) is not None and S._channel_running.get(name):
            break
        time.sleep(0.01)
    return t


def _with_fake_channel(channel):
    """上下文：把 `bus.registry.get_channel` 换成返回探针通道。"""
    class _Ctx:
        def __enter__(self_inner):
            import bus.registry as R
            self_inner.saved = R.get_channel
            R.get_channel = lambda cid: channel
            return channel

        def __exit__(self_inner, *exc):
            import bus.registry as R
            R.get_channel = self_inner.saved
            return False
    return _Ctx()


def _cleanup(name: str, thread: threading.Thread | None = None) -> None:
    S._channel_running.pop(name, None)
    S._channel_loops.pop(name, None)
    S._channel_threads.pop(name, None)
    if thread is not None:
        thread.join(timeout=5.0)


class _Cooperative:
    """正常通道：`run()` 等到 `terminate()` 置位就退出。"""

    def __init__(self) -> None:
        self._stop = None
        self.terminated = False

    async def run(self) -> None:
        self._stop = asyncio.Event()
        await self._stop.wait()

    async def terminate(self) -> None:
        self.terminated = True
        if self._stop is not None:
            self._stop.set()


class _Wedged:
    """卡死的通道：`run()` 永远不返回，`terminate()` 也改变不了它。

 这正是那个现场的形状（循环不动、又没人能停）。    """

    async def run(self) -> None:
        await asyncio.sleep(3600)

    async def terminate(self) -> None:
        return None


# ----------------------------------------------------------------------

def test_stop_is_not_running_is_a_no_op():
    """没在跑，`not_running`，不碰任何东西。"""
    r = S._stop_channel("probe_absent_channel")
    assert r["ok"] is True and r["status"] == "not_running", r
    print("  [ok] 没在跑：not_running")


def test_graceful_stop_ends_the_loop():
    """温和：`terminate()` 一投递，`run()` 就返回，flag 跟着翻。"""
    name = "probe_cooperative_channel"
    ch = _Cooperative()
    with _with_fake_channel(ch):
        t = _spawn(name, ch)
        r = S._stop_channel(name, wait_s=3.0, hard_wait_s=1.0)
        t.join(timeout=5.0)
    try:
        assert r["ok"] is True and r["status"] == "stopped", r
        assert ch.terminated is True, "terminate() 没被投递到它自己的循环上"
        assert S._channel_running.get(name) is False, "flag 没翻（界面会一直显示运行中）"
        assert not t.is_alive(), "线程没退"
    finally:
        _cleanup(name, t)
    print("  [ok] 温和停止：run() 退出、flag 翻、线程退")


def test_hard_stop_rescues_a_wedged_loop():
    """卡死的循环：只有硬停救得了（这一条就是那个现场）。"""
    name = "probe_wedged_channel"
    ch = _Wedged()
    with _with_fake_channel(ch):
        t = _spawn(name, ch)
        r = S._stop_channel(name, wait_s=0.6, hard_wait_s=3.0)
        t.join(timeout=5.0)
    try:
        assert r["ok"] is True, r
        assert r["status"] == "forced", r
        assert S._channel_running.get(name) is False, "硬停之后 flag 还亮着"
        assert not t.is_alive(), "硬停之后线程还在（那会抢同一个 bot）"
    finally:
        _cleanup(name, t)
    print("  [ok] 卡死的循环被硬停住（forced），线程真的退了")


def test_stuck_is_reported_not_faked():
    """救不回来，如实说 `stuck`，且不动 flag（假装停了会起第二条线程）。"""
    name = "probe_stuck_channel"

    class _DeadLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, *a, **k):
            raise RuntimeError("loop 死了")

    S._channel_running[name] = True
    S._channel_loops[name] = _DeadLoop()
    try:
        r = S._stop_channel(name, wait_s=0.1, hard_wait_s=0.2)
        assert r["ok"] is False and r["status"] == "stuck", r
        assert S._channel_running.get(name) is True, (
            "停不下来却把 flag 翻成 False —— 下一次 start 会起第二条线程抢同一个 bot")
    finally:
        _cleanup(name)
    print("  [ok] 停不下来时如实报 stuck，flag 不动")


def test_heartbeat_is_recorded_and_exposed():
    """「在跑」与「真的在收」要分得开：状态端点带 `last_event`。"""
    from core import channel_health as CH
    CH.clear()
    snap0 = CH.snapshot()
    assert snap0 == {}, snap0           # 没收到过 = 不出现（不是 0 秒）

    CH.note("telegram")
    snap = CH.snapshot()
    assert "telegram" in snap, snap
    assert 0.0 <= snap["telegram"]["seconds_since"] < 5.0, snap

    # 端点必须把它带出来（用真 Flask 测试客户端 + 真令牌）
    c = S.app.test_client()
    resp = c.get("/api/channel/status",
                 headers={"X-Crannia-Token": S._LAUNCH_TOKEN})
    assert resp.status_code == 200, resp.status_code
    body = resp.get_json()
    assert "last_event" in body, body
    assert "telegram" in body["last_event"], body["last_event"]
    CH.clear()
    print("  [ok] 心跳按通道记，且状态端点带得出来")


def test_stop_route_is_wired_and_refuses_bad_input():
    """端点本身：要令牌、要 channel_id。"""
    c = S.app.test_client()
    assert c.post("/api/channel/stop", json={"channel_id": "x"}).status_code == 403
    resp = c.post("/api/channel/stop", json={},
                  headers={"X-Crannia-Token": S._LAUNCH_TOKEN})
    assert resp.status_code == 400, resp.status_code
    print("  [ok] 端点要令牌、要 channel_id")


def test_ui_has_a_stop_control():
    """界面上真的有一颗停止（2026-10-25：压根没有）。"""
    ui = (_PROJECT_ROOT / "packages" / "ui" / "src" / "Channels.jsx").read_text(
        encoding="utf-8")
    assert "/api/channel/stop" in ui, "前端没接停止端点"
    assert "stopChannel(" in ui, "没有 stopChannel"
    assert ui.count("stopChannel(") >= 3, (
        "停止入口少于三处（卡片 ×2 + 弹窗）—— 用户还是得点开卡片才能停")
    # 位置：在卡的右上角，不是卡片脚下。
    assert "ch-stop-btn" in ui, "停止按钮没有用 `.ch-stop-btn`（位置那条样式靠它）"
    css = (_PROJECT_ROOT / "packages" / "ui" / "src" / "styles.css").read_text(
        encoding="utf-8")
    assert ".ch-stop-btn{position:absolute" in css.replace("\n", ""), (
        "`.ch-stop-btn` 不是绝对定位 —— 它会被排进卡片正文，而不是右上角")
    assert "ch-card-foot" in ui
    print("  [ok] 卡片与弹窗都有停止入口（卡片那颗在右上角）")


def test_ui_has_a_start_control_for_wechat():
    """微信卡片也有「启动通道」（「停止启动都做」）。

    以前 `def.kind !== "wechat"` 把它整条排除，停掉之后只能去扫卡片
    才能再起来（那颗按钮写着「重新扫码登录」，听着像要重扫，其实有缓存凭据时
    只是把 `run()` 拉起来）。判据没放宽：没登录不许点，标题说清先做什么。
    """
    ui = (_PROJECT_ROOT / "packages" / "ui" / "src" / "Channels.jsx").read_text(
        encoding="utf-8")
    #: 按旧的那整条表达式判，不按单个片段 —— 注释里提到它是允许的
    #:   （`AST` 那一套在这里用不上：这是 JSX 文本）。
    assert 'def.kind !== "cli" && def.kind !== "wechat"' not in ui,         "微信又被排除在启动/停止之外了"
    assert "先扫码登录" in ui, "没登录时那颗按钮的说明没写（用户不知道该先做什么）"
    assert "启动通道" in ui and "停止通道" in ui
    print("  [ok] 微信也有启动/停止，且没登录时说清要先扫码")


def test_ui_has_a_start_on_every_card():
    """每张卡的右下角是「启动」。

    那里原来是一颗「运行中」的字 —— 与卡里那行状态（`.ch-status`）重复，
      而那个位置更该放动作。运行中时这里不出现：停止在右上角那颗 ⏹。
    """
    ui = (_PROJECT_ROOT / "packages" / "ui" / "src" / "Channels.jsx").read_text(
        encoding="utf-8")
    assert "ch-start-btn" in ui, "卡片上没有启动按钮"
    assert ui.count("startChannelFor(") >= 3, (
        "启动入口少于三处（定义 + 主卡 + 附加卡）")
    assert "ch-run-tag" not in ui, "「运行中」那颗字还在（与卡里那行状态重复）"
    css = (_PROJECT_ROOT / "packages" / "ui" / "src" / "styles.css").read_text(
        encoding="utf-8")
    assert ".ch-start-btn{margin-left:auto" in "".join(css.split()), (
        "`.ch-start-btn` 没顶到右下角")
    print("  [ok] 每张卡右下角都是启动（运行中那颗字已去掉）")


def test_wechat_start_refuses_without_login():
    """微信走通用启动路之前必须有缓存凭据。

    没有凭据时 `WeChatChannel.run()` 会阻塞在 `do_login()` 的扫码上，而这条路上
    没有扫码回调，线程挂在一个谁也看不见的二维码上，界面只会一直"启动中"。
    """

    class _NoCreds:
        def __init__(self, path):
            self._credential_path = path

    c = S.app.test_client()
    hdr = {"X-Crannia-Token": S._LAUNCH_TOKEN}
    old_running = S._FRAMEWORK_RUNNING
    tmp = None
    try:
        import tempfile as _tf
        tmp = Path(_tf.mkdtemp(prefix="wxcred-"))
        S._FRAMEWORK_RUNNING = True
        S._channel_running.pop("wechat_channel", None)
        with _with_fake_channel(_NoCreds(str(tmp / "nope.json"))):
            r = c.post("/api/channel/start",
                       json={"channel_id": "wechat_channel"}, headers=hdr)
        body = r.get_json() or {}
        assert r.status_code == 400, (r.status_code, body)
        assert "扫码" in str(body.get("error") or ""), body
        print("  [ok] 没登录 -> 拒绝，并把用户指回去扫码")
    finally:
        S._FRAMEWORK_RUNNING = old_running
        if tmp is not None:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        S._channel_running.pop("wechat_channel", None)


# ----------------------------------------------------------------------

def _main() -> int:
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
        except Exception as exc:                                 # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))
    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
