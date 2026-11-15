"""飞书消息的纯形状 —— content 解析 · 提及替换 · 资源句柄 · 切段 · 卡片。

为什么单独一个文件：这些是不碰网络就能断言的部分，而实际踩过的坑
恰好全在这一层 —— `content` 是 JSON 字符串（当成 dict 用就抛
`'str' object has no attribute 'get'`）、语音/视频/表情没有分支（落到
空链子，一条消息都不回）、富文本把所有语言版本拼在一起（同一条消息说两遍）。
把它们钉在纯函数上，「发了没反应」就不会再来第二次。

这里不发任何请求、不 import SDK —— 未装 `lark-oapi` 时也能导入与自测。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 资源句柄的前缀（转换器造、通道取字节）。
SCHEME = "feishu://"

#: 句柄里的资源种类 -> 下载时的 `type`（飞书只认 `image` / `file` 两种）。
_DOWNLOAD_TYPE = {
    "image": "image",
    "file": "file",
    "audio": "file",
    "video": "file",
    "sticker": "file",
}

#: 后缀（下载下来的字节没有名字，按种类给一个）。
_EXT = {
    "image": ".jpg",
    "file": ".bin",
    "audio": ".opus",
    "video": ".mp4",
    "sticker": ".gif",
}

#: 单条消息最多下多少（飞书侧也有上限；超了如实说，不静默截断）。
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

#: 切段 / markdown 判断与钉钉共用一处（`channels/textfmt.py`）。
from channels.textfmt import (  # noqa: E402,F401
    TEXT_CHUNK_CHARS, looks_like_markdown, split_text,
)


def content_of(msg: dict) -> dict:
    """`message.content` → dict。

    飞书的 content 永远是 JSON 字符串（官方口径：消息内容，json 字符串）。
    这一格就是那个坑的唯一出口：谁要读 content，都从这里过。
    支持三种输入：字符串（解析）/ 已是 dict（测试或新版）/ 其它（给空 dict）。
    """
    raw = (msg or {}).get("content", "")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        got = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def parse_handle(url: str) -> tuple[str, str]:
    """`feishu://image/<key>` → `("image", "<key>")`；不像句柄就给 `("", "")`。"""
    s = str(url or "")
    if not s.startswith(SCHEME):
        return "", ""
    rest = s[len(SCHEME):]
    kind, _, key = rest.partition("/")
    kind = kind.strip().lower()
    if kind not in _DOWNLOAD_TYPE or not key:
        return "", ""
    return kind, key


def download_type(kind: str) -> str:
    """资源种类 → 下载 API 的 `type`（只认 `image` / `file`）。"""
    return _DOWNLOAD_TYPE.get(str(kind).lower(), "file")


def ext_for(kind: str, name: str = "") -> str:
    """落到本地时用什么后缀。有真名字就用真名字的（文件那条）。"""
    if str(kind).lower() == "file" and name:
        m = re.search(r"(\.[0-9A-Za-z]{1,8})$", str(name))
        if m:
            return m.group(1).lower()
    return _EXT.get(str(kind).lower(), ".bin")


#: 飞书在正文里把 @ 显示成 `@_user_1`，真名在 `mentions[]` 里。
_MENTION_RE = re.compile(r"@_user_\d+")


def replace_mentions(text: str, mentions: list | None) -> str:
    """把 `@_user_1` 换成 `@真名`。

    不换的话，模型在群里看到的是 `@_user_1 帮我看下这个` —— 它既不知道
    是谁，也没法在回复里称呼对方。
    """
    if not text or not mentions:
        return text or ""
    table: dict[str, str] = {}
    for m in mentions:
        if not isinstance(m, dict):
            continue
        key = str(m.get("key") or "")
        name = str(m.get("name") or "").strip()
        if key and name:
            table[key] = name
    if not table:
        return text
    def _sub(mo: "re.Match[str]") -> str:
        key = mo.group(0)
        name = table.get(key)
        # 只给换得到的补 `@` —— 否则 `@_user_2`（表里没有）会变成 `@@_user_2`
        return ("@" + name) if name else key
    return _MENTION_RE.sub(_sub, text)


_HEADING_RE = re.compile(r"^#{1,6}\s*(.+?)\s*#*$", re.M)


def to_card(text: str) -> str:
    """markdown 文本 → 卡片 JSON 字符串（`msg_type=interactive`）。

    `lark_md` 认：加粗 / 斜体 / 删除线 / 链接 / 行内代码 / 换行 / 列表。
    不认标题与表格 —— 标题改写成加粗（否则 `##` 原样露出来，比 text 更难看）；
    表格保持原样（它至少还是能读的文字），不假装能渲染。
    """
    body = _HEADING_RE.sub(lambda m: "**" + m.group(1) + "**", str(text or ""))
    card = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
        ],
    }
    return json.dumps(card, ensure_ascii=False)


#: 卡片里值得抽出来的键（不同卡片结构差别很大，只认这几个通用名）。
_CARD_TEXT_KEYS = ("title", "text", "content")


def card_plain_text(obj: Any, limit: int = 2000) -> str:
    """从一个 interactive 卡片里尽力抽出人话（抽不到就返回空串）。

    这是尽力：卡片是任意 JSON，我们不解析它的布局，只把
    `title` / `text` / `content` 里的字符串按出现顺序拼起来、去重。
    抽不到时调用方要给一句"（内容未解析）"，不能静默。
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if len(seen) >= 40 or depth > 12:
            return
        if isinstance(node, dict):
            for key in _CARD_TEXT_KEYS:
                val = node.get(key)
                if isinstance(val, str):
                    add(val)
                elif isinstance(val, dict):
                    walk(val, depth + 1)
            for key, val in node.items():
                if key not in _CARD_TEXT_KEYS:
                    walk(val, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    def add(s: str) -> None:
        t = s.strip()
        if not t or t in seen_set:
            return
        seen_set.add(t)
        seen.append(t)

    walk(obj, 0)
    return "\n".join(seen)[:limit]
