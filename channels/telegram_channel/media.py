"""Telegram 消息里有哪些媒体 —— 要下什么 · 落成什么组件 · 出站发哪一种。

本模块不 import `telegram`：夹具直接喂鸭子类型（`photo` / `voice` /
`document` / `sticker` …），未装 SDK 也能自测（与钉钉、飞书那两份同一条纪律）。

## 一条消息只有一种主媒体

Telegram 的一条消息带一个主媒体字段（相册是 N 条独立消息，各自带
`media_group_id`）。这里仍写成"列表"，是因为文档 + 说明文字天然共存，
而且将来要加"相册合并"时改的是这一处。

## 每种都有去处（不许静默丢）

| 字段 | 组件 | 说明 |
|---|---|---|
| `photo` | `Image` | 取最后一个尺寸（Telegram 给的是多档缩略图） |
| `sticker`（静态） | `Image` | webp |
| `sticker`（动图 / 视频） | `File` | `.tgs` / `.webm` —— 我们没法把它当图片读 |
| `voice` | `Voice` | `.oga` + 时长 |
| `audio` / `video` / `video_note` / `animation` / `document` | `File` | 原样给模型一条"有这个文件" |

判据：先判字段、再判组件。"它是不是图片"由平台字段说，
不由文件名猜 —— 猜错的方向是"把一段视频当图片喂给模型"。
"""

from __future__ import annotations

import re
from typing import Any

#: Bot API `getFile` 的硬上限（不是我们设的）：超过它连下载地址都拿不到。
#:提前判、说出来（"太大了没下"与"下失败了"是两件事）。
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

#: 出站只有两条路：图片消息 / 文件消息。
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

#: 字段 → 种类。顺序即优先级（一条消息只会有其中一个）。
_FIELDS: tuple[tuple[str, str], ...] = (
    ("photo", "photo"),
    ("voice", "voice"),
    ("video_note", "video_note"),
    ("animation", "animation"),
    ("sticker", "sticker"),
    ("audio", "audio"),
    ("video", "video"),
    ("document", "document"),
)

#: 没有名字时按种类给的兜底名（后缀要与真实字节对得上）。
_DEFAULT_NAME = {
    "photo": "photo.jpg",
    "voice": "voice.oga",
    "video_note": "video_note.mp4",
    "animation": "animation.mp4",
    "sticker": "sticker.webp",
    "audio": "audio.mp3",
    "video": "video.mp4",
    "document": "document",
}

#: 按种类给的兜底 mime（平台没给时才用）。
_DEFAULT_MIME = {
    "photo": "image/jpeg",
    "voice": "audio/ogg",
    "video_note": "video/mp4",
    "animation": "video/mp4",
    "sticker": "image/webp",
    "audio": "audio/mpeg",
    "video": "video/mp4",
    "document": "application/octet-stream",
}


def _suffix(name: str, fallback: str) -> str:
    """名字里的后缀（含点）；没有就给兜底。只认短的，不做通用猜测。"""
    m = re.search(r"(\.[0-9A-Za-z]{1,8})$", str(name or ""))
    return m.group(1).lower() if m else fallback


def items_of(tg_msg: Any) -> list[dict]:
    """一条 Telegram 消息 → 待下载媒体（没有 → `[]`）。

    返回的每一项：`kind` / `file_id` / `name` / `mime` / `size` /
    `duration` / `animated`（只有贴纸有）。
    """
    if tg_msg is None:
        return []
    for field, kind in _FIELDS:
        obj = getattr(tg_msg, field, None)
        if not obj:
            continue

        if kind == "photo":
            #: Telegram 给的是多档缩略图，最后一档最大。
            biggest = obj[-1] if isinstance(obj, (list, tuple)) and obj else obj
            return [{
                "kind": "photo",
                "file_id": str(getattr(biggest, "file_id", "") or ""),
                "name": _DEFAULT_NAME["photo"],
                "mime": "image/jpeg",
                "size": int(getattr(biggest, "file_size", 0) or 0),
                "duration": 0,
                "animated": False,
            }]

        animated = bool(getattr(obj, "is_animated", False))
        is_video = bool(getattr(obj, "is_video", False))
        name = str(getattr(obj, "file_name", "") or "")
        if kind == "sticker":
            name = name or ("sticker.tgs" if animated
                            else "sticker.webm" if is_video else "sticker.webp")
        mime = str(getattr(obj, "mime_type", "") or "")
        if kind == "sticker" and not mime:
            mime = ("application/x-tgsticker" if animated
                    else "video/webm" if is_video else "image/webp")
        if not name:
            name = _DEFAULT_NAME.get(kind, "file")
        return [{
            "kind": kind,
            "file_id": str(getattr(obj, "file_id", "") or ""),
            "name": name,
            "mime": mime or _DEFAULT_MIME.get(kind, "application/octet-stream"),
            "size": int(getattr(obj, "file_size", 0) or 0),
            "duration": int(getattr(obj, "duration", 0) or 0),
            "animated": animated or is_video,
        }]
    return []


def ext_of(item: dict) -> str:
    """落盘后缀：用真名字的，名字没有就按种类给。"""
    kind = str((item or {}).get("kind") or "document")
    name = str((item or {}).get("name") or "")
    fallback = _suffix(_DEFAULT_NAME.get(kind, "file"), ".bin")
    return _suffix(name, fallback)


def component_of(item: dict, local_path: str):
    """一条已下载的媒体 → `MessageChain` 组件（`None` = 没有可用字节）。"""
    from channels.message import File as MsgFile, Image, Voice

    if not local_path:
        return None
    kind = str((item or {}).get("kind") or "document")
    mime = str((item or {}).get("mime") or "")
    if kind == "photo" or (kind == "sticker" and not item.get("animated")):
        return Image(local_path=local_path, mime_type=mime or "image/jpeg")
    if kind == "voice":
        comp = Voice(url=None, duration_seconds=int(item.get("duration") or 0))
        #: `Voice` 没有 `local_path` 字段（`channels/message.py`）——
        #: 与钉钉那条同一个挂法：先建对象，再补动态属性。读者（媒体路由）
        #: 读的就是 `local_path`。
        comp.local_path = local_path
        return comp
    return MsgFile(
        name=str(item.get("name") or "file"),
        local_path=local_path,
        mime_type=mime or "application/octet-stream",
        size_bytes=int(item.get("size") or 0),
    )


def outbound_kind(name: str) -> str:
    """出站：按后缀判 `"photo"` / `"document"`（Telegram 只有这两条路）。"""
    s = str(name or "")
    dot = s.rfind(".")
    ext = s[dot:].lower() if dot >= 0 else ""
    return "photo" if ext in _IMAGE_EXT else "document"


def addressed_to_bot(tg_msg: Any, bot_username: str = "",
                      bot_id: Any = None) -> bool:
    """群聊里这条是不是在跟机器人说话。私聊不用问这个（调用方自己判）。

    三种信号，任一命中即可：

    | 信号 | 怎么判 | 为什么可靠 |
    |---|---|---|
    | `text_mention` 实体 | `entity.user.id == bot.id` | 权威：这是"点了人"的结构化结果，不是文本 |
    | 回复机器人 | `reply_to_message.from_user.id == bot.id` | 权威：结构化的 |
    | 文本里 `@username` | 大小写不敏感的子串 | 兜底：有些客户端不给实体；也可能误判（别人手打了这个名字） |

    为什么不用 `entity.type == "mention"` 去切文本：Telegram 的
      `offset` / `length` 是 UTF-16 码元，Python 的字符串下标是码点
前面有 emoji 时切出来的是错的位置。文本兜底走"整串里找"就不会错。
    `bot_username` 与 `bot_id` 都不知道时返回 `False` —— 调用方据此
      放行（判不出来就别拦：漏回一条比误吞一条更糟）。
    """
    if tg_msg is None:
        return False

    uid = None
    try:
        uid = int(bot_id) if bot_id is not None else None
    except (TypeError, ValueError):
        uid = None

    for ent in getattr(tg_msg, "entities", None) or []:
        if str(getattr(ent, "type", "") or "") != "text_mention":
            continue
        user = getattr(ent, "user", None)
        if uid is not None and user is not None:
            try:
                if int(getattr(user, "id", 0) or 0) == uid:
                    return True
            except (TypeError, ValueError):
                continue

    name = str(bot_username or "").lstrip("@")
    if name:
        text = str(getattr(tg_msg, "text", None)
                   or getattr(tg_msg, "caption", None) or "")
        if ("@" + name).lower() in text.lower():
            return True

    reply = getattr(tg_msg, "reply_to_message", None)
    from_user = getattr(reply, "from_user", None) if reply is not None else None
    if uid is not None and from_user is not None:
        try:
            if int(getattr(from_user, "id", 0) or 0) == uid:
                return True
        except (TypeError, ValueError):
            pass
    return False


__all__ = [
    "MAX_DOWNLOAD_BYTES", "items_of", "ext_of", "component_of", "outbound_kind",
    "addressed_to_bot",
]
