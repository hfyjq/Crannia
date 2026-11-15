"""
WeChat iLink MessageConverter — iLink JSON ↔ MessageChain.

iLink item types:
  - type 1: text    → Plain
  - type 2: image   → Image   (aeskey / media.aes_key)
  - type 3: voice   → Voice
  - type 4: file    → File    (media.full_url + media.aes_key)
  - type 5: video   → (unsupported)

Media is stored encrypted on CDN.  The `media` dict contains
`full_url` (download URL) and `aes_key` (AES-128-ECB key).
"""

from __future__ import annotations

from typing import Any

from channels.converter import MessageConverter
from channels.message import File, Image, MessageChain, Plain, Voice


class WeChatMessageConverter(MessageConverter):
    """Translate between iLink JSON messages and MessageChain."""

    # iLink item type constants
    ITEM_TEXT = 1
    ITEM_IMAGE = 2
    ITEM_VOICE = 3
    ITEM_FILE = 4
    ITEM_VIDEO = 5

    # -- platform → internal -----------------------------------------

    def to_internal(self, platform_message: dict[str, Any]) -> MessageChain:
        """Convert an iLink message dict into a MessageChain.

        *platform_message* is one element of `resp["msgs"]` from getUpdates.
        """
        # Raw message is a flat dict (not nested under "msg")
        msg = platform_message.get("msg", platform_message)
        item_list: list[dict] = msg.get("item_list", [])

        components: list = []
        for item in item_list:
            item_type = item.get("type", 0)

            if item_type == self.ITEM_TEXT:
                text = item.get("text_item", {}).get("text", "")
                if text:
                    components.append(Plain(text))

            elif item_type == self.ITEM_IMAGE:
                img = item.get("image_item", {})
                media = img.get("media", {})
                if isinstance(media, str):
                    import ast
                    try:
                        media = ast.literal_eval(media)
                    except (ValueError, SyntaxError):
                        media = {}
                aes_key = img.get("aeskey") or media.get("aes_key", "")
                _img_url = media.get("full_url", "")
                # 拿不到下载地址时不许静默。
                #
                # 否则模型会收到「微信这条通道的图片是加密的（要先用 aeskey 解一次），
                # 那一段还没做」这类误导性判断 —— 而那段代码是在的
                # （`handler.py::_download_files`）。真相是这一张没有下载地址，
                # 于是 `_download_files` 的 `if not url: continue` 一声不吭地跳过了。
                #「没出声」与「没发生」长得一模一样。
                #
                # 连 `image_item` / `media` 的键名一起打：这正是要查的那件事
                #   （是真的没有地址，还是字段叫别的名字 / `media` 没解析成 dict）。
                if not _img_url:
                    print("[wechat] 图片消息里**没有下载地址** —— 跳过了。"
                          " image_item=%s media=%s"
                          % (sorted(img.keys()),
                             sorted(media.keys()) if isinstance(media, dict)
                             else type(media).__name__))
                components.append(
                    Image(
                        url=_img_url,
                        mime_type="image/jpeg",
                    )
                )
                # Store aes_key on the component for download
                if aes_key:
                    components[-1]._aes_key = aes_key

            elif item_type == self.ITEM_FILE:
                f = item.get("file_item", {})
                media = f.get("media", {})
                if isinstance(media, str):
                    import ast
                    try:
                        media = ast.literal_eval(media)
                    except (ValueError, SyntaxError):
                        media = {}
                size_str = f.get("len", "0")
                try:
                    size_bytes = int(size_str)
                except (ValueError, TypeError):
                    size_bytes = 0
                comp = File(
                    name=f.get("file_name", "file"),
                    url=media.get("full_url", ""),
                    mime_type="application/octet-stream",
                    size_bytes=size_bytes,
                )
                aes_key = media.get("aes_key", "")
                if aes_key:
                    comp._aes_key = aes_key
                components.append(comp)

            elif item_type == self.ITEM_VOICE:
                v = item.get("voice_item", {})
                components.append(
                    Voice(url=v.get("cdn_url", ""),
                          duration_seconds=v.get("duration", 0))
                )

            # type 5 (video) — silently ignored for now

        return MessageChain(components) if components else MessageChain()

    # -- internal → platform -----------------------------------------

    def to_platform(self, message_chain: MessageChain, **kwargs: Any) -> dict[str, Any]:
        """Convert a MessageChain into an iLink sendMessage item_list.

        只做文字（含 `At`）。图片和文件不在这里造 ——
        它们的条目要先拿到一个上传后的引用（`encrypt_query_param`）和一把
        密钥，而上传是异步的、要用通道的令牌，那一段在
        `channels/wechat_channel/media.py` + `handler._send_media`。
        以前这里凭 `local_path` 拼过一个 `cdn_url` 字段，那个形状服务端不认
        （发出去对方什么都看不到，而且不报错）。
        """
        item_list: list[dict[str, Any]] = []

        for c in message_chain.components:
            from channels.message import At
            if isinstance(c, (Plain, At)):
                item_list.append({
                    "type": self.ITEM_TEXT,
                    "text_item": {"text": c.to_plain_text()},
                })

        return {"item_list": item_list}
