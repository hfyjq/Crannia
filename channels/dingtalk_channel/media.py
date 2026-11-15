"""钉钉消息的纯形状 —— 提取 · 下载句柄 · 回复载荷 · 主动推送载荷。

为什么单独一个文件：钉钉那条路以前是"从裸 dict 里抠 `text.content`"，
于是图片 / 文件 / 语音 / 视频 / 富文本全被丢掉（用户看到的是"机器人不理我"），
而群聊/私聊的判据也写错了（`conversationId` 非空就算群，而单聊也带它）。
这些都能在不连网的情况下钉住 —— 所以放这里。

本模块不 import `dingtalk_stream`：夹具直接喂生产形状的 dict
（`msgtype` / `text` / `content` / `conversationType` …），未装 SDK 也能自测。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 资源句柄（通道侧取字节时才换成真路径）。
SCHEME = "dingtalk://"

#: 单条资源最多下多少（与飞书那条同一口径）。
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

#: 一次出站最多传多少 —— 这是我们的理智上限，不是平台的上限：
#: 钉钉按种类各有各的额度（图片与文件不同），超了它本来也会拒；
#: 这里只防"把一整块盘读进内存"，所以取与下载同一个数。
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

#: 后缀（下载下来的字节没有名字，按种类给一个）。
_EXT = {"image": ".jpg", "file": ".bin", "audio": ".amr", "video": ".mp4"}

#: 出站时按后缀判"发图片还是发文件"（只有图片有专门的消息类型）。
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

#: 钉钉开放平台入口。
API_BASE = "https://api.dingtalk.com"

#: 旧版 oapi 的域 —— `media/upload` 只在这个域上（v1.0 没有对应端点）。
OAPI_BASE = "https://oapi.dingtalk.com"

#: `conversationType` 的取值（1 = 单聊，2 = 群聊）。这才是群/私聊判据。
CONV_SINGLE = "1"
CONV_GROUP = "2"


def content_of(data: dict) -> dict:
    """`data["content"]` → dict。

    钉钉的 `content` 是 JSON 字符串（图片/文件那条），
    当 dict 用会抛 `'str' object has no attribute 'get'` —— 同一颗雷飞书那边踩过。
    """
    raw = (data or {}).get("content", "")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        got = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def _ms_to_s(value: Any) -> int:
    try:
        return max(0, int(value or 0) // 1000)
    except (TypeError, ValueError):
        return 0


def extract(data: dict) -> dict:
    """一条钉钉回调的 `data` → 这一条消息的全部可读信息。

    返回的键（缺就是空）：
      `msgtype` · `text` · `images`（downloadCode 列表）·
      `files`（`{code,name,kind}` 列表）· `conversation_id` · `conversation_type` ·
      `is_group` · `sender_id` · `sender_staff_id` · `sender_nick` · `msg_id` ·
      `session_webhook` · `robot_code` · `is_in_at` · `at_staff_ids`
    """
    d = data if isinstance(data, dict) else {}
    msgtype = str(d.get("msgtype") or "")
    content = content_of(d)

    text = ""
    images: list[str] = []
    files: list[dict] = []

    if msgtype == "text":
        txt = d.get("text")
        if isinstance(txt, dict):
            text = str(txt.get("content") or "")
    elif msgtype == "richText":
        parts: list[str] = []
        for item in content.get("richText") or []:
            if not isinstance(item, dict):
                continue
            if item.get("text"):
                parts.append(str(item["text"]))
            if item.get("downloadCode"):
                images.append(str(item["downloadCode"]))
        text = "".join(parts)
    elif msgtype == "picture":
        code = content.get("downloadCode") or content.get("pictureDownloadCode")
        if code:
            images.append(str(code))
    elif msgtype in ("file", "audio", "video"):
        code = str(content.get("downloadCode") or "")
        if code:
            files.append({
                "code": code,
                "name": str(content.get("fileName") or ("%s.dat" % msgtype)),
                "kind": msgtype,
                "duration": _ms_to_s(content.get("duration")),
            })
    else:
        # `markdown` / `actionCard` / 其它：正文在 `text.content` 或 `content.text`
        txt = d.get("text")
        if isinstance(txt, dict):
            text = str(txt.get("content") or "")
        if not text:
            text = str(content.get("text") or "")

    conv_type = str(d.get("conversationType") or "")
    conv_id = str(d.get("conversationId") or "")
    # 判据顺序：先看 `conversationType`（权威）；读不到才退回旧判
    #   （`conversationId` 非空 = 群）。旧载荷没有 conversationType 时维持原行为，
    #   猜成私聊会把一群人的对话并进私聊那条。
    if conv_type == CONV_GROUP:
        is_group = True
    elif conv_type == CONV_SINGLE:
        is_group = False
    else:
        is_group = bool(conv_id)

    ats = d.get("atUsers") or []
    at_staff = []
    for a in ats:
        if isinstance(a, dict) and a.get("staffId"):
            at_staff.append(str(a["staffId"]))

    return {
        "msgtype": msgtype,
        "text": text,
        "images": images,
        "files": files,
        "conversation_id": conv_id,
        "conversation_type": conv_type,
        "is_group": is_group,
        "sender_id": str(d.get("senderId") or ""),
        "sender_staff_id": str(d.get("senderStaffId") or ""),
        "sender_nick": str(d.get("senderNick") or ""),
        "msg_id": str(d.get("msgId") or ""),
        "session_webhook": str(d.get("sessionWebhook") or ""),
        "robot_code": str(d.get("robotCode") or ""),
        "is_in_at": bool(d.get("isInAtList")),
        "at_staff_ids": at_staff,
    }


def components_of(info: dict) -> list:
    """`extract` 的结果 → `MessageChain` 的组件列表（媒体先挂句柄）。"""
    from channels.message import File as MsgFile, Image, Plain, Voice

    comps: list = []
    text = str((info or {}).get("text") or "").strip()
    if text:
        comps.append(Plain(text=text))
    for code in (info or {}).get("images") or []:
        comps.append(Image(url=SCHEME + "image/" + code, mime_type="image/jpeg"))
    for f in (info or {}).get("files") or []:
        kind = f.get("kind") or "file"
        if kind == "audio":
            comps.append(Voice(url=SCHEME + "audio/" + str(f["code"]),
                               duration_seconds=int(f.get("duration") or 0)))
        elif kind == "video":
            comps.append(MsgFile(name=str(f.get("name") or "video.mp4"),
                                 url=SCHEME + "video/" + str(f["code"]),
                                 mime_type="video/mp4"))
        else:
            comps.append(MsgFile(name=str(f.get("name") or "file"),
                                 url=SCHEME + "file/" + str(f["code"])))
    return comps


def parse_handle(url: str) -> tuple[str, str]:
    """`dingtalk://image/<code>` → `("image", "<code>")`；不像句柄给 `("", "")`。"""
    s = str(url or "")
    if not s.startswith(SCHEME):
        return "", ""
    kind, _, code = s[len(SCHEME):].partition("/")
    kind = kind.strip().lower()
    if kind not in _EXT or not code:
        return "", ""
    return kind, code


def ext_for(kind: str, name: str = "") -> str:
    """落盘后缀：文件用真名字的，其余按种类给。"""
    if str(kind).lower() == "file" and name:
        m = re.search(r"(\.[0-9A-Za-z]{1,8})$", str(name))
        if m:
            return m.group(1).lower()
    return _EXT.get(str(kind).lower(), ".bin")


#: `[字](url)` —— 标题里只留字（标题栏里挂一条裸 URL 很丑）。
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _title_of(text: str) -> str:
    """markdown 消息的标题：第一行去掉记号，空的话给一个兜底。"""
    for line in str(text or "").splitlines():
        t = _MD_LINK.sub(lambda m: m.group(1), line)      # [字](url) -> 字
        t = t.strip().lstrip("#").replace("**", "").replace("`", "").strip()
        if t:
            return t[:20]
    return "回复"


def reply_payload(text: str, at_ids: list | None = None) -> dict:
    """一条回复的 webhook 载荷：像 markdown 就发 markdown，否则发 text。"""
    from channels.textfmt import looks_like_markdown

    at = {"atUserIds": [str(x) for x in (at_ids or []) if str(x)]}
    body = str(text or "")
    if looks_like_markdown(body):
        return {
            "msgtype": "markdown",
            "markdown": {"title": _title_of(body), "text": body},
            "at": at,
        }
    return {"msgtype": "text", "text": {"content": body}, "at": at}


def _robot_request(session_id: str, is_group: bool,
                   msg_key: str, msg_param: str) -> tuple[str, dict]:
    """机器人发消息的 `(路径, 请求体)` —— 群聊 / 单聊是两个不同的端点。

    文字与图片/文件共用这一对端点，差别只在 `msgKey` / `msgParam`；
      端点写两处就会一边改一边忘。
    """
    if is_group:
        return "/v1.0/robot/groupMessages/send", {
            "openConversationId": str(session_id),
            "msgKey": msg_key,
            "msgParam": msg_param,
        }
    return "/v1.0/robot/oToMessages/batchSend", {
        "userIds": [str(session_id)],
        "msgKey": msg_key,
        "msgParam": msg_param,
    }


def proactive_request(session_id: str, is_group: bool, text: str) -> tuple[str, dict]:
    """主动推送的 `(路径, 请求体)` —— 群聊 / 单聊是两个不同的端点。

    回复走的是 `sessionWebhook`（约 1.5 小时有效、一条一用完），
    主动推送没有那种东西，必须调 OpenAPI（还要先拿 access token）。
    """
    return _robot_request(
        session_id, is_group, "sampleText",
        json.dumps({"content": str(text or "")}, ensure_ascii=False))


def kind_for(name: str) -> str:
    """出站：按后缀判 `"image"` / `"file"`（只有图片有专门的消息类型）。"""
    s = str(name or "")
    dot = s.rfind(".")
    ext = s[dot:].lower() if dot >= 0 else ""
    return "image" if ext in _IMAGE_EXT else "file"


def _file_type(name: str) -> str:
    """`sampleFile.fileType` —— 后缀不带点；没有后缀就给 `file`。"""
    s = str(name or "")
    dot = s.rfind(".")
    ext = s[dot + 1:].lower() if dot >= 0 else ""
    return re.sub(r"[^0-9a-z]", "", ext) or "file"


def media_request(session_id: str, is_group: bool, kind: str,
                  media_id: str, file_name: str = "") -> tuple[str, dict]:
    """出站媒体（图片 / 文件）的 `(路径, 请求体)`。

    `sampleImageMsg` 的 `photoURL` 收的是上传回来的 media_id，不是公网 URL
      —— 钉钉文档没写这一格；按这个填，图片到得了（收到的是那张图，不是灰框）。
    `sampleFile` 要三格：`mediaId` / `fileName` / `fileType`。
    """
    if str(kind) == "image":
        return _robot_request(
            session_id, is_group, "sampleImageMsg",
            json.dumps({"photoURL": str(media_id)}))
    return _robot_request(
        session_id, is_group, "sampleFile",
        json.dumps({"mediaId": str(media_id),
                    "fileName": str(file_name or "file"),
                    "fileType": _file_type(file_name)},
                   ensure_ascii=False))
