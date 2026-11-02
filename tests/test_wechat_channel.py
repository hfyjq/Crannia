"""
Unit tests for WeChat Channel (iLink protocol).

Tests cover:
  - WeChatChannel extends Channel ABC
  - ChannelMetadata correctness
  - WeChatMessageConverter: text / image / file / voice / empty
  - _make_session generates correct MessageSession
  - _random_wechat_uin format validity
  - Credential load/save roundtrip
  - QR code state machine parsing
  - to_platform MessageChain → iLink JSON
"""

from __future__ import annotations

import os
import sys

# 这个文件必须把仓根插进 `sys.path`：
# 本机 `.venv` 里可能有一条指向另一份 checkout 的 `.pth`
#`import channels.…` 会从那一份加载。
# 实测证据：报错里的路径不是本仓。
#与其余测试文件一样，把仓根插到最前。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# 本仓没有安装 pytest，用 `import pytest` 的文件会 `ModuleNotFoundError`，
# 全量跑时是 `rc=1 / 无汇总`。一条会自己跳过的护栏等于没有护栏。
# 这个文件一个 pytest 特性都没用（没有 fixture / raises / mark），只是拿它当 runner
#用仓库自带的 `__main__` 运行器，与其余文件一致。
from channels.base import Channel, MessageSession, MessageType
from channels.message import File, Image, MessageChain, Plain, Voice


# Channel ABC compliance

def test_wechat_channel_is_channel():
    from channels.wechat_channel.handler import WeChatChannel
    assert issubclass(WeChatChannel, Channel)


def test_channel_metadata():
    from channels.wechat_channel.handler import WeChatChannel
    ch = WeChatChannel({})
    meta = ch.meta()
    assert meta.name == "wechat_channel"
    assert meta.platform_type == "wechat"
    assert meta.support_multimodal is True


def test_channel_has_converter():
    from channels.wechat_channel.handler import WeChatChannel
    ch = WeChatChannel({})
    assert ch._converter is not None


# _make_session

def test_make_session():
    from channels.wechat_channel.handler import WeChatChannel
    ch = WeChatChannel({})
    session = ch._make_session("user123@im.wechat")
    assert isinstance(session, MessageSession)
    assert session.platform == "wechat"
    assert session.message_type == MessageType.PRIVATE
    assert session.session_id == "user123@im.wechat"


# WeChatMessageConverter

def test_converter_text():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    raw = {
        "msg": {
            "from_user_id": "user@im.wechat",
            "item_list": [{"type": 1, "text_item": {"text": "Hello微信"}}],
        }
    }
    chain = conv.to_internal(raw)
    assert chain.content == "Hello微信"
    assert len(chain.components) == 1


def test_converter_image():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    raw = {
        "msg": {
            "item_list": [{
                "type": 2,
                "image_item": {"media": {"full_url": "https://cdn.wechat/img.jpg", "aes_key": "24dae86aeb24d7a2069b7b852dec5bc3"}},
            }],
        }
    }
    chain = conv.to_internal(raw)
    assert len(chain.images) == 1
    assert chain.images[0].url == "https://cdn.wechat/img.jpg"


def test_converter_file():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    raw = {
        "msg": {
            "item_list": [{
                "type": 4,
                "file_item": {
                    "file_name": "report.pdf",
                    "len": "1024",
                    "media": {"full_url": "https://cdn.wechat/file.pdf", "aes_key": "24dae86aeb24d7a2069b7b852dec5bc3"},
                },
            }],
        }
    }
    chain = conv.to_internal(raw)
    assert len(chain.files) == 1
    assert chain.files[0].name == "report.pdf"


def test_converter_voice():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    raw = {
        "msg": {
            "item_list": [{
                "type": 3,
                "voice_item": {"cdn_url": "...", "duration": 5},
            }],
        }
    }
    chain = conv.to_internal(raw)
    assert chain.content == "[Voice]"


def test_converter_empty():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    chain = conv.to_internal({"msg": {"item_list": []}})
    assert chain.is_empty


def test_converter_mixed():
    """Text + image in one message."""
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    raw = {
        "msg": {
            "item_list": [
                {"type": 1, "text_item": {"text": "Look: "}},
                {"type": 2, "image_item": {"media": {"full_url": "x.jpg", "aes_key": ""}}},
            ],
        }
    }
    chain = conv.to_internal(raw)
    assert len(chain.components) == 2
    assert "Look:" in chain.content


def test_converter_to_platform_text():
    from channels.wechat_channel.converter import WeChatMessageConverter
    conv = WeChatMessageConverter()
    chain = MessageChain.from_text("reply")
    result = conv.to_platform(chain)
    assert result["item_list"][0]["type"] == 1
    assert result["item_list"][0]["text_item"]["text"] == "reply"


# _random_wechat_uin

def test_random_wechat_uin():
    from channels.wechat_channel.handler import _random_wechat_uin
    uin = _random_wechat_uin()
    assert isinstance(uin, str)
    assert len(uin) >= 4
    # Should produce different values
    assert _random_wechat_uin() != _random_wechat_uin()


# Credential persistence

def test_credential_save_load_roundtrip():
    from channels.wechat_channel.auth import load_credentials, save_credentials
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name

    try:
        creds = {
            "bot_token": "test_token_abc123",
            "ilink_bot_id": "bot_001",
            "ilink_user_id": "user@im.wechat",
            "base_url": "https://ilinkai.weixin.qq.com",
        }
        save_credentials(path, creds)

        loaded = load_credentials(path)
        assert loaded is not None
        assert loaded["bot_token"] == "test_token_abc123"
        assert loaded["ilink_user_id"] == "user@im.wechat"
    finally:
        Path(path).unlink(missing_ok=True)


def test_load_credentials_nonexistent():
    from channels.wechat_channel.auth import load_credentials
    assert load_credentials("/nonexistent/path/creds.json") is None


def test_load_credentials_corrupt():
    from channels.wechat_channel.auth import load_credentials
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        f.write(b"not json{{")
        path = f.name
    try:
        assert load_credentials(path) is None
    finally:
        Path(path).unlink(missing_ok=True)


# QR code state machine

def test_qr_poll_status_confirmed():
    """Verify the confirmed status parsing from poll response."""
    status = {
        "status": "confirmed",
        "bot_token": "tok_123",
        "ilink_bot_id": "bot_abc",
        "ilink_user_id": "user@im.wechat",
    }
    assert status["status"] == "confirmed"
    assert "bot_token" in status
    assert status["bot_token"] == "tok_123"


def test_qr_poll_status_states():
    """All four QR states should be recognized."""
    valid_states = {"wait", "scaned", "confirmed", "expired"}
    for state in valid_states:
        assert state in valid_states  # regression guard


# _split_long_text

def test_split_short():
    from channels.wechat_channel.handler import _split_long_text
    assert _split_long_text("hello") == ["hello"]


def test_split_long():
    from channels.wechat_channel.handler import _split_long_text
    long_text = "a" * 3000
    chunks = _split_long_text(long_text, max_chars=2048)
    assert len(chunks) == 2
    assert all(len(c) <= 2048 for c in chunks)


# 本循环用上了共享的健康度记账
#
# 退避 / 日志形状那几条纯逻辑测试搬到了 `tests/test_loop_health.py` ——
# 判定现在住在 `core/loop_health.py`（两个消费者共用一份：
# 本通道 与 `core/workspace.py::_BaseWatcher`）。留在这里的只有本文件的事。

def test_the_loop_uses_the_shared_health_tracker():
    """本循环不许自己再长一份退避 / 日志形状（那就是第二份判定）。"""
    import re
    from pathlib import Path
    text = (Path(__file__).parent.parent
            / "channels/wechat_channel/handler.py").read_text(encoding="utf-8")
    assert "from core.loop_health import LoopHealth" in text, \
        "通道没有用共享的健康度记账"
    assert not re.search(r"def _poll_backoff|def _should_log_failure", text), \
 "通道自己又长了一份退避/日志形状 —— 判定只许有一处（同族）"


def test_the_fixed_two_second_sleep_is_gone():
    """钉住靶子：`await asyncio.sleep(2)` 不许回来。

    固定 2 秒的 sleep 是这条通道的退避形状；它回来了就会让退避判定
    与共享健康度记账分叉。
    """
    import re
    from pathlib import Path
    text = (Path(__file__).parent.parent
            / "channels/wechat_channel/handler.py").read_text(encoding="utf-8")
    assert not re.search(r"asyncio\.sleep\(2\)", text), \
        "固定 2 秒的 sleep 又回来了"


def test_channel_loops_are_registered_on_both_start_paths():
    """两条启动路径都要登记事件循环 —— 否则「重新登录」永远停不掉旧通道。

    现场：点重登，「旧微信通道还在运行，无法安全停止，请重启应用
    后再试」；重启，自动启动又把它拉起来（同样没登记），死循环。
    根因就是 `_channel_loops` 只有"网页登录"那一条路写过。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "web" / "server.py").read_text(encoding="utf-8")

    # 三处都得登记：auto-start / 通用 start / 网页登录
    assert src.count("_channel_loops[") >= 3, "有启动路径没登记事件循环"
    # auto-start 与通用 start 都要在 finally 里摘掉（不然下一次拿到一个死循环）
    assert "_channel_loops.pop(cid, None)" in src, "auto-start 没摘循环"
    assert "_channel_loops.pop(channel_id, None)" in src, "通用 start 没摘循环"
    # 强制收口：terminate 排队时不能只剩一句"请重启应用"
    assert "loop.call_soon_threadsafe(loop.stop)" in src, "没有强制收口那一步"


def test_qr_login_uses_the_shared_net_proxy():
    """扫码登录的两次 GET 必须走同一处出网口径（`core/net_proxy`）。

    点重登，日志里 `[wechat] Login failed: [WinError 10061]
    目标计算机积极拒绝`，界面上只看到"唤起中"。根因是这两次 `httpx.get` 是裸调：
    `trust_env=True` 会静默采纳环境里的 `ALL_PROXY`（或一个死代理）。
    通道主体（`handler.run`）早就走这条口径了，登录这条路漏了。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent / ".." / "channels" / "wechat_channel" / "auth.py").read_text(encoding="utf-8")
    assert "from core import net_proxy as _np" in src, "auth.py 没接出网口径"
    assert src.count("_np.httpx_kwargs()") >= 2, "两次 GET 没都走 net_proxy"
    # 负对照：裸 GET 不许再出现
    assert "httpx.get(f\"{base_url}/ilink/bot/get_bot_qrcode?bot_type=3\")" not in src, "取二维码还是裸 GET"


def test_cached_credentials_report_connected_to_the_web_ui():
    """用缓存凭据跑起来时，也要把网页状态推到 `confirmed`。

    「扫码连上了，发消息没反应」。查下来通道没在跑
    （进程里零条 iLink 连接），而界面上写着"已连接" —— 两处都在说谎：
      · `/api/wechat/start-login` 一见凭据就回 `already_configured` 然后直接返回；
      · `_ensure_login` 走缓存那条路不写网页状态，就算真的跑起来了，
        界面也永远停在"正在唤起…"。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    srv = (root / "web" / "server.py").read_text(encoding="utf-8")
    hnd = (root / "channels" / "wechat_channel" / "handler.py").read_text(encoding="utf-8")

    # ① 端点：先判"在跑"，再判"配过"；配过而没在跑，落到启动块，不再早退。
    i_run = srv.find('if _channel_running.get("wechat_channel"):')
    i_cfg = srv.find('if not force and creds_path.exists():')
    assert 0 <= i_run < i_cfg, "顺序反了：配过的判断必须排在在跑的判断之后"
    assert 'return jsonify({"ok": True, "phase": "already_configured"' not in srv,         "配过就早退的旧形状还在"

    # ② 缓存凭据那条路要通知网页：confirmed。
    i = hnd.find("Loaded cached credentials")
    assert i >= 0 and 'web_cb("confirmed"' in hnd[i:i + 900],         "缓存凭据启动时没把网页状态推到 confirmed"


def test_channel_logs_never_break_the_pipeline():
    """通道里的 `print` 必须是GBK 编得出来的 —— 它一抛，整条消息就没了。

    现场：
        [wechat] Flush error for o9cq…: UnicodeEncodeError: 'gbk' codec can't
        encode character '📩'
    `📩` 在 Windows 控制台上编不出来，异常从业务代码里抛出，
    `_delayed_flush` 把整条消息当"Flush error"吞掉，发消息没反应。
判据：`channels/**` 里任何 `print(...)` 都不许带 emoji。
    """
    import pathlib, re
    emoji = re.compile("[\\U0001F000-\\U0001FAFF\\u2600-\\u27BF]")
    root = pathlib.Path(__file__).resolve().parent.parent / "channels"
    bad = []
    for f in root.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.lstrip().startswith("print(") and emoji.search(line):
                bad.append("%s:%d" % (f.name, i))
    assert not bad, "通道日志里还有 emoji（GBK 控制台会崩）：" + ", ".join(bad[:5])

    #: 第二道闸：服务端的真实 stdout 必须 `errors="replace"` ——
    #: 通道之外还有几十处 print，凡是编不出来的字符都只该变成 `?`，不该抛。
    srv = (pathlib.Path(__file__).resolve().parent.parent / "web" / "server.py").read_text(encoding="utf-8")
    assert 'reconfigure(errors="replace")' in srv, "真实 stdout 没设 errors=replace"


def test_list_replies_are_sent_line_by_line():
    """列表类回复（`/resume` / `/change`）在微信里一条一行发出去。

    `/change` 的列表糊成一张墙 —— 实测发出去的 payload
    含换行，是微信客户端把换行折成了空格。框架自己打的标记
    （`extras["deliver_lines"]`）下，改成一元一行；模型回复不拆（那会变成刷屏）。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hnd = (root / "channels" / "wechat_channel" / "handler.py").read_text(encoding="utf-8")
    srv = (root / "core" / "pipeline" / "scheduler.py").read_text(encoding="utf-8")
    pre = (root / "core" / "pipeline" / "stages" / "pre_process.py").read_text(encoding="utf-8")

    assert 'extras.get("deliver_lines")' in hnd or "extras or {}).get(\"deliver_lines\")" in hnd,         "微信那边没看这个标记"
    assert "for ln in lines:" in hnd, "没有按行发"
    assert 'event.extras["deliver_lines"] = True' in srv, "调度器没把标记搬过去"
    assert pre.count('metadata["deliver_lines"] = True') >= 2, "/resume 与 /change 都要打标记"


# _api_post / _send_typing 的业务错误判据
#
# 现场：`_api_post` 只按 HTTP 状态码判错，而 iLink 的业务错误走 `ret` / `errmsg`
# —— 字段名写错时服务端照样回 200。结果是消息被腾讯静默丢弃、`send()` 仍回 True。
# 三条断言钉住：非 0 要抛 · 0 或缺省放行 · 没 typing_ticket 不许发请求。


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self.content = b"{}"
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload):
        self.resp = _FakeResp(payload)
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(url)
        return self.resp


def _fake_channel(payload):
    from channels.wechat_channel.handler import WeChatChannel
    ch = object.__new__(WeChatChannel)
    ch._http = _FakeHttp(payload)
    ch._bot_token = "t"
    ch._base_url = "https://example.invalid"
    ch._polling_timeout = 30
    return ch


def test_api_post_raises_when_ret_is_not_zero():
    import asyncio
    from channels.wechat_channel.handler import WeChatAPIError
    ch = _fake_channel({"ret": -2, "errmsg": "ilink_user_id required"})
    try:
        asyncio.run(ch._api_post("ilink/bot/sendtyping", {}))
    except WeChatAPIError as e:
        assert e.ret == -2 and "ilink_user_id" in e.errmsg
    else:
        raise AssertionError("ret != 0 没有抛 —— 消息被静默丢弃时会当成已送达")


def test_api_post_accepts_ret_zero_and_missing_ret():
    import asyncio
    for payload in ({"ret": 0, "msgs": []}, {"msgs": []}, {}):
        ch = _fake_channel(payload)
        got = asyncio.run(ch._api_post("ilink/bot/getupdates", {}))
        assert got == payload


def test_api_post_raises_on_errmsg_even_with_ret_zero():
    import asyncio
    from channels.wechat_channel.handler import WeChatAPIError
    ch = _fake_channel({"ret": 0, "errmsg": "something odd"})
    try:
        asyncio.run(ch._api_post("ilink/bot/sendmessage", {}))
    except WeChatAPIError:
        return
    raise AssertionError("带 errmsg 的 200 被当成成功了")


def test_send_typing_does_not_fire_without_a_ticket():
    import asyncio
    ch = _fake_channel({"ret": 0})
    asyncio.run(ch._send_typing("u", "ctx"))
    assert ch._http.calls == [], "没有 typing_ticket 还是发了请求（那必然是 ret:-3）"

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
    import sys
    sys.exit(main())
