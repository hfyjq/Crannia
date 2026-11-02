"""iLink 出站媒体 —— 加密、上传地址、消息条目形状（纯函数，不碰网络）。

一条媒体要三步才发得出去：

  1. `ilink/bot/getuploadurl` 换一个 CDN 上传地址（要明文大小、明文 md5、密文大小）；
  2. 把 AES-128-ECB + PKCS#7 加密后的字节 POST 到那个地址，
     响应头 `x-encrypted-param` 就是下一步要用的引用；
  3. `ilink/bot/sendmessage` 的条目里放 `media`（引用 + 密钥 + `encrypt_type: 1`）。

四处容易写错，而且错了不会报错、只会"对方看不见"：

  * `filesize` 是密文大小（填充后），`rawsize` 才是明文；
  * 发出去的 `aes_key` 是 base64(32 位十六进制字符串)，不是 base64(原始 16 字节)
    —— 反过来会让接收端解不开，图片显示成灰框；
  * `file_item.len` 是明文字节数的字符串；
  * 图片走 `image_item`（`mid_size` = 密文大小），其余一律走 `file_item`。

为什么单独一个模块：这三步里只有第 1/3 步是形状，可以不起网络就钉住；
把形状和网络混在 handler 里，测试就只能测到"调了没调"。
"""

from __future__ import annotations

import mimetypes
from typing import Any

#: 消息条目类型（`item_list[].type`）。
ITEM_IMAGE = 2
ITEM_FILE = 4

#: 上传时声明的媒体类型（`getuploadurl.media_type`）。
MEDIA_IMAGE = 1
MEDIA_FILE = 3

#: CDN 根。上传与下载同源；登录返回的 `base_url` 是 API 那一个，不是这个。
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"

#: 加密块大小（AES）。
BLOCK = 16


class MediaError(RuntimeError):
    """出站媒体失败。`str(exc)` 会原样进日志/回给用户，所以要说人话。"""


def aes_padded_size(size: int) -> int:
    """密文大小（PKCS#7：整块时不省，照样补一整块）。"""
    n = int(size)
    if n < 0:
        raise MediaError("文件大小算成负数了：%d" % n)
    return n + (BLOCK - (n % BLOCK))


def pkcs7_pad(data: bytes, block: int = BLOCK) -> bytes:
    pad = block - (len(data) % block)
    if pad == 0:
        pad = block
    return data + bytes([pad]) * pad


def pkcs7_unpad(data: bytes, block: int = BLOCK) -> bytes:
    """去掉填充。只给测试与排查对照用（出站方向不调它）。"""
    if not data or len(data) % block:
        raise MediaError("密文长度不是块大小的整数倍")
    pad = data[-1]
    if not (1 <= pad <= block) or data[-pad:] != bytes([pad]) * pad:
        raise MediaError("填充不合法")
    return data[:-pad]


def encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS#7。

    ECB 在这里是协议规定，不是我们选的（同一条链的下载方向也是它）。
    """
    if len(key) != 16:
        raise MediaError("AES 密钥必须是 16 字节（拿到 %d）" % len(key))
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # noqa: BLE001
        raise MediaError(
            "这台机器没装 `cryptography`，发不了图片和文件。"
            "装上再试：pip install cryptography") from exc
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(pkcs7_pad(data)) + enc.finalize()


def decrypt(data: bytes, key: bytes) -> bytes:
    """`encrypt` 的逆。给测试用的配对实现，生产路径不调它。"""
    if len(key) != 16:
        raise MediaError("AES 密钥必须是 16 字节（拿到 %d）" % len(key))
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # noqa: BLE001
        raise MediaError("这台机器没装 `cryptography`") from exc
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return pkcs7_unpad(dec.update(data) + dec.finalize())


def kind_consts(kind: str) -> tuple[int, int]:
    """`"image"` / `"file"` → `(media_type, item_type)`。"""
    if kind == "image":
        return MEDIA_IMAGE, ITEM_IMAGE
    return MEDIA_FILE, ITEM_FILE


def kind_for(name_or_path: str) -> tuple[str, int, int]:
    """文件名 → `(kind, media_type, item_type)`。

    `kind` 只有 `"image"` / `"file"` 两种：没识别成图片的一律当文件发 ——
    宁可对方收到一个能下载的附件，也不要发一个打不开的"图片"。

    只看扩展名：调用方知道组件类型时必须自己给（见
    `handler._send_media(kind_hint=)`）—— 微信下载回来的图片可能没有扩展名。
    """
    mime, _ = mimetypes.guess_type(str(name_or_path))
    kind = "image" if (mime or "").lower().startswith("image/") else "file"
    media_type, item_type = kind_consts(kind)
    return kind, media_type, item_type


def cdn_upload_url(cdn_base: str, upload_param: str, filekey: str,
                   quote_fn=None) -> str:
    """由 `upload_param` 拼上传地址（服务端也可能直接给 `upload_full_url`）。"""
    from urllib.parse import quote as _quote
    q = quote_fn or (lambda s: _quote(s, safe=""))
    base = (cdn_base or DEFAULT_CDN_BASE).rstrip("/")
    return "%s/upload?encrypted_query_param=%s&filekey=%s" % (
        base, q(str(upload_param)), q(str(filekey)))


def media_ref(encrypt_query_param: str, aes_key_hex: str,
              encrypt_type: int = 1) -> dict:
    """`media` 那一格。`aes_key` 的形状见模块 docstring 第二条。"""
    import base64
    return {
        "encrypt_query_param": str(encrypt_query_param),
        "aes_key": base64.b64encode(str(aes_key_hex).encode("ascii")).decode("ascii"),
        "encrypt_type": int(encrypt_type),
    }


def build_item(kind: str, media: dict, *, cipher_size: int, plain_size: int,
               file_name: str = "") -> dict:
    """一个消息条目（放进 `sendmessage` 的 `item_list`）。"""
    if kind == "image":
        return {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": dict(media),
                "mid_size": int(cipher_size),
            },
        }
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": dict(media),
            "file_name": str(file_name or "file"),
            "len": str(int(plain_size)),
        },
    }


def upload_payload(filekey: str, media_type: int, to_user: str, raw_size: int,
                   raw_md5: str, aes_key_hex: str,
                   channel_version: str = "1.0.3") -> dict[str, Any]:
    """`getuploadurl` 的请求体。两个大小别弄反（见模块 docstring）。"""
    return {
        "filekey": str(filekey),
        "media_type": int(media_type),
        "to_user_id": str(to_user),
        "rawsize": int(raw_size),
        "rawfilemd5": str(raw_md5),
        "filesize": aes_padded_size(raw_size),
        "no_need_thumb": True,
        "aeskey": str(aes_key_hex),
        "base_info": {"channel_version": channel_version},
    }
