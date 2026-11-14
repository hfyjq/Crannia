"""
Unit tests for FeishuChannel + FeishuMessageConverter.

Validates:
  1. FeishuChannel is a Channel subclass
  2. ChannelMetadata is correct
  3. test_connection() returns proper status
  4. FeishuMessageConverter: text / image / file / empty / unknown
  5. MessageSession key generation across platform types
  6. to_platform() round-trip consistency

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_feishu_channel.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_text_msg_data(text: str = "hello") -> dict:
    """Build a mock Feishu text message payload."""
    import json
    return {
        "message_type": "text",
        "content": json.dumps({"text": text}),
        "message_id": "om_abc123",
        "chat_id": "oc_chat456",
    }


def _make_image_msg_data(image_key: str = "img_abc123") -> dict:
    return {
        "message_type": "image",
        "content": {"image_key": image_key},
        "message_id": "om_img001",
        "chat_id": "oc_chat456",
    }


def _make_file_msg_data(file_key: str = "file_abc", file_name: str = "report.pdf") -> dict:
    return {
        "message_type": "file",
        "content": {"file_key": file_key, "file_name": file_name},
        "message_id": "om_file001",
        "chat_id": "oc_chat456",
    }


# ------------------------------------------------------------------
# 1. Channel class tests
# ------------------------------------------------------------------

class TestFeishuChannelMeta:
    """Metadata and construction — no network needed."""

    def test_channel_is_subclass(self):
        from channels.base import Channel
        from channels.feishu_channel.handler import FeishuChannel
        assert issubclass(FeishuChannel, Channel)

    def test_meta_returns_correct_metadata(self):
        from channels.feishu_channel.handler import FeishuChannel
        ch = FeishuChannel(config={})
        meta = ch.meta()
        assert meta.name == "feishu_channel"
        assert meta.platform_type == "feishu"
        assert meta.support_multimodal is True
        # 这一格改成 False —— 飞书那条路一次发一条完整消息，
        #   以前写 True 是空头承诺（`support_streaming` 全仓零读者）。
        assert meta.support_streaming is False

    def test_constructor_stores_config_and_deps(self):
        from channels.feishu_channel.handler import FeishuChannel
        config = {"app_id": "cli_abc", "app_secret": "secret123"}
        deps = {"scheduler": "mock_scheduler", "session_pool": "mock_pool"}
        ch = FeishuChannel(config=config, **deps)
        assert ch.config == config
        assert ch._scheduler == "mock_scheduler"
        assert ch._session_pool == "mock_pool"

    def test_test_connection_no_config(self):
        """Without app_id, test_connection reports not-ok."""
        from channels.feishu_channel.handler import FeishuChannel
        from core import channel_configs as _cc
        import asyncio
        #: 真机配过飞书之后账本会交出那一条（它排在入参之前）—— 见
        #:   `TelegramChannel` 那三条同族的测试。这里要的是"没有账本"那一支。
        orig = _cc.credentials_for
        _cc.credentials_for = lambda name: {}
        try:
            ch = FeishuChannel(config={})
            result = asyncio.run(ch.test_connection())
        finally:
            _cc.credentials_for = orig
        assert result["ok"] is False
        assert "not configured" in result["message"]

    def test_test_connection_sdk_missing(self):
        """If lark-oapi is importable, returns ok; else not-ok.

        Uses the nested config layout matching config.yaml:
        `channel.feishu.app_id`.
        """
        from channels.feishu_channel.handler import FeishuChannel
        import asyncio
        ch = FeishuChannel(config={"channel": {"feishu": {"app_id": "cli_test"}}})
        result = asyncio.run(ch.test_connection())
        # Either ok (SDK installed) or not-ok (SDK missing) — both valid
        assert "ok" in result
        assert "message" in result
        if result["ok"]:
            assert "SDK available" in result["message"]
        else:
            assert "not installed" in result["message"]


# ------------------------------------------------------------------
# 2. MessageConverter tests
# ------------------------------------------------------------------

class TestFeishuMessageConverter:
    """FeishuMessageConverter: platform ↔ internal MessageChain."""

    def test_to_internal_text(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_text_msg_data("你好世界"))
        assert not chain.is_empty
        assert "你好世界" in chain.content

    def test_to_internal_text_empty(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_text_msg_data(""))
        # Empty text → empty chain
        assert chain.is_empty

    def test_to_internal_image(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        from channels.message import ComponentType
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_image_msg_data("img_key_001"))
        assert not chain.is_empty
        assert chain.components[0].type == ComponentType.IMAGE
        assert "img_key_001" in chain.components[0].url

    def test_to_internal_image_no_key(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_image_msg_data(""))
        # Empty image_key → no component added
        assert chain.is_empty

    def test_to_internal_file(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        from channels.message import ComponentType
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_file_msg_data("f_key", "slides.pptx"))
        assert not chain.is_empty
        comp = chain.components[0]
        assert comp.type == ComponentType.FILE
        assert comp.name == "slides.pptx"
        assert "f_key" in comp.url

    def test_to_internal_file_no_key(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal(_make_file_msg_data("", "empty.pdf"))
        assert chain.is_empty

    def test_to_internal_unknown_type(self):
        """Unknown message_type → empty chain (graceful degrade)."""
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal({"message_type": "sticker", "content": {}})
        assert chain.is_empty

    def test_to_platform_roundtrip(self):
        """to_platform returns dict with text matching original chain content."""
        from channels.feishu_channel.handler import FeishuMessageConverter
        from channels.message import MessageChain, Plain
        converter = FeishuMessageConverter()
        chain = MessageChain([Plain("roundtrip test")])
        payload = converter.to_platform(chain)
        assert payload["text"] == "roundtrip test"

    def test_to_platform_empty_chain(self):
        from channels.feishu_channel.handler import FeishuMessageConverter
        from channels.message import MessageChain
        converter = FeishuMessageConverter()
        payload = converter.to_platform(MessageChain())
        assert payload["text"] == ""


# ------------------------------------------------------------------
# 3. MessageSession key tests
# ------------------------------------------------------------------

class TestFeishuSessionKeys:
    """Session key uniqueness across platform/message_type."""

    def test_private_session_key(self):
        from channels.base import MessageSession, MessageType
        session = MessageSession(
            platform="feishu",
            message_type=MessageType.PRIVATE,
            session_id="ou_user123",
        )
        key = f"{session.platform}:{session.message_type.value}:{session.session_id}"
        assert key == "feishu:private:ou_user123"

    def test_group_session_key(self):
        from channels.base import MessageSession, MessageType
        session = MessageSession(
            platform="feishu",
            message_type=MessageType.GROUP,
            session_id="oc_chat456",
        )
        key = f"{session.platform}:{session.message_type.value}:{session.session_id}"
        assert key == "feishu:group:oc_chat456"

    def test_different_platforms_different_keys(self):
        from channels.base import MessageSession, MessageType
        feishu = MessageSession("feishu", MessageType.PRIVATE, "123")
        wechat = MessageSession("wechat", MessageType.PRIVATE, "123")
        f_key = f"{feishu.platform}:{feishu.message_type.value}:{feishu.session_id}"
        w_key = f"{wechat.platform}:{wechat.message_type.value}:{wechat.session_id}"
        assert f_key != w_key


# ------------------------------------------------------------------
# 4. Edge cases
# ------------------------------------------------------------------

class TestFeishuEdgeCases:
    """Boundary and edge-case behaviour."""

    def test_malformed_text_content(self):
        """JSON-decode failure in text content → no crash, empty chain."""
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal({
            "message_type": "text",
            "content": "not-json!!!",
        })
        # Graceful degrade: malformed JSON → empty chain, no crash
        assert chain.is_empty

    def test_none_content(self):
        """msg_data with content=None → empty chain, no crash."""
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal({
            "message_type": "text",
            "content": None,
        })
        assert chain.is_empty

    def test_int_content(self):
        """msg_data with content=int → empty chain, no crash."""
        from channels.feishu_channel.handler import FeishuMessageConverter
        converter = FeishuMessageConverter()
        chain = converter.to_internal({
            "message_type": "text",
            "content": 12345,
        })
        assert chain.is_empty


# 运行器（本仓库约定：每个测试文件自带 `__main__`）
#
# 本仓库没有 pytest，这里自己把 `Test*` 类的 `test_*` 方法收进来跑。
# 没有入口的文件在全量跑里表现为「无汇总」而不是「红」—— 那是最难发现的一种，
# 所以新增测试类时必须同时确认它们被 `_collect()` 收得到。

def _collect():
    out = []
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            out.append((name, obj))
        elif name.startswith("Test") and isinstance(obj, type):
            try:
                inst = obj()
            except Exception:  # noqa: BLE001 —— 构造不了就整体跳过
                continue
            for mn in sorted(dir(inst)):
                if mn.startswith("test_"):
                    out.append((name + "." + mn, getattr(inst, mn)))
    return out


def main():
    tests = _collect()
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
