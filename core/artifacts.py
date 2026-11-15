"""产物底座 —— 一张图一条记录，UI 只拿 id。

## 它解决什么

模型/工具产出的图片、音频、视频在此之前没有落点：`plot_excel_data` 只回一句
`output_path`，聊天区里什么都看不到；上游生图返回的 base64 更是直接丢掉。
这一层把「一个产物」变成一条可以挂在消息上、可以被浏览器取回的记录。

## 三条不变量

1. 内容寻址：`sha256(bytes)[:16] + 扩展名`。同一张图产出两次只有一个文件；
   `id` 由字节决定，不由路径/名字决定（与 `image_input._spill` 同一条判据）。
2. 按字节嗅探类型，不信扩展名（扩展名是声明，魔数是证据）。
   认不出来就不收 —— 宁可少一张图，也不把一份未知容器用 `image/png` 发出去。
3. 端点只认 id，不认路径（`GET /api/artifact?id=…`）。`resolve()` 只会在
   `data/attachments/` 下找那 16 位十六进制 + 白名单扩展名的文件。

## 记录形状（落库、发给前端的就这一份）

```json
{"id": "a1b2c3d4e5f6a7b8.png", "kind": "image", "name": "销售趋势.png",
 "mime": "image/png", "size": 12345, "inline": true, "origin": "tool"}
```

磁盘路径绝不进记录 —— UI 只拿 `id`，路径由 `resolve()` 现算
（一个口径一个地方）。

## 两个闸，不是一道

在此之前只有一套上限，而它同时管两件不同的事：能不能存 与
能不能在聊天区直接画/播。「落盘不设限，单个 > 1GB 给个通知」
两件事必须分开：

* 落盘闸（`save_*`）：只拒「空」与「认不出类型」。没有大小上限
  （文件由用户自己管；要清理是他的事）；
* 内联闸（`INLINE_MAX_BYTES_*`）：超过它照样落盘，但记录里 `inline: false`
界面给一行下载，不塞 `<img>/<video>`（判据是"浏览器要不要一次读完"）。

于是「大文件被静默丢掉」这个失败形状消失了：它要么内联、要么是一条下载项，
不会两者都不是。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

# 与图片输入同一个目录：它们都是"这一轮带进来的字节"，
#   分成两个目录只会让「内容寻址」在两个地方各算一份（同一个理由）。
from core.image_input import ATTACHMENT_DIR, sniff_media_type

#: 内联闸：不超过它才在聊天区直接画 / 播。判据不是"能不能解开"，
#: 是"浏览器/播放器要不要一次读完"。
#: 它不是落盘上限 —— 超了照样存，只是记录里 `inline: false`（见模块 docstring）。
#: SVG 那一格不是 kind 的上限 —— 它是同一 kind 里的特例（见 `_inline_cap_for`）。
INLINE_MAX_BYTES_IMAGE = 20 * 1024 * 1024
INLINE_MAX_BYTES_AUDIO = 20 * 1024 * 1024
INLINE_MAX_BYTES_VIDEO = 50 * 1024 * 1024
INLINE_MAX_BYTES_SVG = 200 * 1024


#: 每次工具回执里最多收几条 —— 一个产 50 张图的工具会把一条消息撑爆。
MAX_PER_RESULT = 12

#: `id` 可能带的扩展名 —— 唯一来源（`_EXT_BY_MIME` 的值 + `.jpeg` 别名）。
#: 回收器（`core/attachment_gc.py`）也从这一份拼正则：两处各写一张表，
#:   加一种媒体时只改一处就会让另一处永远不回收（批 6 之前正是这样：
#:   GC 只认 png/jpg/webp/gif，音视频永不回收）。
#: 定义必须排在 `_EXT_BY_MIME` 之后 —— 它读的就是那张表。

#: `id` 的唯一合法形状。端点拿它当第一道闸（不合法的直接 400，
#: 与"合法但不存在"的 404 分得开）。

#: mime -> 扩展名。这是 id 后缀的唯一来源（反向表由它推，不手写第二份）。
_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

#: 扩展名 -> 回给浏览器的 mime（端点用；`.jpg` 与 `.jpeg` 都收）。
_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
for _m, _e in _EXT_BY_MIME.items():
    _MIME_BY_EXT.setdefault(_e, _m)

#: `id` 可能带的扩展名 —— 唯一来源（`_EXT_BY_MIME` 的值 + `.jpeg` 别名）。
#: 回收器（`core/attachment_gc.py`）也从这一份拼正则：两处各写一张表，
#:   加一种媒体时只改一处就会让另一处永远不回收（批 6 之前正是这样：
#:   GC 只认 png/jpg/webp/gif，音视频永不回收）。
ID_EXTENSIONS: tuple[str, ...] = tuple(sorted(
    {e.lstrip(".") for e in _EXT_BY_MIME.values()} | {"jpeg"}))

#: `id` 的唯一合法形状。端点拿它当第一道闸（不合法的直接 400，
#: 与"合法但不存在"的 404 分得开）。
_ID_RE = re.compile(
    r"^[0-9a-f]{16}\.(?:" + "|".join(ID_EXTENSIONS) + r")$")

#: 允许出现的 kind。`other` 不收（认不出就不收，见模块 docstring 第 2 条）。
_KINDS = ("image", "audio", "video")

#: 记录名最多多长（显示用；超了截断 —— 名字不是判据，不影响取回）。
_MAX_NAME = 120

#: 进程内的诊断计数（端点把不合法的 id 记一笔；有上限 —— 它是 untrusted 输入）。
_rejected_ids: set[str] = set()
_REJECTED_MAX = 64


# 一、类型嗅探（图片复用 image_input；音频/视频/SVG 在这里补）

def _sniff_av(data: bytes) -> Optional[str]:
    """认音频 / 视频 / SVG；认不出返回 `None`。

    判据同 `sniff_media_type`：读不到 ≠ 不是。
    """
    if not data:
        return None

    # SVG：文本，不是二进制魔数
    head = data[:4096]
    if b"<" in head:
        try:
            text = head.decode("utf-8", "ignore").lstrip("﻿ \t\r\n")
        except Exception:                     # noqa: BLE001 — 解不开就当不是 SVG
            text = ""
        low = text[:1024].lower()
        if low.startswith("<svg") or (
                low.startswith("<?xml") and "<svg" in low):
            return "image/svg+xml"

    if len(data) < 12:
        return None

    # RIFF 一族：WAV / WEBP / AVI（WebP 在上面那个函数里，这里只认 WAV）
    if data[:4] == b"RIFF":
        if data[8:12] == b"WAVE":
            return "audio/wav"

    # OGG
    if data[:4] == b"OggS":
        return "audio/ogg"

    # ISO BMFF（mp4 / m4a）：第 4 字节起是 `ftyp`
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P ", b"F4A "):
            return "audio/mp4"
        return "video/mp4"

    # Matroska / WebM
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"

    # MP3：ID3 标签，或帧同步（11 个 1）
    if data[:3] == b"ID3":
        return "audio/mpeg"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # 层号不能是 0（那是保留值）；简单判一下，避免把随机字节当音频
        if ((data[1] >> 1) & 0x03) != 0:
            return "audio/mpeg"

    return None


def sniff(data: bytes) -> Optional[str]:
    """这一块字节到底是什么 —— 图片走 `image_input`，其余在这里认。"""
    m = sniff_media_type(data)
    if m:
        return m
    return _sniff_av(data)


def kind_of(mime: str) -> str:
    """mime -> `image` / `audio` / `video` / `""`（认不出）。"""
    if not mime:
        return ""
    for k in _KINDS:
        if mime.startswith(k + "/"):
            return k
    return ""


def _inline_cap_for(mime: str) -> int:
    """这一类产物能不能内联的闸。一项定义一处：直接读模块常量，
    不在模块层再抄一张 `kind -> 上限` 的字典（那张抄本会在改上限时静默过期）。"""
    if mime == "image/svg+xml":
        return INLINE_MAX_BYTES_SVG
    return {
        "image": INLINE_MAX_BYTES_IMAGE,
        "audio": INLINE_MAX_BYTES_AUDIO,
        "video": INLINE_MAX_BYTES_VIDEO,
    }.get(kind_of(mime), INLINE_MAX_BYTES_IMAGE)


def _clean_name(name: str, fallback: str) -> str:
    """只留展示用的名字：去目录、去控制字符、限长。"""
    raw = str(name or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = "".join(ch for ch in raw if ch >= " " and ch != "\x7f").strip()
    return (raw or fallback)[:_MAX_NAME]


# 二、写入（内容寻址）

_CHUNK = 1024 * 1024
#: 嗅探只看开头这么多字节（最大那个魔数在 `_sniff_av` 里要 4096）。
_HEAD = 64 * 1024


def _store(read, *, mime: str = "", name: str = "",
           origin: str = "tool") -> Optional[dict]:
    """把一串字节流式存成产物，返回记录；认不出 / 空 / 存不下，`None`。

    它是唯一的落点（`save_bytes` 与 `save_path` 都走这里）——
    "什么算一个产物、记录长什么样、内联闸怎么判"只能有一份，否则两条入口
    迟早给出两种记录。

    不设大小上限：判据是「落盘不设限」。
       大文件只影响 `inline`（能不能在聊天区直接画/播），不影响存不存。
    """
    first = read(_HEAD)
    if not first:
        return None
    real = sniff(first)
    if real is None or real not in _EXT_BY_MIME:
        print("[artifact] 认不出类型，不收（声明为 %r）" % (str(mime or "")[:40]))
        return None
    kind = kind_of(real)
    if kind not in _KINDS:
        return None

    digest = hashlib.sha256(first)
    total = len(first)
    d = ATTACHMENT_DIR
    rid = ""
    tmp_path = ""
    try:
        d.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再换名：读到一半崩掉也不会在内容寻址的目录里
        #   留下半张图（那个 id 以后再也不会被重写 —— 内容寻址的代价）。
        fd, tmp_path = tempfile.mkstemp(prefix=".in-", dir=str(d))
        with os.fdopen(fd, "wb") as f:
            f.write(first)
            while True:
                chunk = read(_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                f.write(chunk)
                total += len(chunk)
        rid = digest.hexdigest()[:16] + _EXT_BY_MIME[real]
        p = d / rid
        if p.exists():              # 内容寻址，同一份字节只写一次
            os.unlink(tmp_path)
        else:
            os.replace(tmp_path, p)
        tmp_path = ""
    except OSError as exc:          # noqa: BLE001 — 存不下不该弄挂这一轮
        print("[artifact] 落盘失败（%s）：%s" % (rid or "?", exc))
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    cap = _inline_cap_for(real)
    inline = total <= cap
    if not inline:
        # 出声，但不刷屏：这是"存下来了、只是不内联"，不是失败。
        print("[artifact] %s 超过内联闸（%d > %d 字节），只给下载"
              % (kind, total, cap))

    return {
        "id": rid,
        "kind": kind,
        "name": _clean_name(name, rid),
        "mime": real,
        "size": total,
        "inline": inline,
        "origin": str(origin or "tool"),
    }


def save_bytes(data: bytes, *, mime: str = "", name: str = "",
               origin: str = "tool") -> Optional[dict]:
    """把一块已经在内存里的字节存成产物（data URL 那条路）。

    `mime` 只是提示：真正判类型的是 `sniff`（扩展名与上游那句话都不是证据）。
    """
    if not data:
        return None
    return _store(io.BytesIO(data).read, mime=mime, name=name, origin=origin)


def save_data_url(url: str, *, name: str = "",
                  origin: str = "model") -> Optional[dict]:
    """从一条 data URL 收成产物（上游媒体的唯一入口）。

    只认 `data:<mime>;base64,<...>`。`http(s)` 外链一律不收 ——
    下载它等于绕开出网闸。
    """
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    header, sep, payload = url.partition(",")
    if not sep or ";base64" not in header:
        return None
    try:
        import base64
        data = base64.b64decode(payload, validate=False)
    except Exception:                    # noqa: BLE001 — 坏 base64 ⇒ 不收
        return None
    mime = header[5:].split(";", 1)[0].strip().lower()
    return save_bytes(data, mime=mime, name=name, origin=origin)


def save_path(path, *, name: str = "", origin: str = "tool") -> Optional[dict]:
    """从一个本地文件收成产物。文件不存在 / 空 / 读不动，`None`。

    流式读：以前是 `read_bytes()`，所以一个 2 GB 的视频
    先要整个进内存 —— 而它真正的用处正是"把大文件交给用户"。现在按块算哈希、
    按块写盘，内存占用与文件大小无关。
    不再按大小丢弃：落盘不设限，大小只影响 `inline`。
    """
    if not path:
        return None
    try:
        p = Path(str(path))
        if not p.is_file():
            return None
        if p.stat().st_size <= 0:
            return None
        f = p.open("rb")
    except OSError:
        return None
    try:
        return _store(f.read, name=name or p.name, origin=origin)
    finally:
        f.close()


# 三、读回（端点唯一的入口）

def valid_id(rid: str) -> bool:
    """`id` 形状是否合法 —— 端点用它区分 400 与 404。"""
    return bool(_ID_RE.match(str(rid or "")))


def resolve(rid: str) -> Optional[Path]:
    """`id` -> 磁盘路径；不合法或不存在，`None`。

    只在 `data/attachments/` 下找，绝不接受调用方给的路径。
    """
    if not valid_id(rid):
        if len(_rejected_ids) < _REJECTED_MAX:
            _rejected_ids.add(str(rid)[:40])
        return None
    p = ATTACHMENT_DIR / str(rid)
    return p if p.is_file() else None


def mime_for_id(rid: str) -> str:
    """`id` -> 回给浏览器的 mime（由扩展名推，不读文件）。"""
    return _MIME_BY_EXT.get(Path(str(rid)).suffix.lower(), "application/octet-stream")


# 四、工具回执里的产物

def collect_tool_artifacts(result) -> list[dict]:
    """从一条工具回执里收产物，并把那个键从回执里删掉。

    契约（可选字段，manifest 不用改）::

        {"success": true, "output_path": "...",
         "artifacts": [{"path": "...", "name": "..."}]}

    为什么要删：这个键是框架 ↔ 界面的，模型不需要它
    （模型有 `output_path` 就够了）。留着 = 每条请求多付一份路径文本，
    而且下一轮它可能照着去 `read_file` 一张图（那不是我们要的用法）。

    收不下的一条不出声（工具作者没做错什么），但整批收不下会打一行。
    """
    if not isinstance(result, dict):
        return []
    items = result.pop("artifacts", None)
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in items[:MAX_PER_RESULT]:
        if isinstance(item, str):
            path, name = item, ""
        elif isinstance(item, dict):
            path = item.get("path") or item.get("file_path") or ""
            name = item.get("name") or ""
        else:
            continue
        rec = save_path(path, name=name, origin="tool")
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            out.append(rec)
    return out


#: 回复里的 Markdown 链接 → 本机文件时，只有这些扩展名才收成产物。
#: 判据：它们是"看得见/听得到"的东西；`.pdf` / `.docx` 仍走原来的"打开文件"那条路。
_LINK_MEDIA_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm",
})

#: 一条回复最多收几张 —— 模型偶尔会把同一个目录里的图全列一遍。
MAX_LINKS_PER_REPLY = 6

_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\n]+)\)")


def _resolve_local(url: str, roots: list) -> Optional[Path]:
    """把回复里的链接解析成本机路径；不是本机媒体 / 不在根里，`None`。

    只在 `lookup_roots()` 给的根里找 —— 与"打开文件夹"（`/api/reveal`，
    可以点桌面上的任意文件）不是同一条边界：这里会把文件内容读进产物库
    并显示给用户，所以只认工作区/工作组那几棵树的。
    """
    raw = str(url or "").strip().strip("<>").strip().strip('"').strip("'")
    if not raw or raw.startswith("#"):
        return None
    # 明确的 scheme（http/data/mailto…）不是本机路径；Windows 盘符要放行。
    if not re.match(r"^[A-Za-z]:[\\/]", raw) and re.match(
            r"^[A-Za-z][A-Za-z0-9+.-]*:", raw):
        return None
    # 模型会把空格写成 `%20`；先原样、再解码一次
    #   （与 `/api/reveal` 同一条顺序：真带 `%20` 的文件名优先）。
    variants = [raw]
    if "%" in raw:
        try:
            from urllib.parse import unquote
            decoded = unquote(raw)
            if decoded != raw:
                variants.append(decoded)
        except Exception:                        # noqa: BLE001
            pass

    root_dirs = []
    for r in roots:
        try:
            root_dirs.append(Path(r).resolve())
        except OSError:
            continue

    def _under_any(rp: Path) -> bool:
        return any(rp == r or r in rp.parents for r in root_dirs)

    for value in variants:
        p = Path(value)
        if p.suffix.lower() not in _LINK_MEDIA_EXTS:
            continue
        cands = ([p] if p.is_absolute()
                 else [Path(r) / p for r in roots] + [p])
        for c in cands:
            try:
                if not c.is_file():
                    continue
                rp = c.resolve()
                # 落在根里才收 —— 相对路径也不例外：`../outside.png`
                #   解析出来在根之外，那是"模型让我们读了工作区以外的文件"，
                #   直接不收（读内容与"打开文件夹"是两条边界）。
                if not _under_any(rp):
                    continue
                return rp
            except OSError:
                continue
    return None


def collect_local_links(text: str, *, roots=None) -> list[dict]:
    """把回复里指向本机媒体文件的 Markdown 链接收成产物。

    失败模式：模型没写围栏，而是把文件存进 `workspace/`、
    再在回复里给一个链接，只认围栏的话，用户看到的是一行链接而不是图。
    这一条把那种"我把文件做好了"的回复接进同一条呈现链（缩略图 / 播放器）。

    边界：只在 `lookup_roots()` 里找、只收媒体扩展名、按字节嗅探、
    大小只影响 `inline`（与 `save_path` 同一条，落盘不设限）；
    不读任意绝对路径（与 `/api/reveal` 不同）。
    """
    body = str(text or "")
    if not body:
        return []
    if roots is None:
        try:
            from core.workspace import lookup_roots
            roots = lookup_roots()
        except Exception:                        # noqa: BLE001
            roots = [Path("workspace")]
    out: list[dict] = []
    seen: set[str] = set()
    for m in _MD_LINK.finditer(body):
        if len(out) >= MAX_LINKS_PER_REPLY:
            break
        p = _resolve_local(m.group(1), roots)
        if p is None:
            continue
        rec = save_path(p, name=p.name, origin="file")
        if rec and rec["id"] not in seen:
            seen.add(rec["id"])
            out.append(rec)
    return out


def records_from_messages(messages, start: int = 0) -> list[dict]:
    """从消息里最后一条带产物的 assistant 上取记录。只读。

    与 `_attach_artifacts`（`core/agent_loop.py`）同一个锚点 —— 挂哪儿、
      从哪儿取，是一件事的两面；分两处写就会各自漂。

    *start* 限定"只看这一轮新增的那段"（上一轮挂的不该在本轮再报一遍）。
    找不到，`[]`，绝不发明一个空记录。
    """
    try:
        window = messages[start:] if start >= 0 else messages
        for m in reversed(window):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            arts = m.get("artifacts")
            if isinstance(arts, list) and arts:
                return [a for a in arts if isinstance(a, dict)]
    except Exception:                            # noqa: BLE001 — 观测不许弄挂这一轮
        return []
    return []


def reset_for_tests() -> None:
    """清掉进程内的诊断状态（不删文件 —— 内容寻址的目录是共享的）。"""
    _rejected_ids.clear()


__all__ = [
    "INLINE_MAX_BYTES_IMAGE", "INLINE_MAX_BYTES_AUDIO",
    "INLINE_MAX_BYTES_VIDEO", "INLINE_MAX_BYTES_SVG",
    "MAX_PER_RESULT",
    "sniff", "kind_of",
    "save_bytes", "save_path", "save_data_url", "resolve", "valid_id", "mime_for_id",
    "collect_tool_artifacts", "collect_local_links", "MAX_LINKS_PER_REPLY",
    "records_from_messages", "reset_for_tests",
]
