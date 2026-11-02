"""图片输入 —— 唯一的图片规范化处。

## 它解决什么

通道转换器会造出 `channels.message.Image` 组件，而 `MessageChain.content`
把它渲染成一句 `[Image: /path/x.jpg]` —— 模型看到的是一行字。
常见场景里「截图给我看看」是很常见的输入形态之一，所以图片必须能真的
以图片形式到达模型。

## 三条判据

1. 走 provider 的原生 image block —— 不把 base64 塞进文本字段指望模型去解码。
   本仓的内部消息格式本来就是 OpenAI 形状（`role` / `tool_calls` /
   `tool_call_id`），13 个 provider 里 11 个是 OpenAI 兼容，规范形状就用
   OpenAI 的 content part：
   ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}``。
   Anthropic 那条路由 `providers/anthropic/client.py` 翻译（它本来就有翻译层）。

2. 图片是"不可信内容" —— 像素扫不了，但元数据能：
   一个 PNG 的 `tEXt` 块、一张 JPEG 的 EXIF `ImageDescription`，
   都是任意文本的免费载体，而模型看得见它们。一律重新编码，
   元数据在这一步掉光（顺带把 EXIF 的旋转烘进像素，见 `_normalize`）。

3. 有上限，超了要说出来 —— 不静默砍。
   每次规范化产出一串 `notes`：「已降采样 4032×3024 -> 2000×1500」
   「EXIF 已剥离」「动图只取第一帧」。这些话会跟着这一轮进上下文。

## 两条与别处的接口

* `content_text(content)` —— 消息 `content` 的纯文本视图，全仓唯一一处。
  消息的 `content` 平时是 `str`，带图那一轮是块列表；而 SQLite 的
  `content` 是 TEXT 列（绑一个 list 直接 `InterfaceError`），
  压缩闸门 / 分类器 / ICE / 统计都按字符串用它。
谁要把 `content` 当字符串，就走这里（别在别处再写一份 `isinstance` 分支）。
* `offload_old_images(messages, keep=)` —— 老图片换成一句带路径的话。
  判据来自四类裁剪里的第四类（图片卸载）：图片在历史里每一轮都重发，
  不卸载的话账单随轮数线性涨。
* `ImageToolResult` —— 工具回执也可以带图（「路径图」那一半）。
  落点是 `read_file`（不新开工具 —— 文本与图片同一件已有先例，
  而新开一个常驻工具 = 每请求多付一份 schema 的永久税）。
  文本与图分开放：文本要过截断与 `<user_document>`，图块在包裹之外。

## 图片存哪

`data/attachments/<sha16>.<ext>` —— 内容寻址：同一张图粘两次只有一个文件。
形状照 `_truncate_to_artifact` 的先例（大块落盘 + 消息里留真能取回来的指针）：
消息落库的是文字，图片留在盘上。
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# 限额
#
# ## 这一组数的依据
#
# | 数 | 值 | 为什么是它 |
# |---|---|---|
# | 长边 | 2000 | 够看清截图里的字，再大只是多花 token |
# | 单张 | base64 5 MB | 量的是真正发出去的那个串，不是原图 |
# | 每轮张数 | 20 | 与合计字节上限配套 —— 单看张数管不住体积 |
# | 单张源文件 | 20 MiB | 原图会被降采样重编码，所以比发出去的那条宽 |
# | 每轮合计 | 40 MiB | 真正管住体积的是这一条 |
#
# 长边那条最值得记：1568 是 API 服务端内部的缩放阈值，
# 客户端用 2000 —— 拿服务端的阈值当"客户端该用多少"是错的。
#
# "每轮张数"单独管不住体积：20 张 × 5 MB = 100 MB。两个一起上才闭合 ——
# 张数管"几张"，合计字节管"多大"，缺一个都会在某一类输入上漏出去。

#: 长边上限（像素）。2000。
#: 1568 是服务端内部的缩放阈值，客户端取 2000。
#: 改小 = 截图里的字开始糊；改大 = 每张图多花 token 而模型看不出区别。
MAX_EDGE_PX = 2000

#: 单张图片在 wire 上（base64 之后）的上限。
#: 它量的都是base64 字符串长度，
#: 因为那才是真正发出去的东西。
MAX_BASE64_BYTES = 5 * 1024 * 1024

#: 归一化之后单个文件的字节上限 —— 由上面那个数反推
#: （原始大小 = `(5MB*3)/4`）。
#: 判定仍然打在原始字节上（`_encode` 比的就是它）——
#: 这样"限额"与"发出去的大小"是同一条约束的两种写法，不会漂。
MAX_IMAGE_BYTES = (MAX_BASE64_BYTES * 3) // 4

#: 一轮最多几张 —— 20。它是经验值，不是从量的分布推出来的；
#: 它和下面的合计字节上限配套用。
#: 超出的不丢、换成一句带路径的话（判据 3：不静默）。
MAX_IMAGES_PER_TURN = 20

#: 一轮的合计字节上限（归一化后）—— 40 MiB，按每请求的体量预算定的。
#: 这一条才是真正管住体积的那个：张数只管"几张"，管不住"多大"。
#: 判据是"超了就报数，不静默"——不砍单张（那会把好的图弄坏），
#: 而是收不下后面的并说清。
MAX_TURN_TOTAL_BYTES = 40 * 1024 * 1024

#: 输入的原始字节上限 —— 20 MiB。
#: 比 `MAX_IMAGE_BYTES` 宽（原图可以更大，因为它会被降采样重编码），
#: 但仍要有顶 —— 否则一个 500 MB 的 TIFF 会先被读进内存。
#:
#: 判据是"这个数必须在读之前生效"：`stat()` 先判，然后有界读
#: （最多读上限 + 1 字节）。先整读再判，等于上限只保护了后面的步骤。
MAX_SOURCE_BYTES = 20 * 1024 * 1024

#: 源图像素总量上限 —— 在 `load()` 之前判。
#:
#: 文件字节小不代表像素少：一张几 MB 的 PNG 可以声明上亿像素，而真正的
#: 内存分配发生在解码那一刻。5000 万像素够 48 MP 的手机照片过，
#: 又不给压缩炸弹留空间。
MAX_PIXELS = 50_000_000

#: 视频源文件上限。抽帧是流式读的（不整读），这条防的是"把一个没打算
#: 发出去的大文件拖进解码器"；真正占内存的是帧尺寸，那一格用 `MAX_PIXELS`。
MAX_VIDEO_SOURCE_BYTES = 512 * 1024 * 1024

#: 模态路由的三个常量。
#: 音频直发的源文件上限 —— 与图片同一个数（同一个"先读进内存"的风险）。
MAX_AUDIO_SOURCE_BYTES = MAX_SOURCE_BYTES
#: OpenAI 兼容 `input_audio.format` 的白名单。不在名单里的不猜 ——
#: 换成一句可见说明（读不到 ≠ 能假装读到）。
AUDIO_WIRE_FORMATS: tuple[str, ...] = ("mp3", "wav")
#: 视频最多抽几帧。用现成的 `cv2`（OpenCV 自带解码器），零新依赖；
#: 抽出来的帧走图片链，所以模型收不收图由 `sees_images` 那一格管。
MAX_VIDEO_FRAMES = 3
#: 音频 token 的估算额度（不是账单）：上游按编解码器算，我们只求量级对。
#: 实际口径约 10 token/秒；时长读不到时给 1000（读不到不按 0 算）。
AUDIO_TOKEN_PER_SECOND = 10
AUDIO_TOKEN_FALLBACK = 1000

#: 请求里最多保留几张图。比 `MAX_IMAGES_PER_TURN` 宽 ——
#: 那一格管"一轮能贴几张"，这一格管"历史里能留几张"。
#: 保留最近 N 张而不是"最近 N 轮"：一轮四张图是常态，
#: 按轮算会让一个四图轮把预算吃光是同一条行为，但按张算更好解释。
KEEP_IMAGES_IN_REQUEST = 2

#: 认这四种。判据来自附件校验（列的就是这四个），
#: 也正好是 OpenAI 兼容端点普遍接受的集合。
SUPPORTED_MEDIA: tuple[str, ...] = (
    "image/png", "image/jpeg", "image/webp", "image/gif",
)

#: 归一化后的落盘目录（相对路径 —— 与 `short_term.db` 同一条判据：
#: 相对路径按 cwd 解析，所以 `tests/_sandbox.py::sandbox()` 能把它关进临时目录）。
ATTACHMENT_DIR = Path("data") / "attachments"

#: 重编码时 JPEG 的质量。95 而不是 85：**这一步的目的是剥元数据与限尺寸，
#: 不是省字节** —— 顺手把用户的截图压糊是另一种"静默改坏"。
_JPEG_QUALITY = 95

#: 降采样用的重采样核。LANCZOS 是缩小时最不糊的那个（Pillow 文档口径）。
#: Pillow 10 起字符串名不再被接受（Pillow 12 传 `"lanczos"` 直接
#: `ValueError: Unknown resampling filter`）—— 必须用枚举。
#: 于是它不能在模块层求值（那会把 Pillow 拖进每一次 import，
#: 而 `content_text` 是全仓都要用的），由 `_resample()` 现取。
_RESAMPLE_FALLBACK = 1          # Image.BILINEAR 的旧数值，只在没有枚举时用


def _resample():
    """重采样核 —— 取一次，取不到就退到数值常量。"""
    from PIL import Image
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS",
                   _RESAMPLE_FALLBACK)


class ImageInputError(RuntimeError):
    """这张图收不下。

    ## 两个读者、两个字段

    `str(exc)` 会进模型的上下文（`prepare_chain` 把它拼成一句 note，
    而 note 并进这一轮的正文）。所以：

    * `str(exc)`（= `brief`）—— 给模型的：一句话，说清"这张没收下、
      是什么性质"。必须短，不许写成一份排障指南。
    * `.detail` —— 给控制台/开发者的：判据、两种可能的成因、去看哪一行。
      它绝不进上下文（`prepare_chain` 只 `print` 它）。

    判据：决定一条文本去哪儿的，是它的读者是谁，不是它有多重要。
    框架自己的诊断有它自己的出口（stdout），别顺手塞进对话；否则模型
    读到"去看日志"就判定"这是个待修的问题"，跑去找工具修，白烧轮次。
    """

    def __init__(self, brief: str, detail: str = "") -> None:
        super().__init__(brief)
        self._brief = brief
        self.detail = detail or brief

    @property
    def brief(self) -> str:
        return self._brief


# 一、类型判定：看字节，不看扩展名
#
# 判据：扩展名是一个声明，魔数是证据。`.png` 的 JPEG、以及压根
# 没有扩展名的粘贴数据，都从这里过。
# 这条不是安全边界 —— 它的作用是"别拿一个我们不认识的容器去喂 Pillow"，
# 真正的把关在下面的重编码（Pillow 自己会拒掉解不开的东西）。

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_media_type(data: bytes) -> Optional[str]:
    """从开头几个字节认出媒体类型；认不出返回 `None`。

    `None` 是"认不出"，不是"不是图片"（读不到 ≠ 0）。
    调用方拿到它要换成一句实话，不是当成 JPEG 硬塞。

    WebP 要看到第 8 字节（`RIFF....WEBP`），所以它单独判。
    """
    if not data or len(data) < 12:
        return None
    for magic, media in _MAGIC:
        if data.startswith(magic):
            return media
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


#: 认得出但发不出去的图片容器。判据不是"能不能解开"（Pillow 解开 TIFF 没
#: 问题），是"上游收不收" —— 收不下的时候要给的是一句说得准的话
#: （「这是一张 BMP，只收 PNG/JPEG/WebP/GIF」），而不是「看起来是二进制文件」。
#: 后者诚实但没路线（路径图那一半的止血点就在这里）。
_EXTRA_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"8BPS", "image/vnd.adobe.photoshop"),
    (b"qoif", "image/qoi"),
)

#: 给人看的容器名（错误话术里用）。认不出的一律回落到媒体类型本身 ——
#: 编一个好听的名字比说不知道更坏。
_IMAGE_KIND: dict[str, str] = {
    "image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WebP",
    "image/gif": "GIF", "image/bmp": "BMP", "image/tiff": "TIFF",
    "image/x-icon": "ICO", "image/vnd.adobe.photoshop": "PSD",
    "image/avif": "AVIF", "image/heic": "HEIC", "image/qoi": "QOI",
    "image/svg+xml": "SVG",
}


def looks_like_image(head: bytes) -> Optional[str]:
    """这是不是一张图片（含我们发不出去的那些），媒体类型，否则 `None`。

    ## 它与 `sniff_media_type` 的分工

    那个只认四种能发的（`SUPPORTED_MEDIA`），用途是"拿什么去喂 wire"。
    这个还认 BMP / TIFF / ICO / PSD / AVIF / HEIC / SVG —— 用途是
    把话说准：`read_file` 撞上一张图时，能说出"这是一张 BMP"，
    而不是"看起来是二进制文件"。

    两者判据不同，所以是两个函数：合成一个的话，
    "能发"与"认得"这两件事会挤在一处，而它们的失败方向相反
    （前者要保守，后者要尽量认出来）。

    SVG 单独判（它是文本，没有魔数）：`<svg` / `<?xml` 开头。
    这条只在 `read_file` 的止血话术里有用 —— SVG 永远进不了图片管线
    （`prepare_bytes` 走的是 Pillow，Pillow 不认 SVG）。
    """
    if not head:
        return None
    found = sniff_media_type(head)
    if found is not None:
        return found
    for magic, media in _EXTRA_IMAGE_MAGIC:
        if head.startswith(magic):
            return media
    # ISO-BMFF（HEIC / AVIF 一族）：`....ftyp<brand>`
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].strip()
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1"):
            return "image/heic"
    # SVG / 别家 XML 图形：剥掉前导空白与 BOM 再看
    probe = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if probe.startswith(b"<svg") or probe.startswith(b"<?xml"):
        return "image/svg+xml"
    return None


def image_kind_name(media: str) -> str:
    """给人看的容器名（错误话术里用）。认不出就回落到媒体类型本身 ——
    编一个好听的名字比说不知道更坏。"""
    return _IMAGE_KIND.get(media, media)


# 二、产物


@dataclass
class PreparedImage:
    """一张已经可以发出去的图。"""

    #: 发到 wire 上的那个 content part（OpenAI 形状，见模块 docstring 判据 1）。
    block: dict
    #: 归一化之后的字节落在哪 —— 这是落库那句占位里要写的路径。
    path: Path
    media_type: str
    width: int
    height: int
    byte_size: int
    #: 它从哪来（给人看的一句话：`粘贴` / `本地文件 截图.png` / `data URL`）。
    source: str = ""
    #: 这一步动了什么 —— 降采样 / 剥 EXIF / 转码 / 动图取首帧。
    #: 非空就要进上下文（判据 3）。空列表 = 一个字节都没动。
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """一行给人的说明（也进模型看到的那个 text 块）。"""
        base = "%d×%d %s" % (
            self.width, self.height,
            {"image/png": "PNG", "image/jpeg": "JPEG",
             "image/webp": "WebP", "image/gif": "GIF"}.get(self.media_type,
                                                           self.media_type))
        if self.source:
            base += "，" + self.source
        return base

    # 指针格式必须与 `_POINTER_RE` 同源：那句反解式（`content_text` 与
    # `paths_in_text` 共用）要求指针带序号。不带序号的指针装不回来，
    # 症状是"第二句问同一张图时模型看不见图"（静默丢的一种形态）。
    #一个看着像指针产者、产出的东西读者认不出的方法，比没有它更坏。


# 二之二、工具回执「文本 + 图」的形状（路径图那一半）
#
# ## 为什么需要一个具名类
#
# 工具 handler 的返回值今天是 `str`（纯文本回执）或者任意对象
# （循环里 `json.dumps`）。要让"回执里带一张图"成为可能，最省事的做法是
# 让 handler 直接返回一个块列表 —— 而那条路是错的：
# `as_text()` 对认不出的形状一律 `json.dumps`，于是"这是不是块列表"
# 就变成了类型判断，而任何一天某个工具恰好返回 `list[dict]`
# （`glob` 那种本来就返回列表）都会被静默当成图片块。
#
# 判据与 `PLAIN_TEXT_TOOLS` 互为镜像：那里是"认名单不认类型"
# （一个恰好返回 `str` 的工具不该被换掉 wire 形状），这里是"必须有一个
# 可认的标记"。两者是同一条：别让类型成为语义。


@dataclass
class ImageToolResult:
    """工具回执里带图时的形状：一段文本 + N 张已经归一化好的图。

    文本与图分开放，因为它们要去不同的地方：
      · 文本走 `_format_tool_result`（截断 / 落盘 / `<user_document>` 包裹）；
      · 图片块在包裹之外 —— 它没有可打码的字符，`sanitize_text` 对它
        无从下手，而塞进包裹里只会得到一个假的边界感。

    图必须已经落过盘（`prepare_*` 的产物）：`content_text` 要从
    data URL 反推路径（内容寻址），反推不出来时那句指针就只剩 `[图片 1]`，
    跨轮取回（`expand_attachments`）就断了。
    """

    #: 给模型看的那段话（框架自己写的部分由调用方拼好）。
    text: str = ""
    #: 已经归一化 + 落盘的图。0 张 = 这就是一条普通的文本回执。
    prepared: list[PreparedImage] = field(default_factory=list)

    @property
    def blocks(self) -> list[dict]:
        """发到 wire 上的那串块（OpenAI 形状，见模块 docstring 判据 1）。"""
        return [p.block for p in self.prepared]

    def render(self) -> str:
        """文本部分 = 调用方给的话 + 每张图一行说明 + 它自己的 notes。

        判据 3（超了要说）：`PreparedImage.notes`（已降采样 / EXIF 已剥离 /
        动图只取首帧 / 已降质量）非空就必须进上下文 —— 空列表 = 一个
        字节都没动，那时多说一个字都是噪声。
        这里不写 `_POINTER_TEMPLATE` 那种指针：运行时那句话会被
        `content_text` 再产一遍，同一张图两个指针，下一轮装回两张图
        （`paths_in_text` 按出现次数还原）。落库那句指针由 `content_text` 一处产。
        """
        lines: list[str] = []
        if self.text:
            lines.append(self.text)
        for idx, p in enumerate(self.prepared, 1):
            lines.append("%d. %s（已存在 %s）" % (idx, p.label, p.path.as_posix()))
            for note in p.notes:
                lines.append("   - %s" % note)
        return "\n".join(lines)


# 三、规范化


def _normalize(data: bytes, media_type: str, source: str) -> PreparedImage:
    """校验 -> 转正 -> 剥元数据 -> 限尺寸 -> 编码，产出 `PreparedImage`。

    无条件重编码（判据 2）：不做"字节没变就原样透传"的短路。
    那条短路看着省事，实际把两个洞一起放进来 ——
    ① 元数据里能塞任意文本（`tEXt` / EXIF `ImageDescription` / ICC 描述），
    模型看得见；② 尺寸与体积的上限只对新图生效，老图绕过。
代价是每次多一次编解码，买的是"进了上下文的那张图，字节是我们造的"。
    """
    # Pillow 只在这几个函数里 import —— `content_text` 是全仓都要用的，
    # 而它不该把 Pillow 拖进每一个 import 了 `core.image_input` 的模块。
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise ImageInputError(
            "本机没有 Pillow，图片输入不可用（`pip install pillow`）：%s" % exc
        ) from exc

    import io

    notes: list[str] = []

    try:
        probe = Image.open(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 —— Pillow 的异常类型很杂
        raise ImageInputError(
            "这不是一张能解开的图片（%s）：%s" % (type(exc).__name__, exc)
        ) from exc

    # 像素总量在 `load()` 之前判：头里的尺寸已经足够，而 `load()` 那一刻
    # 才真正分配像素内存（压缩炸弹就是在这里展开的）。
    try:
        probe_w, probe_h = probe.size
    except Exception:  # noqa: BLE001 —— 读不到尺寸就交给下面 load 去判
        probe_w = probe_h = 0
    if probe_w > 0 and probe_h > 0 and probe_w * probe_h > MAX_PIXELS:
        raise ImageInputError(
            "这张图解出来是 %d×%d（约 %d 万像素），超过上限 %d 万像素 —— "
            "请先裁小一块再发。"
            % (probe_w, probe_h, probe_w * probe_h // 10000,
               MAX_PIXELS // 10000))

    try:
        probe.load()
    except Exception as exc:  # noqa: BLE001
        raise ImageInputError(
            "这不是一张能解开的图片（%s）：%s" % (type(exc).__name__, exc)
        ) from exc

    # 动图：只取第一帧
    #
    # 各家的 image block 都只承载一帧（OpenAI 的 `image_url` 没有帧概念）。
    # 不取首帧的话，Pillow 重存会把整段动画丢掉而不留痕迹 ——
    # 那就是"静默改坏"。取首帧 + 说出来。
    if getattr(probe, "is_animated", False):
        notes.append("这是动图，只取了第一帧")
        probe.seek(0)

    # EXIF 方向烘进像素
    #
    # 手机拍的图靠 EXIF 的 `Orientation` 转正，而下面要丢掉整个 EXIF
    #不烘的话所有竖拍图会横过来（而且没人知道为什么）。
    try:
        img = ImageOps.exif_transpose(probe)
    except Exception:  # noqa: BLE001 —— 坏 EXIF 不该让整张图作废
        img = probe

    # 转正：带透明度的走 PNG，其余按原格式
    #
    # 判据是"上游认不认这个容器"，不是"哪个更小"：
    # 截图（大色块）PNG 更小、照片 JPEG 更小，按内容猜格式是在替用户做取舍。
    src_media = media_type
    has_alpha = (img.mode in ("RGBA", "LA")
                 or (img.mode == "P" and "transparency" in img.info))
    if media_type not in SUPPORTED_MEDIA:
        media_type = "image/png" if has_alpha else "image/jpeg"
        notes.append("原始格式 %s 上游不认，已转成 %s"
                     % (src_media, media_type.split("/")[-1].upper()))
    elif has_alpha and media_type in ("image/jpeg",):
        media_type = "image/png"
        notes.append("这张图带透明通道，JPEG 存不下 —— 已转成 PNG")
    elif media_type == "image/gif" and not getattr(probe, "is_animated", False):
        # 单帧 GIF：留 GIF 没好处（上游多半转码），统一成 PNG
        media_type = "image/png"
        notes.append("单帧 GIF 已转成 PNG")

    if media_type == "image/jpeg" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif media_type == "image/png" and img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        img = img.convert("RGBA")

    # 限尺寸（判据 3：超了要说出来）
    w, h = img.size
    longest = max(w, h)
    if longest > MAX_EDGE_PX:
        scale = MAX_EDGE_PX / float(longest)
        new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
        img = img.resize(new_size, _resample())
        notes.append("已降采样 %d×%d -> %d×%d（超过长边上限 %d，那个尺寸对模型"
                     "没有额外信息，只是更多 token）"
                     % (w, h, new_size[0], new_size[1], MAX_EDGE_PX))
        w, h = new_size

    # 编码（不带 exif / icc / pnginfo —— 元数据在这里掉光）
    #
    # 这就是判据 2 的牙齿：`save()` 不传这三个参数，就是一次 exif 清洗。
    # 有断言按 AST 反查（`tests/test_image_input.py`），防止有人"顺手"把
    # `exif=img.info.get('exif')` 加回来。
    payload, img, w, h, extra = _encode(img, media_type, w, h)
    notes.extend(extra)

    prepared = PreparedImage(
        block={"type": "image_url",
               "image_url": {"url": _data_url(payload, media_type)}},
        path=Path(),          # 由 `_spill` 填
        media_type=media_type,
        width=w,
        height=h,
        byte_size=len(payload),
        source=source,
        notes=notes,
    )
    return _spill(prepared, payload)


def _encode(img, media_type: str, w: int, h: int):
    """编码到字节。超 `MAX_IMAGE_BYTES`，分级降，每次都出声；降完还超就拒。

    判据：只在超限时才动它。给一个"目标大小"就不停压是错的 ——
    那会把一张正常的照片压成渣，而用户不知道为什么（不许静默改坏）。
    阶梯分两段：先降质量、再降尺寸。顺序有理由 ——
    降尺寸会丢内容（截图里的字先糊），降质量只丢精度；先做便宜的那个。
    """
    import io

    notes: list[str] = []
    fmt = {"image/png": "PNG", "image/jpeg": "JPEG",
           "image/webp": "WEBP", "image/gif": "GIF"}[media_type]

    def _save(im, quality=None) -> bytes:
        buf = io.BytesIO()
        kw: dict[str, Any] = {}
        if fmt == "JPEG":
            kw = {"quality": quality or _JPEG_QUALITY, "optimize": True}
        elif fmt == "PNG":
            kw = {"optimize": True}
        elif fmt == "WEBP":
            kw = {"quality": quality or _JPEG_QUALITY}
        im.save(buf, format=fmt, **kw)
        return buf.getvalue()

    payload = _save(img)

    if len(payload) > MAX_IMAGE_BYTES and fmt in ("JPEG", "WEBP"):
        for quality in (85, 70, 55):
            payload = _save(img, quality)
            if len(payload) <= MAX_IMAGE_BYTES:
                notes.append("已降质量到 %d（原图超过单图上限 %d 字节）"
                             % (quality, MAX_IMAGE_BYTES))
                return payload, img, w, h, notes

    for _ in range(3):
        if len(payload) <= MAX_IMAGE_BYTES:
            return payload, img, w, h, notes
        w, h = max(1, w // 2), max(1, h // 2)
        img = img.resize((w, h), _resample())
        payload = _save(img)
        notes.append("已再降一档到 %d×%d（仍然超过单图上限 %d 字节）"
                     % (w, h, MAX_IMAGE_BYTES))

    if len(payload) > MAX_IMAGE_BYTES:
        # 降完还超，拒，不静默发一张上游会 400 的图。
        raise ImageInputError(
            "这张图压到 %d 字节仍然超过单图上限 %d —— 请先裁小一块再发。"
            % (len(payload), MAX_IMAGE_BYTES))
    return payload, img, w, h, notes


def _data_url(payload: bytes, media_type: str) -> str:
    return "data:%s;base64,%s" % (
        media_type, base64.b64encode(payload).decode("ascii"))


def _spill(prepared: PreparedImage, raw: bytes) -> PreparedImage:
    """把归一化后的字节写进 `data/attachments/`（内容寻址），填上 `path`。

    内容寻址（sha256 前 16 位）：同一张图粘两次只有一个文件，
    而"这张图是谁"由字节决定 —— 这正是 `_path_of` 能把 data URL
    还原成路径的原因（两处必须同源）。
    """
    digest = hashlib.sha256(raw).hexdigest()[:16]
    d = ATTACHMENT_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / (digest + _EXT[prepared.media_type])
    if not p.exists():          # 内容寻址，同一张图粘两次只写一次
        p.write_bytes(raw)
    prepared.path = p
    return prepared


_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
        "image/webp": ".webp", "image/gif": ".gif"}


# 四、三个入口


def _read_bounded(p: Path, limit: int) -> bytes:
    """最多读 `limit + 1` 字节。

    判据：`stat()` 只是读之前的一次估计（文件仍可能在这期间长大），
    真正管住内存的是这个上限。多读那一个字节是用来判"超了没有"的 ——
    恰好 `limit` 字节的文件不该被判成超限。
    """
    with p.open("rb") as fh:
        return fh.read(limit + 1)


def _b64_decoded_bound(b64: str) -> int:
    """一段 base64 文本解码后的字节数上限（不解码就能算）。

    去掉尾部 `=` 再按 3/4 折算；文本里若夹了空白，这个数只会偏大 ——
    偏大只会让边界附近的那一个被拒，不会让超限的溜过去。
    """
    return (len(b64.rstrip("=")) * 3) // 4


def _guard_b64_size(b64: str, what: str) -> None:
    """解码之前判体积 —— `base64.b64decode` 那一刻就把整段铺进内存了。"""
    if _b64_decoded_bound(b64) > MAX_SOURCE_BYTES:
        raise ImageInputError(
            "%s解码后超过能处理的 %s 字节 —— 请先裁小一块再发。"
            % (what, MAX_SOURCE_BYTES))


def prepare_bytes(data: bytes, *, media_type: str = "",
                  source: str = "") -> PreparedImage:
    """从内存里的字节做一张图。`media_type` 只是提示 —— 真正判的是魔数。

    魔数优先：`media_type` 传了也只在魔数认不出时当兜底，
    因为"用户说它是 PNG"与"它真的是 PNG"是两件事。
    """
    if not data:
        raise ImageInputError("这张图是空的（0 字节）")
    if len(data) > MAX_SOURCE_BYTES:
        raise ImageInputError(
            "原图 %s 字节，超过能处理的 %s 字节 —— 请先裁小一块再发。"
            % (len(data), MAX_SOURCE_BYTES))
    sniffed = sniff_media_type(data)
    if sniffed is None:
        claimed = ("（说是 %s，但文件头对不上）" % media_type) if media_type else ""
        raise ImageInputError(
            "认不出这是什么图片格式%s —— 支持 PNG / JPEG / WebP / GIF。" % claimed)
    return _normalize(data, sniffed, source)


def prepare_path(path: str | Path, *, source: str = "") -> PreparedImage:
    """从本地文件路径做一张图（路径图）。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ImageInputError("找不到这个文件：%s" % p)
    if not p.is_file():
        raise ImageInputError("%s 不是一个文件" % p)
    # 先 stat 再有界读 —— 上限必须在字节进内存之前生效。
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise ImageInputError(
            brief="这个文件读不了",
            detail="路径在，但 stat 失败：%s（%s）" % (p, exc)) from exc
    if size > MAX_SOURCE_BYTES:
        raise ImageInputError(
            "原图 %d 字节，超过能处理的 %d 字节 —— 请先裁小一块再发。"
            % (size, MAX_SOURCE_BYTES))
    try:
        data = _read_bounded(p, MAX_SOURCE_BYTES)
    except OSError as exc:
        raise ImageInputError(
            brief="这个文件读不了",
            detail="路径在，但读取失败：%s（%s）" % (p, exc)) from exc
    return prepare_bytes(data, source=source or ("本地文件 %s" % p.name))


def prepare_data_url(url: str, *, source: str = "") -> PreparedImage:
    """从 `data:` URL 做一张图（浏览器粘贴上来的就是这个形状）。"""
    if not isinstance(url, str) or not url.startswith("data:"):
        raise ImageInputError("不是 data: URL（收到 %s）"
                              % (type(url).__name__ if not isinstance(url, str)
                                 else url[:40]))
    head, _, b64 = url.partition(",")
    if not b64:
        raise ImageInputError("data: URL 里没有 payload")
    claimed = head[5:].split(";")[0].strip().lower()
    if claimed and not claimed.startswith("image/"):
        raise ImageInputError("这不是图片（说是 %s）" % claimed)
    _guard_b64_size(b64, "这张图")
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ImageInputError("data: URL 的 base64 解不开：%s" % exc) from exc
    return prepare_bytes(data, media_type=claimed, source=source or "粘贴")


def _decode_data_url(url: str) -> bytes:
    """把刚做出来的 data URL 解回字节（落盘要原字节，不是 base64）。"""
    _, _, b64 = url.partition(",")
    return base64.b64decode(b64)


def prepare_chain(components: Iterable[Any]) -> tuple[list[PreparedImage],
                                                      list[str]]:
    """把一串 `channels.message.Image` 组件变成可发的图。

    每一张失败都要留下一句话（判据 3：不静默）。
    返回 `(成功的图, 给用户/模型看的说明)` —— 两个都非空是常态：
    三张里坏一张，那就发两张 + 说一句"第三张没收下，因为它……"。

    四种来源各自能不能用：

    | 组件形态 | 谁造的 | 能不能取到字节 |
    |---|---|---|
    | `local_path` | Telegram（已下载）· 微信（下载 + AES 解密） · 飞书（通道侧下载） | ✅ |
    | `base64` | Web / 内联数据 | ✅ |
    | `url` = `feishu://image/<key>`（没有 `local_path`） | 飞书这次没取下来 | ❌ |
    | `url` = `http(s)://`（没有 `local_path`） | 只给地址、不下发的通道 | ❌ |

    取不到的那两种给的是实话（"这条通道这次没把图取下来"），
    不是"图片处理失败"——两种失败的原因不同，用户能做的事也不同。
    """
    ok: list[PreparedImage] = []
    notes: list[str] = []
    raw = list(components)
    if not raw:
        return ok, notes
    total_bytes = 0

    for idx, comp in enumerate(raw, 1):
        if len(ok) >= MAX_IMAGES_PER_TURN:
            rest = len(raw) - idx + 1
            notes.append(
                "还有 %d 张没收下 —— 一轮最多 %d 张。它们没有被处理，"
                "要看得分开几轮发。" % (rest, MAX_IMAGES_PER_TURN))
            break
        # 合计字节（`MAX_TURN_TOTAL_BYTES`，每请求的体量预算）。
        # 判据：到了就不再往里加（不砍单张 —— 那会把一张好图弄坏）。
        # 所以最后收下的那张可能把它顶过去一点 —— 这是有意的：
        #   "保证不超过"与"不要再加了"是两条不同的话，而后者才是我们真需要的
        #   （要前者就得在收下之后反悔，那是"收下了又说没收下"）。
        if total_bytes >= MAX_TURN_TOTAL_BYTES:
            rest = len(raw) - idx + 1
            notes.append(
                "后面 %d 张没收下 —— 这一轮收下的图**合计已经到了 %s**（上限 %s）。"
                "它们没有被处理，分开几轮发。"
                % (rest, _human_bytes(total_bytes),
                   _human_bytes(MAX_TURN_TOTAL_BYTES)))
            print("[image] 合计字节到顶：%s >= %s，余下 %d 张没收下"
                  % (_human_bytes(total_bytes),
                     _human_bytes(MAX_TURN_TOTAL_BYTES), rest))
            break
        try:
            _img = _prepare_one(comp, idx)
            ok.append(_img)
            total_bytes += _img.byte_size
        except ImageInputError as exc:
            # 两个出口，两个读者：
            #   给模型的 = 短句（进这一轮正文）；给控制台的 = 完整判据（只 print）。
            print("[image] 第 %d 张没收下 —— %s" % (idx, exc.detail))
            notes.append("%s（第 %d 张）" % (_NO_ACTION_BRIEF % exc.brief, idx))
        except Exception as exc:  # noqa: BLE001 —— 一张坏图不该毁掉整轮
            print("[image] 第 %d 张没收下（%s）：%s"
                  % (idx, type(exc).__name__, exc))
            notes.append("%s（第 %d 张，%s）"
                         % (_NO_ACTION_BRIEF % "这张图没能读出来",
                            idx, type(exc).__name__))
    return ok, notes


#: 进模型上下文的那句话的后半截。
#:
#: 判据：决定一条文本去哪儿的，是它的读者是谁，不是它有多重要。
#: 框架自己的诊断走 stdout；进对话的那一句只回答"这对模型意味着什么"，
#: 并且明确说"不用你处理" —— 否则"有线索"就等于"有任务"：
#: 模型会把排障指南当成待修问题，跑去找工具修，白烧轮次。
_NO_ACTION_BRIEF = ("⚠️ %s。**这是图片来源那边的问题，不需要你去查或修** —— "
                    "你收不到这张图，直接告诉用户即可")


# 四之二、一轮的图片（入口层唯一的调用点）


def _is_image_comp(comp: Any) -> bool:
    """它是不是一个图片组件 —— 鸭子类型（不 import `channels.message`）。

    `channels.message.Image.type` 是 `ComponentType.IMAGE` 这个枚举，
    所以取 `.value` 比字符串；取不到枚举时按字符串比（替身 / 老形状）。
    """
    t = getattr(comp, "type", None)
    if t is None:
        return False
    return getattr(t, "value", t) == "image"


def _comp_kind(comp: Any) -> str:
    """组件类型字符串 —— 鸭子类型（不 import `channels.message`）。"""
    t = getattr(comp, "type", None)
    if t is None:
        return ""
    return str(getattr(t, "value", t))


def _mime_of(comp: Any) -> str:
    return str(getattr(comp, "mime_type", "") or "").lower()


def _is_video_comp(comp: Any) -> bool:
    """视频 = `File` 组件 + `video/*`。音频不走这里（它是 `Voice`）。"""
    return _comp_kind(comp) == "file" and _mime_of(comp).startswith("video/")


def _plain_text_of(components) -> str:
    """这一轮里纯文本部分拼成的正文（图片/语音/文件/视频由路由层自己写）。"""
    parts: list[str] = []
    for c in components or ():
        if _comp_kind(c) in ("image", "voice", "file"):
            continue
        fn = getattr(c, "to_plain_text", None)
        if callable(fn):
            parts.append(fn())
    return "".join(parts)


@dataclass
class TurnImages:
    """这一轮模态路由的全部结论（入口层只要看这几个字段）。

    名字沿用 `TurnImages`，但它管的是图片 + 音频 + 视频 + 文件四类组件 ——
    改名的爆炸半径（`pre_process` / 测试 / 导出清单）不值得，判据看字段。
    """

    #: 去掉媒体占位之后的正文（已含下面那串说明）。
    #: `None` = 没拿到消息链，调用方保留原来那份 `user_message` 不动。
    text: Optional[str] = None
    #: 要发给 provider 的媒体块（`image_url` / `input_audio`；0 个 = 没有）。
    blocks: list[dict] = field(default_factory=list)
    #: 每一步动了什么（降采样 / 转码 / 没发出去 / 抽帧）—— 非空就要进上下文。
    notes: list[str] = field(default_factory=list)
    #: 被能力闸挡下来的图片张数（模型不收图）—— 单列一格，因为它的
    #: 补救办法与"这张图坏了"完全不同（换模型 vs 换图）。
    #: 音频/视频的"发不出去"不记在这里 —— 它们各自的说明写在 `notes` 里
    #: （补救办法是"转文字 / 抽帧"，不是"换模型"）。
    dropped: int = 0


#: 音频格式：mime / 扩展名 → `input_audio.format` 的取值。
_AUDIO_FMT_BY_MIME = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/ogg": "ogg", "audio/webm": "webm", "audio/mp4": "m4a",
    "audio/x-m4a": "m4a", "audio/m4a": "m4a",
}


def _audio_format(comp: Any) -> str:
    mime = _mime_of(comp).split(";")[0].strip()
    if mime in _AUDIO_FMT_BY_MIME:
        return _AUDIO_FMT_BY_MIME[mime]
    local = getattr(comp, "local_path", None)
    if local:
        ext = Path(str(local)).suffix.lower().lstrip(".")
        return {"mpeg": "mp3", "x-wav": "wav", "wave": "wav",
                "mp4": "m4a"}.get(ext, ext)
    return ""


def _audio_seconds(comp: Any) -> Optional[float]:
    try:
        v = float(getattr(comp, "duration_seconds", 0) or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _read_audio_bytes(comp: Any) -> tuple[bytes, str]:
    """`Voice` / 音频 `File` 的字节。取不到，带原因的 `ImageInputError`。

    不在这里发网络请求：通道负责把字节弄到手（Telegram/WeChat 已下载），
    只给句柄的通道（飞书）如实说"没拿到本地文件"。
    """
    local = getattr(comp, "local_path", None)
    if local:
        p = Path(str(local))
        try:
            if p.is_file():
                # 先 stat 再有界读 —— 20 MiB 的上限必须在字节进内存之前生效。
                size = p.stat().st_size
                if size > MAX_AUDIO_SOURCE_BYTES:
                    raise ImageInputError(
                        brief="语音文件太大",
                        detail="%d 字节，超过上限 %d 字节"
                               % (size, MAX_AUDIO_SOURCE_BYTES))
                return _read_bounded(p, MAX_AUDIO_SOURCE_BYTES), str(p)
        except OSError as exc:
            raise ImageInputError(
                brief="语音文件读不出来",
                detail="路径在，但读取失败：%s（%s）" % (local, exc)) from exc
        raise ImageInputError(
            brief="语音文件不见了",
            detail="通道给的路径不存在：%s" % local)
    url = str(getattr(comp, "url", "") or "")
    if url.startswith("data:"):
        try:
            b64 = url.split(",", 1)[1]
            _guard_b64_size(b64, "这段语音")
            return base64.b64decode(b64, validate=False), ""
        except Exception as exc:                                # noqa: BLE001
            raise ImageInputError(
                brief="语音的内联数据解不开",
                detail="%s: %s" % (type(exc).__name__, exc)) from exc
    raise ImageInputError(
        brief="这条通道没有把语音下载到本地",
        detail="它只给了一个句柄/URL（%s）—— 模型拿不到字节。"
               "要支持这条通道，得在通道侧先下载。" % (url[:120] or "空"))


def _audio_note(comp: Any, idx: int, why: str) -> str:
    secs = _audio_seconds(comp)
    head = "[语音 %d" % idx + ("，%0.1f 秒" % secs if secs else "") + "]"
    local = getattr(comp, "local_path", None)
    path = ("文件在 %s" % local) if local else "没有本地文件"
    return (head + " " + path + "。当前模型**不能**处理这段语音：" + why
            + " —— 它的内容没有被交给模型。要处理它，把语音转成文字再发一遍，"
            "或者换一个能收音频的模型（在设置页「端配置」那条配置的「高级选项 →"
            " 音频能力」里可以「强制开启」，或在 `config.yaml` 里声明"
            " `supports_audio: true`）。"
            "**不要**自己去改配置。")


def _prepare_audio(comp: Any, idx: int, sees_audio: bool) -> tuple[Optional[dict], str]:
    """一个音频组件 → `(input_audio 块, 说明)`。两个都可能为空。"""
    if not sees_audio:
        return None, _audio_note(comp, idx, "模型没有声明支持音频输入")
    try:
        data, path = _read_audio_bytes(comp)
    except ImageInputError as exc:
        return None, _audio_note(comp, idx, exc.brief or str(exc))
    fmt = _audio_format(comp)
    if not fmt:
        return None, _audio_note(comp, idx, "认不出音频格式")
    if fmt not in AUDIO_WIRE_FORMATS:
        return None, _audio_note(
            comp, idx, "格式 %s 不在直发白名单（%s）"
            % (fmt, "/".join(AUDIO_WIRE_FORMATS)))
    if len(data) > MAX_AUDIO_SOURCE_BYTES:
        return None, _audio_note(
            comp, idx, "文件太大（%.1f MiB，上限 %d MiB）"
            % (len(data) / 1048576.0, MAX_AUDIO_SOURCE_BYTES // 1048576))
    block = {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(data).decode("ascii"),
            "format": fmt,
        },
        # 路径统一成 `/`：指针文本是落库的，两种斜杠会让同一份历史
        # 在重新落库（`repersist_messages`）时变成另一个字符串（前缀稳定性同族）。
        # 私有字段：只给落库指针与重新装回用；wire 前由 provider 剥掉
        #   （`openai_compat._wire_messages`）。
        "_path": str(path or "").replace("\\", "/"),
        "_seconds": _audio_seconds(comp),
    }
    return block, ""


def _video_frames(path: str) -> tuple[list[bytes], dict]:
    """抽几帧 JPEG（`cv2`）。返回 `(帧, 元信息)`；抽不到就空列表。

    `meta["error"]` 非空 = 抽帧没能开始（太大 / 帧尺寸超限），
    由调用方换成一句人话 —— 与"解码器不可用"是两件事。
    """
    try:
        size = Path(str(path)).stat().st_size
    except OSError:
        return [], {"error": "读不到这个文件"}
    if size > MAX_VIDEO_SOURCE_BYTES:
        return [], {"error": "文件太大（%s，上限 %s）"
                            % (_human_bytes(size),
                               _human_bytes(MAX_VIDEO_SOURCE_BYTES))}
    try:
        import cv2
    except Exception:                                            # noqa: BLE001
        return [], {}
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        return [], {}
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        meta = {
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
            "seconds": (total / fps) if (total > 0 and fps > 0) else None,
        }
        # 单帧像素总量也在解码之前判 —— 与图片那条同一个理由。
        fw, fh = meta["width"], meta["height"]
        if fw > 0 and fh > 0 and fw * fh > MAX_PIXELS:
            meta["error"] = ("画面尺寸 %d×%d（约 %d 万像素）超过上限 %d 万像素"
                             % (fw, fh, fw * fh // 10000, MAX_PIXELS // 10000))
            return [], meta
        frames: list[bytes] = []
        fracs = (0.1, 0.5, 0.9) if total > 2 else (0.0,)
        for frac in fracs[:MAX_VIDEO_FRAMES]:
            idx = int(total * frac) if total > 0 else 0
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
            except Exception:                                    # noqa: BLE001
                ok, frame = False, None
            if not ok or frame is None:
                continue
            ok2, buf = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if ok2:
                frames.append(bytes(buf))
        return frames, meta
    finally:
        cap.release()


def _prepare_video(comp: Any, idx: int, sees_images: bool) -> tuple[str, list[dict]]:
    """一个视频组件 → `(说明, 帧的图片块)`。视频本身不发（各家没有统一 wire）。"""
    name = str(getattr(comp, "name", "") or "视频")
    local = getattr(comp, "local_path", None)
    if not local:
        return ("[视频 %d] %s —— 这条通道没有把视频下载到本地，"
                "模型无法读取它的画面或声音。" % (idx, name), [])
    frames, meta = _video_frames(str(local))
    bits = ["[视频 %d] %s" % (idx, name)]
    if meta.get("width"):
        bits.append("%dx%d" % (meta["width"], meta["height"]))
    if meta.get("seconds"):
        bits.append("%0.1f 秒" % meta["seconds"])
    bits.append("文件在 %s" % local)
    head = " · ".join(bits)
    if not frames:
        why = meta.get("error") or "本机抽帧失败（没有可用的视频解码器）"
        return (head + "。模型不能直接看视频，而" + why
                + " —— 它的内容没有被交给模型。", [])
    if not sees_images:
        return (head + "。模型不能直接看视频；抽了 %d 帧，但**图片输入也被关掉了**"
                " —— 所以画面也没有发出去。" % len(frames), [])
    blocks: list[dict] = []
    failed: list[str] = []
    for i, data in enumerate(frames, 1):
        try:
            prepared = prepare_bytes(
                data, media_type="image/jpeg",
                source="视频「%s」第 %d 帧" % (name, i))
        except ImageInputError as exc:
            failed.append("第 %d 帧没收下：%s" % (i, exc.brief or str(exc)))
            continue
        blocks.append(prepared.block)
    if blocks:
        tail = "。模型不能直接看视频 —— 已抽 %d 帧作为图片交给它。" % len(blocks)
    else:
        tail = "。模型不能直接看视频，抽出来的帧也都没能收下。"
    if failed:
        tail += "（%s）" % "；".join(failed)
    return head + tail, blocks


def _file_note(comp: Any) -> str:
    """非媒体文件的文本句柄（不假装原生，给路径让人去读）。"""
    name = str(getattr(comp, "name", "") or "文件")
    size = int(getattr(comp, "size_bytes", 0) or 0)
    local = getattr(comp, "local_path", None)
    size_txt = ("（%s）" % _human_bytes(size)) if size > 0 else ""
    if local:
        return ("[文件] %s%s · 本地路径 %s。要用工具（read_file / parse 系列）去读，"
                "**不要**再向用户要内容。" % (name, size_txt, local))
    return ("[文件] %s%s —— 这条通道没有把文件下载到本地（只给了句柄），"
            "模型读不到它的内容。要处理它，请让用户换一个能下载附件的入口。"
            % (name, size_txt))


def prepare_turn(components, *, sees_images: bool = True,
                 sees_audio: bool = False) -> TurnImages:
    """`MessageChain` 的组件 → 这一轮的 `(正文, 媒体块, 说明)`。

    模态路由，三条判据：

      1. 模型声明支持该模态，给原生部件（image_url / input_audio）；
      2. 声明不支持，给一句模型看得懂的说明（时长 / 路径 / 下一步），
         绝不静默丢；
      3. 视频没有统一的原生 wire，抽帧走图片链，抽不到就说抽不到。

    判据 1：能力闸在"准备"这一步，不在 provider 里。
    交给 provider 去"发现收不下"的代价是上游 400 打断整轮，
    而这里挡下来的代价是"没发 + 一句话"（`providers/base.py::accepts_images`
    / `accepts_audio`）。

    判据 2：没发出去必须留下痕迹。悄悄少一张图/一段语音，用户会以为
    模型"看不见重点"，而真相是它根本没到。
    """
    if components is None:
        return TurnImages()

    comps = list(components)
    text = _plain_text_of(comps)
    images: list = []
    audios: list = []
    videos: list = []
    files: list = []
    for c in comps:
        kind = _comp_kind(c)
        mime = _mime_of(c)
        if kind == "image" or (kind == "file" and mime.startswith("image/")):
            images.append(c)
        elif kind == "voice" or (kind == "file" and mime.startswith("audio/")):
            audios.append(c)
        elif kind == "file" and mime.startswith("video/"):
            videos.append(c)
        elif kind == "file":
            files.append(c)

    notes: list[str] = []
    blocks: list[dict] = []
    dropped = 0

    for c in files:
        notes.append(_file_note(c))
    for i, c in enumerate(videos, 1):
        line, frame_blocks = _prepare_video(c, i, sees_images)
        notes.append(line)
        blocks.extend(frame_blocks)
    for i, c in enumerate(audios, 1):
        block, note = _prepare_audio(c, i, sees_audio)
        if block:
            blocks.append(block)
        if note:
            notes.append(note)

    if images:
        if not sees_images:
            n = len(images)
            # 这句话两个读者（用户 + 模型），所以要把"谁该动手"写死：
            #   模型有 `edit_file` / `run_command`，看见"改那一行"是会去改的
            #   （一份排障指南会让它跑去找工具修，白烧轮次）。
            note = ("⚠️ 这一轮附了 %d 张图片，但**图片输入被关掉了**"
                    "（设置里「端配置」那条配置的「高级选项 → 图片能力」设成了"
                    "「强制关闭」，或者 `config.yaml` 里 `supports_vision` 写了"
                    " false / 不是 `true`/`false`）—— 所以它们没有被发出去，"
                    "上面这段文字是模型收到的全部内容。"
                    "要打开：在设置页的「端配置」里改这条配置的「高级选项 →"
                    " 图片（视觉）能力」为「强制开启」；或者把 `config.yaml` 的"
                    " `model` 段那一行改成 `true` 或删掉它（默认就是开）。"
                    "**这一步由用户决定、用户来做 —— 你不要去改配置。**" % n)
            print("[image] 能力闸拦下 %d 张（supports_vision 被显式关掉了）" % n)
            notes.append(note)
            dropped = n
        else:
            ok, inotes = prepare_chain(images)
            # 图片块在前：只有图片的那一轮，字节必须是稳定形状
            #   （缓存前缀只增不改）。
            blocks = [b.block for b in ok] + blocks
            notes.extend(inotes)

    text = _join(text, "\n".join(n for n in notes if n))
    return TurnImages(text=text, blocks=blocks, notes=notes, dropped=dropped)


#: 框架附注的分界线。
#:
#: 这些说明是给模型看的（"图被拦下了"/"这个文件只有路径"……），
#: 但它们拼在 `ctx.user_message` 尾巴上，落库之后长得跟用户说的话一模一样：
#: 聊天区把它渲染进用户气泡、会话标题也会被它污染。
#:
#:拼的时候打一个人能读、模型能懂的分界：模型照旧看得到全部内容，
#: 而显示侧（`/api/messages` · `/api/threads` 的标题）从这一行起截掉。
#: 判据是「框架的话不许冒充用户的话」—— 不是"模型别看见"。
NOTE_HEADER = "—— 以下是框架附注（不是用户说的话）——"


def _join(text: str, extra: str) -> str:
    """正文与说明拼接 —— 空正文时不留前导换行（那会变成一条奇怪的消息）。"""
    if not extra:
        return text
    marked = NOTE_HEADER + "\n" + extra
    return (text + "\n" + marked) if text else marked


#: 没有 `NOTE_HEADER` 分界线的历史消息 —— 按已知附注的开头认。
#: 不放带 emoji 的那几个字符：`check_gbk_safe` 数的是"处"，多一处就把基线顶上去。
_LEGACY_NOTE_HEADS = ("这一轮附了", "[语音 ")


def strip_notes(text: str) -> str:
    """显示侧用的：把框架附注截掉，回用户真正说的那句（展示/标题/预览用）。

    除了新分界线，还要认没有分界线的历史写法 —— 否则那几条老线程
      点开仍然会显示框架附注。

    只许用在"给人看"的出口 —— 模型那条路（`messages` / `load_session`）
      必须保留全文，否则它下一轮就不知道自己少看了一张图。
    """
    if not text:
        return text
    cut = text.find(NOTE_HEADER)
    for head in _LEGACY_NOTE_HEADS:
        j = text.find(head)
        if j < 0:
            continue
        j = text.rfind("\n", 0, j) + 1          # 回到这一行的行首（没有换行就是 0）
        if cut < 0 or j < cut:
            cut = j
    if cut < 0:
        return text
    return text[:cut].rstrip("\n")


def _prepare_one(comp: Any, idx: int) -> PreparedImage:
    """一个组件 -> 一张图。取不到字节时说的是"为什么取不到"。"""
    local = getattr(comp, "local_path", None)
    if local:
        return prepare_path(local, source="你发的第 %d 张" % idx)

    b64 = getattr(comp, "base64", None)
    if b64:
        media = getattr(comp, "mime_type", "") or "image/png"
        _guard_b64_size(b64, "这张图")
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ImageInputError("base64 解不开：%s" % exc) from exc
        return prepare_bytes(data, media_type=media,
                             source="你发的第 %d 张" % idx)

    url = getattr(comp, "url", None) or ""
    if url.startswith("data:"):
        return prepare_data_url(url, source="你发的第 %d 张" % idx)

    if url.startswith("feishu://"):
        # 飞书那条已经接了（通道侧下载，见 `channels/feishu_channel/
        #   handler.py::_resolve_media`），走到这里只可能是这一次没取到
        #   （下载被拒 / 太大 / 消息里没有 message_id）。
        #   别写"还没做"—— 那会把真正的信号盖住，模型会跑去找工具修。
        raise ImageInputError(
            brief="飞书这条通道这次没能把图片取下来",
            detail="飞书给的是一个 `feishu://` 句柄，通道侧要去下载一次；"
                   "这一张没取到（控制台会说是哪一种：下载被拒 / 超出上限 / "
                   "消息里没有 message_id）。")
    if hasattr(comp, "_aes_key"):
        # 给模型的一句话（`brief`）+ 给控制台的一份（`detail`）。
        #
        # ① 不能写「那一段还没做」—— 下载+解密那段代码是在的，那会把真正的
        #    信号盖住（真相是这一次没拿到字节）。
        # ② 不能把排障指南放进 `brief`：它进了模型的上下文之后，模型会判定
        #    "这是个待修的问题"，跑去找工具修，白烧轮次。
        raise ImageInputError(
            brief="微信这条通道这次没能把图片取下来",
            detail=(
                "微信这条通道的图片要先**下载并解密**，而这一张在本机"
                "**没有拿到字节**（组件里 `local_path` 是空的）。\n"
                "⚠️ 这与「这条通道还没做」是两回事 —— 下载那段代码是在的"
                "（`channels/wechat_channel/handler.py::_download_files`），"
                "是这一次没成功。\n"
                "控制台会说出是哪一种："
                "① `[wechat] 图片消息里**没有下载地址**` ⇒ 上游没给地址；"
                "② `[wechat] Downloading: …` / `Download failed: …` ⇒ 取的时候出错。"
                "⚠️ 常见情形是 ②，根因通常是**文件名太长**"
                "（`Path(url).name` 把 800 字符的 query 当成了文件名 ⇒ "
                "Windows `MAX_PATH`）—— 已修。"))
    if url.startswith(("http://", "https://")):
        raise ImageInputError(
            brief="这张图只有一个远端地址，本框架不替你去下载",
            detail="这张图只有一个远端地址（%s…），本轮不替你去下载 —— "
                   "它会绕开出网审批。要看得先把它存到本地再发路径。"
                   % url[:60])
    raise ImageInputError(
        brief="这张图在本机没有拿到字节（也没带任何来源）",
        detail="这个图片组件 `url` / `base64` / `local_path` 全空 —— "
               "多半是**通道侧下载那一步没成功**，去该通道的日志找那一行。")


# 五、`content` 的纯文本视图 —— 全仓唯一一处


def content_text(content: Any) -> str:
    """把一条消息的 `content` 变成字符串。这是唯一的转换处。

    ## 为什么必须有它

    带图那一轮的 `content` 是一个块列表（OpenAI 形状），而下游有十几处
    按字符串用它：SQLite 的 TEXT 列（绑 list 直接 `InterfaceError`）、
    压缩闸门、自动审查器、ICE 信息层、历史检索、tool_result_prune 的
    `len(content)`、`estimate_request_tokens` 的 `count_tokens(content)`。

两种做法都不行：
    · 每处各写一个 `if isinstance(content, str)` —— 迟早漏一处，
      而漏掉的那处是崩还是静默算错取决于上下文；
    · 在持久化层把 list `json.dumps` 进去 —— 那会把整张图的 base64
      写进 SQLite，且装载回来是一块模型的输入（它看不懂，还会被算进 token）。

    ## 口径

    `str`，原样返回（一个字节都不动 —— 这是缓存前缀那条纪律：不带图的那一轮
    必须逐字节等于今天）。

    块列表，文本照取，图片换成一句带路径的指针
    （`[图片 1 —— 原图在 data/attachments/ab12….png]`）。
    指针里必须有路径，因为它是跨轮取回的唯一载体 ——
    见 `expand_attachments`：下一轮靠在这句话里找到那条路径，把图装回去。
    只写 `[图片 1]` 的话，第二句「那按它改一下」就没有图可看了，
    而用户完全不知道为什么（那是"静默丢"的另一种形态）。
    """
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        n_img = 0
        n_audio = 0
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                parts.append(str(part.get("text") or ""))
            elif kind == "image_url":
                n_img += 1
                p = _path_of(part)
                if p:
                    parts.append(_POINTER_TEMPLATE.format(n=n_img, path=p))
                else:
                    # 反推不出路径，不编一个。这时那句话只保证"这里本来有张图"，
                    # 跨轮取回做不到 —— 而这是说得出来的（少一个路径而已）。
                    parts.append("[图片 %d]" % n_img)
            elif kind == "input_audio":
                # 音频块同样不进库（TEXT 列装不下 base64）——
                #   落一句带路径的指针，下一轮 `expand_attachments` 装回去。
                n_audio += 1
                p = str(part.get("_path") or "")
                if p:
                    parts.append(_AUDIO_POINTER_TEMPLATE.format(n=n_audio, path=p))
                else:
                    parts.append("[音频 %d]" % n_audio)
        return "\n".join(p for p in parts if p)
    # 认不出来的形状：不抛（一条坏消息不该毁掉整轮），但要说出来
    print("[image] content 是认不出的类型 %s —— 按空文本处理"
          % type(content).__name__)
    return ""


#: 指针模板 —— `content_text` 与下面的反解式必须同源。
#: 出处是同一族：`core/agent_loop.py:1270` 的 `_POINTER_RE`
#: （`<persistent_output file="…">` 也是"从我们自己写的文本里读回路径"）。
#: 别把这里放宽成"任何 `data/attachments/…` 子串" —— 卸载那句话里
#: 也带着路径（`_OFFLOAD_TEMPLATE`），放宽了就会把刚卸载的图又装回来，
#: 于是卸载永远不生效（而且看起来一切正常）。
_POINTER_TEMPLATE = "[图片 {n} —— 原图在 {path}]"
_POINTER_RE = re.compile(r"\[图片\s*\d+\s*——\s*原图在\s*([^\]]+?)\s*\]")
#: 音频指针 —— 形状与图片那条同族（从我们自己写的
#: 文本里读回路径），只是动作不同：图片卸老、音频装回。
_AUDIO_POINTER_TEMPLATE = "[音频 {n} —— 原音频在 {path}]"
_AUDIO_POINTER_RE = re.compile(r"\[音频\s*\d+\s*——\s*原音频在\s*([^\]]+?)\s*\]")
#: 只认内容寻址那一种文件名（16 位十六进制 + 我们产出的四种扩展名）
#: —— 那正是 `_spill` 写出来的形状（两处同源）。
_ATTACHMENT_NAME_RE = re.compile(
    r"^data/attachments/[0-9a-f]{16}\.(?:png|jpg|webp|gif)$")


def split_text_and_media(content: Any) -> tuple[str, list[dict]]:
    """`str` / 块列表 -> `(文本, 媒体块)`。给那些只改文本、不动图的消费者用。

    文本永远是第 0 块 —— 装配的三处（`agent_loop._format_tool_result` ·
    `agent_loop.user_content` · `expand_attachments`）同形，所以这里按
    「第一块是不是 text」取，不按 type 逐个分类：分类会漏掉将来新增的
    块类型，而漏掉的那一块会静默消失（读不到 ≠ 0 的同族）。

    认不出的形状，`(content_text(content), [])` —— 退化成"只有文本"，
    呼叫方至少不会炸；而那一支不是这里定义的，别指望它保真。
    """
    if isinstance(content, str):
        return content, []
    if isinstance(content, (list, tuple)):
        parts = [p for p in content if isinstance(p, dict)]
        if parts and parts[0].get("type") == "text":
            return str(parts[0].get("text") or ""), parts[1:]
        return "", parts
    return content_text(content), []


def append_text_block(messages: Sequence[dict], block: str) -> list:
    """把一段框架文本挂到最后一条 user 消息的尾巴上，返回新的 list。

    ## 为什么它值得单独一个函数

    两个模块（技能索引 `core/skill_index.py` · 记忆索引 `core/memory_store.py`）
    干的是同一件事，各自都写着一段"找到最后一条 user 消息 -> 内容 +
    block"。而 `if not isinstance(content, str): return messages` 会漏掉
    带图那一轮的最后一条 user 消息（它是块列表），那两份索引在**每一个
    图片轮上静默不挂**（模型看不到自己有哪些工具、也看不到记忆索引），
    而没有任何地方会红。
修在一处，两处调用；再写第二份就是把这个洞再挖一遍。

    追加的形态：`str` 走拼接、块列表追加一个文本块 —— 两种都是
    只往尾部加，所以「前缀只许增长」照旧成立。
    找不到 user 消息 / 形状认不出，原样返回（一个字都不动）。
    """
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            copy = list(messages)
            copy[i] = {**m, "content": content + block}
            return copy
        # 只有"块列表"才追加块 —— 判据与 `content_text` /
        # `count_image_blocks` / `offload_old_images` 同一条：
        # `list` + 每一项都是带 `type` 的 dict。
        # 别的 list（旧的富文本链那种）原样返回：往里塞一个
        # `{"type": "text", …}` 会把一个我们没定义过的东西变成"看起来像块列表"。
        if (isinstance(content, (list, tuple)) and content
                and all(isinstance(p, dict) and p.get("type") for p in content)):
            copy = list(messages)
            copy[i] = {**m, "content": [*content,
                                        {"type": "text", "text": block}]}
            return copy
        return messages
    return messages


def paths_in_text(text: str) -> list[str]:
    """一句话里那些图片指针指向的路径，按出现顺序。

    顺序承重：图片的序号是模型唯一的指代方式（「第 2 张」），
    所以这里不许重排、不许去重（同 `rebuild_blocks` 那条；同一张图
    发两次就是两次，模型看到两个块）。
    """
    if not isinstance(text, str) or not text:
        return []
    out: list[str] = []
    for m in _POINTER_RE.finditer(text):
        p = m.group(1).replace("\\", "/").strip()
        if _ATTACHMENT_NAME_RE.match(p):
            out.append(p)
    return out


def audio_paths_in_text(text: str) -> list[str]:
    """一句话里那些音频指针指向的路径，按出现顺序。

    与图片那条不同：音频路径不是内容寻址的（通道把它下到自己的临时目录），
    所以这里不套 `_ATTACHMENT_NAME_RE`；能不能装回由 `rebuild_audio_blocks` 现判。
    """
    if not isinstance(text, str) or not text:
        return []
    return [m.group(1).replace("\\", "/").strip()
            for m in _AUDIO_POINTER_RE.finditer(text)
            if m.group(1).strip()]


def rebuild_audio_blocks(paths: Sequence[str]) -> tuple[list[dict], list[str]]:
    """把音频指针的路径还原成 `input_audio` 块。确定性（同 `rebuild_blocks`）。"""
    blocks: list[dict] = []
    notes: list[str] = []
    for idx, raw in enumerate(paths, 1):
        p = Path(raw)
        fmt = p.suffix.lower().lstrip(".")
        fmt = {"mpeg": "mp3", "x-wav": "wav", "wave": "wav", "mp4": "m4a"}.get(fmt, fmt)
        if fmt not in AUDIO_WIRE_FORMATS:
            notes.append("第 %d 段音频取不回来了（格式 %s 不直发）" % (idx, fmt or "?"))
            continue
        try:
            data = p.read_bytes()
        except OSError:
            notes.append("第 %d 段音频取不回来了（%s 不在了）" % (idx, p.as_posix()))
            continue
        if len(data) > MAX_AUDIO_SOURCE_BYTES:
            notes.append("第 %d 段音频取不回来了（文件太大）" % idx)
            continue
        blocks.append({
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(data).decode("ascii"),
                "format": fmt,
            },
            "_path": raw,
        })
    return blocks, notes


def expand_attachments(messages: Sequence[dict],
                       keep: int = KEEP_IMAGES_IN_REQUEST) -> int:
    """把历史里那些指针装回成图片块，然后卸载掉太老的。返回装回去几张。

    ## 它为什么存在

    消息行的 `content` 是 TEXT 列（`core/memory/short_term.py:104`，
    绑一个 list 直接 `InterfaceError`），图片不进库，库里留的是一句
    带路径的指针（`content_text` 写的）。

    而用户的行为是：「看看这张图」→ 下一轮「那按它改一下」。
    第二句还需要那张图。发请求之前，把指针装回去。

    ## 两条判据

    * 确定性 —— 装回去的字节只由指针文本与盘上文件决定，
      与第几轮无关。这就是为什么它对缓存前缀无害：
      同一份历史每一轮装出同样的字节，前缀只增不改。
      反过来（今天装、明天不装）＝ 每轮重写前缀 ＝ 缓存全废（45x 那种尖峰）。
    * 装不回来就说出来 —— 文件被留档回收了 / 换过机器，那句
      指针留着不动（它本来就说"原图在 <路径>"），由模型自己发现取不到。
      不许把它清成 `[图片 1]` 那种没有下文的占位。

    只处理 `content` 还是 `str` 的消息：已经是块列表的说明这一轮
    已经装过了（就地改、不改结构，`offload_old_images` 同一条）。
幂等：同一份 `messages` 连调两次，第二次返回 0 且一个字节不改。
    """
    n = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        paths = paths_in_text(content)
        audio_paths = audio_paths_in_text(content)
        if not paths and not audio_paths:
            continue
        blocks, _notes = rebuild_blocks(paths)
        audio_blocks, _anotes = rebuild_audio_blocks(audio_paths)
        if not blocks and not audio_blocks:
            # 一个都没装回来（文件全没了），指针留着，别动它
            continue
        # 文本块在前、紧接着是图片块、音频块在最后 —— 与 `agent_loop` 拼当轮
        #   那条同形（图片在前），而且顺序对缓存前缀没有影响
        #   （同一份历史每轮一样）。
        msg["content"] = [{"type": "text", "text": content},
                          *blocks, *audio_blocks]
        n += 1
    # 卸载每轮都要跑，不是"装回去了才跑" —— 装回去为 0 的轮次照样可能有
    #   （a）已经装过的老图（`content` 是列表，上面跳过了）与
    #   （b）这一轮刚append 上去的新图，两者合起来一样会超 `keep`。
    #   挂在上面的 `if n:` 里，第 3 张图进来那一轮卸载不发生，
    #   而症状是"账单比预期高"（没有任何地方会红）。
    offload_old_images(messages, keep=keep)
    return n


# 六、卸载老图片（四类裁剪的第四类）

_OFFLOAD_TEMPLATE = (
    "[早前发的图片已卸载 —— 原图在 {path}，需要时用 read_file 取；"
    "或者让用户再发一次]"
)


def _human_bytes(n: int) -> str:
    """给人看的字节数（`note` 里会进上下文，写 `41943040` 没法读）。"""
    for unit, div in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if n >= div:
            v = n / div
            return ("%.0f %s" % (v, unit)) if v == int(v) else ("%.1f %s" % (v, unit))
    return "%d B" % n


def count_image_blocks(content: Any) -> int:
    """一段 `content` 里有几个图片块（`str` / 认不出的形状，`0`）。

    单独一个函数而不是内联 `sum(...)`：`providers/base.py` 的 token 估算
    要按它乘一个额度，而"什么算一个图片块"这个判断只许写一遍
    （两份会漂，而两个答案看着都对）。

    `0` 是"没有图"，不是"没读到" —— 这里不需要 `None`
    （"读不到"与 0 的区分针对的是"上游报了什么"，而这里是本地结构，数得清）。
    """
    if not isinstance(content, (list, tuple)):
        return 0
    return sum(1 for p in content
               if isinstance(p, dict) and p.get("type") == "image_url")


def count_audio_blocks(content: Any) -> int:
    """一段 `content` 里有几个音频块（0 = 没有音频）。"""
    if not isinstance(content, (list, tuple)):
        return 0
    return sum(1 for p in content
               if isinstance(p, dict) and p.get("type") == "input_audio")


def audio_token_allowance(content: Any) -> int:
    """音频按时长给额度；时长读不到时给一个保守常量。

    与 `IMAGE_TOKEN_ALLOWANCE` 同族：估算，不是账单。音频真实的 token
      数由上游按编解码器算，而我们要的只是"别让压缩闸门以为这一轮没成本"。
    读不到就按兜底算，不按 0（读不到 ≠ 没有）。
    """
    if not isinstance(content, (list, tuple)):
        return 0
    total = 0
    for p in content:
        if not (isinstance(p, dict) and p.get("type") == "input_audio"):
            continue
        try:
            secs = float(p.get("_seconds") or 0)
        except (TypeError, ValueError):
            secs = 0.0
        total += int(secs * AUDIO_TOKEN_PER_SECOND) if secs > 0 else AUDIO_TOKEN_FALLBACK
    return total


def offload_old_images(messages: Sequence[dict],
                       keep: int = KEEP_IMAGES_IN_REQUEST) -> int:
    """把最近 `keep` 张之外的图片块换成一句带路径的话。返回换了几张。

    ## 为什么要卸载

    图片在历史里每一轮都重发。一轮贴四张截图，往后每轮都带着那四张 ——
    账单随轮数线性涨，而模型早就不看它们了。四类裁剪里第四类（图片卸载）
    干的就是这件事。

    ## 两条判据

    * 卸载 ≠ 丢掉：换上去的那句话里带着路径，而 `data/attachments/`
      是可读的（`_framework_readable`），模型取得回来。
      这是「指针必须真能取到」的直接套用 —— 不写路径的卸载就是"把丢失
      伪装成清理"。
    * 就地改，不改结构：只替换块里面那一项，
      `content` 仍然是同一个列表、同样的块数，前后两条消息的 JSON 形状一致，
      压缩/分类器/持久化都不需要知道这里发生过什么。

    它是有副作用的（改 `messages` 里的对象）—— 所以调用点必须在
    发请求之前、且在 `messages` 的副本语义明确之后。返回计数是为了让
    调用方能出声（"卸载了 N 张"），不是给它做判据用。
    """
    if keep < 0:
        keep = 0

    # 先从后往前数出所有图片块的位置（`(msg_idx, part_idx)`），再决定换哪些。
    found: list[tuple[int, int, str]] = []
    for mi, msg in enumerate(messages):
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                path = _path_of(part)
                found.append((mi, pi, path))

    if len(found) <= keep:
        return 0

    replaced = 0
    for mi, pi, path in found[:-keep] if keep else found:
        messages[mi]["content"][pi] = {
            "type": "text",
            "text": _OFFLOAD_TEMPLATE.format(
                path=path or "（路径没记下来 —— 这张图取不回来了）"),
        }
        replaced += 1
    return replaced


# 七、跨轮次取回：落库的路径 -> 图片块
#
# ## 为什么需要它
#
# 落库的是文字（SQLite 的 `content` 是 TEXT 列，绑一个 list 直接
# `InterfaceError`，见 `core/memory/short_term.py:104`）。而用户的行为是：
# 「看看这张图」→ 下一轮「那按它改一下」。第二句还需要那张图。
#
#形状：消息行照旧只存文字，另在 `extra_json` 里记一串
# `"attachments": ["data/attachments/<摘要>.png"]`
# （`messages` 表本来就有 `extra_json` 列装非标准键，`load_messages` 会
# `msg.update(json.loads(extra_json))`，不加列、不改格式）。
# 发请求之前把这串路径还原成图片块。
#
# ## 两条判据
#
# * 还原失败要说出来（判据 3）：文件被 retention 清了 / 换过机器，
#   模型拿到的是「这张图现在取不回来了」，不是一句凭空消失的 `[图片 1]`。
# * 进程内缓存按 `(mtime, size)` 失效 —— 一次解码 + 一次 base64
#   在 2000px 上要几十毫秒，而它每轮都发生。
#   来源必须跟着值走，缓存也是。

#: 还原缓存的总预算。超了从最久没用到的那个开始丢。
#:
#: 为什么是预算而不是"定期清空"：图片是内容寻址的，正常会话里同一张图会被
#: 反复还原（每轮都要），清空等于每轮重解码。但电脑操作 / 浏览器每走一步
#: 就产生一张新截图（内容寻址 -> 每一步一个新键），没有上限时长跑必然攒到
#: GB 级。判据同 `_evicted` 那一类：需要预算，不是需要清空。
BLOB_CACHE_MAX_BYTES = 64 * 1024 * 1024

#: `{路径: (mtime_ns, size, data_url)}`，按最近使用排序。进程级 ——
#: 图片是内容寻址的，同一个路径的字节在正常情况下永不改变
#: （改了就说明有人手改过盘）。
_blob_cache: "OrderedDict[str, tuple[int, int, str]]" = OrderedDict()


def _blob_put(key: str, mtime: int, size: int, url: str) -> None:
    """写缓存，并按字节预算淘汰最久没用过的那些。

    预算按 `len(url)` 估（data URL 是 ASCII base64，字符数≈字节数）——
    这是代理量，不是精确内存占用；它只需要单调于真实占用。
    """
    _blob_cache[key] = (mtime, size, url)
    _blob_cache.move_to_end(key)
    total = sum(len(v[2]) for v in _blob_cache.values())
    while total > BLOB_CACHE_MAX_BYTES and len(_blob_cache) > 1:
        _k, _v = _blob_cache.popitem(last=False)
        total -= len(_v[2])


def rebuild_blocks(paths: Sequence[str], *, source: str = "") -> tuple[list[dict],
                                                                     list[str]]:
    """把一串落库路径还原成图片块，返回 `(块, 说明)`。

    顺序即参数顺序 —— 图片的序号是模型唯一的指代方式（「第 2 张」），
    所以这里不许重排、不许去重（同一张图发两次就是两张，模型看到两个块）。
    """
    blocks: list[dict] = []
    notes: list[str] = []
    for idx, raw in enumerate(paths, 1):
        if not raw:
            continue
        p = Path(raw)
        try:
            st = p.stat()
        except OSError:
            notes.append("第 %d 张图取不回来了（%s 不在了）"
                         % (idx, p.as_posix()))
            continue
        key = p.as_posix()
        cached = _blob_cache.get(key)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            url = cached[2]
            _blob_cache.move_to_end(key)          # 命中即刷新热度（LRU 序）
        else:
            try:
                url = _data_url(p.read_bytes(), _media_of(p))
            except Exception as exc:  # noqa: BLE001 —— 一张坏图不该毁掉整轮
                notes.append("第 %d 张图取不回来了（%s）：%s"
                             % (idx, p.as_posix(), exc))
                continue
            _blob_put(key, st.st_mtime_ns, st.st_size, url)
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return blocks, notes


def _media_of(p: Path) -> str:
    """按字节（不是扩展名）认类型 —— 同 `sniff_media_type` 那条判据。"""
    try:
        with p.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return "image/png"
    return sniff_media_type(head) or "image/png"


def reset_blob_cache() -> None:
    """丢掉还原缓存（测试用；盘上文件被换掉时也用它）。"""
    _blob_cache.clear()


def _path_of(part: dict) -> str:
    """从图片块里读出它的落盘路径。

    路径不写在块里（块是 wire 形状，多一个键上游可能不认），
    而是从 data URL 反推：内容寻址，摘要就是文件名。
    这就是为什么 `_spill` 用 sha256 前 16 位命名 —— 同一个函数
    在这里把摘要还原成路径。两处必须同源。
    """
    url = (part.get("image_url") or {}).get("url") or ""
    if not url.startswith("data:"):
        return ""
    try:
        raw = _decode_data_url(url)
    except Exception:  # noqa: BLE001
        return ""
    digest = hashlib.sha256(raw).hexdigest()[:16]
    for ext in (".png", ".jpg", ".webp", ".gif"):
        p = ATTACHMENT_DIR / (digest + ext)
        if p.exists():
            return p.as_posix()
    # 盘上没有（被清理了 / 换过机器）—— 仍然给一个猜得出来的路径，
    # 而不是空：空字符串会让那句话变成"取不回来"，而它多半只是还没写。
    return (ATTACHMENT_DIR / (digest + ".png")).as_posix()


__all__ = [
    "MAX_EDGE_PX", "MAX_BASE64_BYTES", "MAX_IMAGE_BYTES",
    "MAX_IMAGES_PER_TURN", "MAX_TURN_TOTAL_BYTES",
    "MAX_SOURCE_BYTES", "SUPPORTED_MEDIA", "ATTACHMENT_DIR",
    "KEEP_IMAGES_IN_REQUEST",
    "ImageInputError", "PreparedImage", "ImageToolResult",
    "sniff_media_type", "looks_like_image", "image_kind_name",
    "prepare_bytes", "prepare_path", "prepare_data_url",
    "prepare_chain", "content_text", "offload_old_images",
    "rebuild_blocks", "reset_blob_cache", "count_image_blocks",
    "count_audio_blocks", "audio_token_allowance",
    "TurnImages", "prepare_turn",
    "paths_in_text", "audio_paths_in_text", "rebuild_audio_blocks",
    "expand_attachments",
    "split_text_and_media", "append_text_block",
]
