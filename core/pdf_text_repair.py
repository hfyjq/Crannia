# -*- coding: utf-8 -*-
"""PDF 的"文本层坏了"要有人管 —— 判出来、修回来、修不了就出声。

## 它治什么

字体没有 `/ToUnicode` 的 PDF，`page.get_text()/extract_text()` 会照常返回一串错字
（数字与英文是对的 —— 那些走的是另外几个正常字体）。
那串东西会原样进 chunk → 摘要 → 模型的上下文，全程零报警，
而用户看到的是"模型读了个 PDF 然后胡说"。

本仓对同一件事早就有一半答案：`parse_txt` 撞上二进制文件是会拒的
（`file_parser/handler.py` 的 `_binary_refusal`：「二进制文件，按文本读只会得到乱码」）。
txt 那层有人管、PDF 这层没人管 —— 这个模块就是那一层。

## 它不是乱码，是整体平移（解出来的，不是猜的）

在一份 415 页的真实 PDF 上读到：

```
原始 : 㘼傿㈍㨡㓸℧䓳…况2024⸶12㛊31㖧㬤⸶⹨䙆Ṽ㯓⸥1,918⃆℅㷝⯓2.8%
真值 : 智能手機收入由截至2024年12月31日止年度的人民幣1,918億元減少2.8%
```

`真字 = chr(原始码 + K)`，而 `K` 不是一个常数，是"一类字符一个"：

| 这一类 | 这一份的 `K` | 例 |
|---|---|---|
| 汉字 | `0x303E` | `0x36EE → 本` |
| CJK 标点 | `0x2D03` | `0x02FE → 、` |
| 全角形式 | `0x8E9B` | `0x706D → （` |

## 判"坏没坏"用的是峰度，不是"落没落在汉字区里"

这条判据写在这里，免得再走一遍：判
「`code + K` 有没有落在汉字区间里」—— 那条判据根本不判别：
对一个正常的中文 PDF，任何小的 K 都能把码留在 `U+4E00–U+9FFF` 里
（`U+4E00+0x1A` 减一点还是汉字），干净的 PDF 也被判成坏的，
还解出一个负偏移。

真信号是"解出来像不像人话"：拿一小撮高频汉字当先验，
数一数每个候选 `K` 能把多少字符解成它们。量下来：

| | 最高分的 `K` | 命中高频字 | 次高分 | 峰度 |
|---|---|---|---|---|
| 坏文件（714 字符 / 264 个码） | `+0x303E` ✓ | 15.1% | 5.3% | 2.85 |
| 干净文件（267 字符 / 11 个码） | `+0x811`（噪声） | 20.2% | 19.1% | 1.06 |

绝对值不判别（20% > 15%！），峰度判别（2.85 vs 1.06）。
而且样本量要够：11 个不同的码解不出任何偏移 —— 那种情况判「证据不足」，不猜。

## 三条纪律

1. 只对"没有 `/ToUnicode` 的字体"的 span 动手 —— 同一页上的拉丁字体
   （`DIN` / `Georgia`）本来就是对的，平移会把它毁掉。
2. 解不出来的码要报出来（`unresolved`），不静默丢 —— 读不到 ≠ 0。
3. 判据是"像不像自然语言"，不是"像不像 UTF-8" —— 这串东西本身是
   合法 UTF-8，任何编码检查都发现不了它。

## 边界（如实标，别读成"修好了就万事大吉"）

- 只修字符映射。表格的列分隔 / 空格丢了就丢了（资产负债表那一行
  会连成 `(438,291)60,778,2878,345,086…`）—— 有能力的模型能靠上下文读出来，
  但这不是我们的功劳，也不保证。
- 同一个目标区间里可能不止一个偏移，而"每个区间只取一条"，少数标点会解成
  同区间的另一个标点。结果：`（「ASP」）` 解成了 `（》 ASP「` ——
  因为 `「`(`0x0308`) 与 `、。`(`0x02FE`/`0x02FF`) 虽然都落 CJK 标点区，
  却不是同一个偏移（`+0x2D04` vs `+0x2D03`），而后者出现得多、把那条规则占住了。
不影响读（人看得懂），但不逐字精确。真要做到逐字，得放开"一区间一条"，
  而那会把噪声也放进来 —— 现在的取舍是有意的。
- 零 `unresolved` 不等于"全对"：只说明每个码都落进了某个"像字"的类里，
  不保证是那个字，验收要渲一页图核对。
- 本模块不调模型、不联网，纯函数 + pypdf（字体表）+ pdfplumber（字符表）。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Sequence, Tuple

#: pdfminer 对没有 CMap/ToUnicode 的 Type0 会输出 `(cid:12345)` 这种占位符。
#: 另一条取数路径给的是平移后的字符，pdfminer 给的是 CID 数字 ——
#: 而两者是同一个码位（坏字体 `(cid:14062)` = `0x36EE`）。
#:判据 / 偏移表都不用改，只在取数层把它还原成 `chr(N)`。
_CID_RE = re.compile(r"^\(cid:(\d+)\)$")


def raw_char_text(text) -> str:
    """`(cid:14062)` → `chr(14062)`；别的原样返回。"""
    t = str(text or "")
    m = _CID_RE.match(t.strip())
    if not m:
        return t
    try:
        code = int(m.group(1))
        return chr(code) if 0 < code <= 0x10FFFF else t
    except (ValueError, OverflowError):
        return t

#: 高频汉字先验（约 60 个字）。它是频率先验，不是答案 ——
#: 换一份文件、换一种语言，这几个字照样是最高频的那批。
#: 不要往里加"这份年报里的词" —— 那就把答案写死了。
COMMON_HANZI = (
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对"
    "生能而子那得于着下自之年过发后作里用道行所然家种事成方多经么去法学"
)
#: 汉字那批都是高频字，权重一样，不加权。
_HANZI_W: Dict[str, float] = {ch: 1.0 for ch in COMMON_HANZI}

#: 常用标点先验，带频率权重 —— 「标点」不是一类东西，`，` 比 `〈` 常见几百倍。
#:
#: 权重是必须的，不然会挑错：`0x7071` 这个码能被两个规则解释 ——
#: 落 CJK 标点区是 `「`、落全角区是 `，`。光看"像不像标点"两者都对，
#: 只有按频率才分得开（`，` 100 : `「` 5）。
#:数值是中文排版的常见比例（量级对就行，不是精确统计）。
_PUNCT_W: Dict[str, float] = {
    "，": 100, "。": 90, "、": 30, "；": 10, "：": 10, "？": 3, "！": 2,
    "（": 15, "）": 15, "「": 5, "」": 5, "《": 5, "》": 5, "〈": 0.5, "〉": 0.5,
    "『": 1, "』": 1, "—": 3, "…": 3, "·": 2, "％": 3, "／": 2, "　": 6,
    "×": 2, "℃": 1,
}
#: 兼容旧名（外部/测试按这个名字引）。
COMMON_PUNCT = "".join(_PUNCT_W)

#: 真文字可能落进去的类（用于"应用"那一步，不用于"发现"）。
TEXT_BLOCKS: Tuple[Tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK 统一表意文字
    (0x3000, 0x303F),   # CJK 符号与标点
    (0xFF00, 0xFFEF),   # 全角形式
    (0x2000, 0x206F),   # 通用标点
)

#: 判"坏了"的峰度门槛（最高分 ÷ 次高分）。
#: 余量：坏的那份 2.85、干净的那份 1.06，1.6 两头都不贴边。
_PEAK_RATIO = 1.6
#: 判"坏了"的命中率门槛（最高分覆盖的字符占比）。坏的那份 15.1%，干净的噪声 ≈ 19%
#:这一条不判别（干净那份更高！），它只是兜底防"冷启动"
#: （样本极小的时候，随便一个 K 都能偶然拿到高分，峰度也会虚高）。
_MIN_SHARE = 0.05
#: 证据下限：不同的码太少就解不出偏移，判「证据不足」，不猜。
_MIN_DISTINCT_CODES = 30
#: 采样目标：坏字体上至少攒这么多字符再判（不够就继续翻页，翻到 `_MAX_SCAN_PAGES`）。
_WANT_SAMPLE_CHARS = 1500
_MAX_SCAN_PAGES = 25
#: 只有 >= 它的码才参与 —— ASCII（< 0x80）是拉丁正文，
#: 它不在被平移的那一批里（真文件里数字与英文走别的字体，本来就对）。
#: 不加这条会怎样：合成用例里数字 `2`(0x32) 被某个标点偏移
#: 解成了 `。`(0x3002) —— 因为 `0x32 + 0x2FD0 = 0x3002`，看着完全合法。
_MIN_CODE = 0x80

#: 不要加"已经是字的码不许再搬"这类守卫。
#: 它看似能防 `，`(U+FF0C) 被某个标点偏移搬成 `。`(U+3002)，
#: 但那条判据原理上就是错的：平移后的汉字还是汉字
#: （`能`U+80FD 打坏成 `傿`U+50BF，两个都在 `U+4E00–9FFF` 里）
#:它会把真正该搬的一起挡在门外。
#: 那个"`，`被搬成`。`"的情况是合成器漏打全角标点造出的混合体，现实中不存在
#: （真文件里 `，` 有自己的码 `0x7071`）。

#: 最多解几个偏移（汉字 / 标点 / 全角 —— 再多就是过拟合噪声了）。
_MAX_OFFSETS = 4
#: 第一个之后的偏移要有多少字符支撑才收（占采样总字符的比例）。见 `find_offsets`。
_EXTRA_SHARE = 0.01

#: 每条先验只认自己那个"家区间" —— 这一条是踩出来的。
#:
#: 让每条先验都往 `TEXT_BLOCKS`（全部四个类）里铺的后果是：
#: 汉字那个偏移 `+0x303E` 把标点的码也抢走了 ——
#: 因为 `0x02FE + 0x303E = 0x333C`，也落在 CJK 标点区 `U+3000–U+303F` 里。
#:标点被映射成错字（虽然"看着像标点"），而真正的标点偏移（`+0x2D03`）
#: 永远没机会被发现（它的码已经被划走了）。
#:
#:判据：先验说的是"哪一类字"，就只在那 一类字生活的区间里收码。
_PRIORS: Tuple[Tuple[Dict[str, float], Tuple[Tuple[int, int], ...]], ...] = (
    (_HANZI_W, ((0x4E00, 0x9FFF),)),
    (_PUNCT_W, ((0x3000, 0x303F), (0xFF00, 0xFFEF), (0x2000, 0x206F))),
)


# 一、从数据里解偏移（不硬编码）

def _score(counter: Dict[int, int], prior: Dict[str, float],
           home: Tuple[int, int]) -> Counter:
    """每个候选 `K` 的得分 = 它能把多少字符解成 `prior` 里、且落在 `home` 里的字
    （按先验的权重加权）。

    做法是倒推：对每个 `(码 c, 次数 n)` 和 `prior` 里每个字 `ch`，
    `K = ord(ch) - c` 就是一个能把这批字符解成 `ch` 的偏移。
累加一遍即可，复杂度 `O(码数 × |prior|)`，不需要在 K 上做扫描。
    """
    s: Counter = Counter()
    lo, hi = home
    for c, n in counter.items():
        if c < _MIN_CODE:
            continue
        for ch, w in prior.items():
            r = ord(ch)
            if lo <= r <= hi:
                s[r - c] += n * w
    return s


#: 一个偏移至少要覆盖多少字符才收（占采样总字符的比例，按先验权重折算前）。
#: 不设的话会收进 `+0x-2264`（只有 4 个码、解出"不东三上"）这种纯噪声。
_MIN_OFFSET_SHARE = 0.01
#: 从 0.004 提到 0.01 的原因：孤零零一两个码的偏移分不出真假 ——
#: 有个假偏移 `+0xFC0D` 把 `。` 的码解成了 `，`（因为 `，` 的权重 100 > `。` 90），
#: 而它只覆盖一两个码。真文件里每条真偏移都占 1% 以上（`。` 是 1.1%）。


def find_offsets(counter: Dict[int, int]) -> List[Tuple[int, str, Tuple[int, int]]]:
    """解出 `[(K, 先验名, 目标区间), ...]`。纯函数。

    粒度是"每个目标区间一个 `K`" —— 不是"每条先验一个"、也不是"全书一个"。
    判据：这份文件里

    · `0x36EE → 本` 走 `+0x303E`（落进汉字区）
    · `0x02FE → 、` 走 `+0x2D03`（落进 CJK 标点区）
    · `0x706D → （` 走 `+0x8E9B`（落进全角区）

三个不同的目标区间，三个不同的 `K`。一个"全局常数"是解不出来的。

    标点那几条是"全局择优、取走再选"，不是逐区间各挑一个 ——
    `0x7071` 这一个码能被两个规则解释（落 CJK 标点区是 `「`、
    落全角区是 `，`），而光看"像不像标点"两者都成立。
逐区间各挑一个的话，`U+3000` 那条会先把 `0x7071` 抢走并解成 `「`。
    现在改成：先按权重算出全部候选，取全局最好的那条、把它覆盖的码划走，再选下一条
    （加上 `_PUNCT_W` 那些频率权重，`，`(100) 自然压过 `「`(5)）。

    发现用先验、应用用区间（两步判据不同，别混）：
    · 发现：哪个 `K` 能把最多的字符解成高频字 / 常用标点，`_score` 的 argmax；
    · 应用：不收码表 —— 只留 `K` 与目标区间，解的时候按规则算
      （`apply_rules`）。收码表是个坑：采样只覆盖一部分页，
      表里没有的码在别的页上就修不了（第 21 页仍是一片乱）。

    `K = 0` 永远排除 —— 它就是"不改"，而"不改"的正确表达是"不修"。
    """
    total = sum(counter.values()) or 1
    remaining: Dict[int, int] = dict(counter)
    found: List[Tuple[int, str, Tuple[int, int]]] = []
    for prior, homes in _PRIORS:
        # 汉字只有一个目标区间，一轮；标点有三个，最多挑三轮（一轮一个区间）
        for _ in range(len(homes) if len(homes) > 1 else 1):
            if not remaining:
                break
            best = None                       # (k, 加权得分, home)
            for home in homes:
                s = _score(remaining, prior, home)
                if 0 in s:
                    del s[0]                  # "不改"不是候选
                if not s:
                    continue
                k, w = s.most_common(1)[0]
                if best is None or w > best[1]:
                    best = (k, w, home)
            if best is None:
                break
            k, w, home = best
            claimed = [c for c in remaining if home[0] <= c + k <= home[1]]
            # 门槛按字符数算（不是加权分）—— 权重是为了排序，不是为了过闸
            if not claimed or sum(remaining[c] for c in claimed) / total < _MIN_OFFSET_SHARE:
                break
            for c in claimed:
                del remaining[c]
            found.append((k, _prior_name(prior), home))
    return found


def _prior_name(prior: Dict[str, float]) -> str:
    return "汉字" if prior is _HANZI_W else "标点"


def build_decoder(found: Sequence[Tuple[int, str, Tuple[int, int]]],
                  codes: Sequence[int]) -> Dict[int, str]:
    """把规则套在给定的一组码上，返回码 → 字（给测试与自检用）。

    规则才是产物，表不是 —— 生产那条路走 `Repair.decode()` 直接套规则，
    因为采样看不到的码在别的页上也要能修（存码表的话，第 21 页
    会因为它的码不在采样里而整页没修）。
    """
    table: Dict[int, str] = {}
    for c in codes:
        ch = apply_rules(found, c)
        if ch is not None:
            table[c] = ch
    return table


def apply_rules(found: Sequence[Tuple[int, str, Tuple[int, int]]], code: int) -> Optional[str]:
    """一个码按规则解成真字；解不出来返回 `None`（调用方原样留着）。

    按 `found` 的顺序取第一个命中的 —— 顺序就是"先试汉字那条、再试标点那两条"。
    """
    if code < _MIN_CODE:
        return None
    for k, _p, (lo, hi) in found:
        r = code + k
        if lo <= r <= hi:
            return chr(r)
    return None


# 二、判定：这份 PDF 的文本层坏了吗？

class Verdict:
    """判定读数。`garbled=False` 时 `reason` 说清为什么不动它。"""

    __slots__ = ("garbled", "reason", "best_k", "peak", "share", "distinct")

    def __init__(self, garbled: bool, reason: str, best_k: int = 0,
                 peak: float = 0.0, share: float = 0.0, distinct: int = 0) -> None:
        self.garbled = garbled
        self.reason = reason
        self.best_k = best_k
        self.peak = peak
        self.share = share
        self.distinct = distinct


def judge(counter: Dict[int, int]) -> Verdict:
    """这份文本的码位被平移过吗？ 纯函数，不碰文件。

    三道，顺序即短路：
      ① 证据够不够（不同的码 ≥ `_MIN_DISTINCT_CODES`）—— 不够就不猜；
      ② 最高分的 `K` 是不是 0 —— 是 0 就是"不用改"（正常文件的正确答案）；
      ③ 峰度（最高 ÷ 次高）≥ `_PEAK_RATIO` —— 这才是判别的那一条。
    """
    total = sum(n for c, n in counter.items() if c >= 0x20)
    distinct = sum(1 for c in counter if c >= 0x20)
    if distinct < _MIN_DISTINCT_CODES or not total:
        return Verdict(False, "证据不足（只有 %d 个不同的码）" % distinct,
                       distinct=distinct)

    s = _score(counter, _HANZI_W, _PRIORS[0][1][0])
    ranked = s.most_common(8)
    if not ranked:
        return Verdict(False, "没有可用的候选偏移", distinct=distinct)
    k, weight = ranked[0]
    # 次高要跳过"和最高挨着的那些 K" —— 相邻的 K 常常因为字的邻接而一起变高，
    # 它们不是另一个解释。判据是「存不存在另一个同样好的解释」。
    runner = next((w for kk, w in ranked[1:] if abs(kk - k) > 0x80), 0)
    peak = (weight / runner) if runner else float("inf")
    share = weight / total

    if k == 0:
        return Verdict(False, "最高分的偏移是 0（这份不用改）",
                       best_k=0, peak=peak, share=share, distinct=distinct)
    if share < _MIN_SHARE:
        return Verdict(False, "最高分只覆盖 %.1f%% （太薄）" % (share * 100),
                       best_k=k, peak=peak, share=share, distinct=distinct)
    if peak < _PEAK_RATIO:
        return Verdict(False, "最高分与次高分差距不够（峰度 %.2f）" % peak,
                       best_k=k, peak=peak, share=share, distinct=distinct)
    return Verdict(True, "偏移 +0x%X，命中 %.1f%%，峰度 %.2f" % (k, share * 100, peak),
                   best_k=k, peak=peak, share=share, distinct=distinct)


# 三、在真 PDF 上跑：认字体 → 普查 → 解偏移 → 逐 span 修

def _short_font_name(name: str) -> str:
    """`IHDOJK+HYQiHei-DZS-GBK-EUC-H` → `HYQiHei-DZS-GBK-EUC-H`（去掉子集前缀）。

    必须去 —— `get_fonts()` 给的是带前缀的，而 `span["font"]` 给的是不带的。
    不去的话匹配永远为假，整个修复静默失效（症状是解出来的文字看起来"没修"）。
    """
    return name.split("+", 1)[-1]


def broken_fonts(reader, pages: Sequence[int] | None = None) -> set:
    """哪些字体没有 `/ToUnicode`（= 读出来可能是错的）。

    判据是 `Subtype == /Type0` 且 `/ToUnicode` 不存在。
    `Type1`/`TrueType` 的简单字体靠 `/Encoding` 就够（`DIN` 那些数字就是这么对的），
    不能把它们算进来 —— 平移会把它们毁掉。

    取数层用 pypdf 的 `/Resources /Font` 遍历（不再依赖 PyMuPDF 的
      `get_fonts()/xref_get_key`）。判据一个字没改；
      修复算法（`judge`/`find_offsets`/`apply_rules`）与库无关。

    "没有 ToUnicode" 不等于"读出来一定是乱码"：pdfminer 对预定义 CMap
      的 Type0 也能解对（ReportLab 的 `STSong-Light` 就没有 ToUnicode，
      但 pdfplumber 读得完全正确）。这里只是候选集，最终由 `judge()`
      看"解出来像不像人话"来定 —— 判不出来就不动。
    """
    names: set = set()
    n = len(reader.pages)
    idx = list(pages) if pages is not None else range(n)
    for pno in idx:
        try:
            page = reader.pages[pno]
            res = page.get("/Resources")
            if res is None:
                continue
            res = res.get_object()
            fonts = res.get("/Font")
            if fonts is None:
                continue
            fonts = fonts.get_object()
            for key in fonts:
                try:
                    f = fonts[key].get_object()
                    if str(f.get("/Subtype")) != "/Type0":
                        continue
                    if f.get("/ToUnicode") is None:
                        base = str(f.get("/BaseFont") or "").lstrip("/")
                        if base:
                            names.add(_short_font_name(base))
                except Exception:                      # noqa: BLE001 - 坏一个不毁全表
                    continue
        except Exception:                              # noqa: BLE001
            continue
    return names


def census(path, fonts: set, want: int = _WANT_SAMPLE_CHARS,
           max_pages: int = _MAX_SCAN_PAGES) -> Dict[int, int]:
    """数坏字体那些字符里每个码出现多少次。

    翻页翻到攒够 `want` 个字符为止（页序是首 / 中 / 尾再往后铺）——
    只抽 3 页会得到"证据不足"，而全扫 415 页没必要。
    只数坏字体（纪律 ①）：好字体的码是正确的字符，混进来会把先验的峰摊平。

    字表用 pdfplumber 的 `page.chars`（每个 char 自带 `fontname` + `text`），
      不再走 PyMuPDF 的 `get_text("dict")` span。
      pdfplumber 会把同一段的字符拆成单字符 —— 计数口径与 span 一样，
      只是"一批一批"变成"一个一个"。
    """
    import pdfplumber

    c: Counter = Counter()
    with pdfplumber.open(str(path)) as pdf:
        n = len(pdf.pages)
        order: List[int] = []
        if n:
            order = sorted({0, n // 2, n - 1})
            head = [i for i in range(min(n, max_pages)) if i not in order]
            order += head
        got = 0
        for pno in order[:max_pages]:
            try:
                chars = pdf.pages[pno].chars
            except Exception:                          # noqa: BLE001
                continue
            for ch in chars:
                if _short_font_name(ch.get("fontname", "")) not in fonts:
                    continue
                for t in raw_char_text(ch.get("text") or ""):
                    if ord(t) >= _MIN_CODE:
                        c[ord(t)] += 1
                        got += 1
            if got >= want:
                break
    return dict(c)


class Repair:
    """一次修复的读数。`found` 是规则不是表；`unresolved` 是解不出来的那些码。"""

    __slots__ = ("fonts", "found", "unresolved", "verdict")

    def __init__(self, fonts: set, found, unresolved: Dict[int, int],
                 verdict: Verdict) -> None:
        self.fonts = fonts
        self.found = found
        self.unresolved = unresolved
        self.verdict = verdict

    def decode(self, text: str) -> str:
        """修一段文本。调用方负责只把坏字体的 span 送进来。"""
        out = []
        for c in text:
            ch = apply_rules(self.found, ord(c))
            out.append(ch if ch is not None else c)
        return "".join(out)

    def summary(self) -> str:
        ks = " · ".join("+0x%X" % k for k, _p, _h in self.found) or "（无）"
        return ("文本层修复：偏移 %s，%s，采样里未解 %d 个不同的码"
                % (ks, self.verdict.reason, len(self.unresolved)))


class Diagnosis:
    """一次诊断的三个读数：哪些字体没有桥 · 判定 · 修不修得了。

    `repair_plan()` 返回 `None` 有两种含义（"没坏"和"坏了但修不了"），
    而调用方对这两种的处理完全不同（前者照常读，后者要出声）。
用这个类把它们分开 —— 别让一个 `None` 背两种意思。
    """

    __slots__ = ("fonts", "verdict", "repair")

    def __init__(self, fonts: set, verdict: Verdict, repair: Optional[Repair]) -> None:
        self.fonts = fonts
        self.verdict = verdict
        self.repair = repair


def diagnose(path) -> Optional[Diagnosis]:
    """只读，不改任何东西。返回 `None` 表示"连一个可疑字体都没有"。

    三种结局：
      · `None`                    —— 没有可疑字体 / 判不出来（照常读）
      · `Diagnosis(repair=None)`  —— 判出来是坏的，但偏移没解出来，调用方要出声
      · `Diagnosis(repair=...)`   —— 坏的，而且修得了

    入参是路径 —— pypdf 取字体表、pdfplumber 取字符表，
      两把仪器各管一半。
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    fonts = broken_fonts(reader)
    if not fonts:
        return None
    cnt = census(path, fonts)
    if not cnt:
        return None
    v = judge(cnt)
    if not v.garbled:
        return None
    found = find_offsets(cnt)
    if not found:
        return Diagnosis(fonts, v, None)
    unresolved = {c: n for c, n in cnt.items() if apply_rules(found, c) is None}
    return Diagnosis(fonts, v, Repair(fonts, found, unresolved, v))


def repair_plan(path) -> Optional[Repair]:
    """（薄封装）坏了且修得了才返回一份修复计划，否则 `None`。

    要区分"没坏"与"坏了修不了"就用 `diagnose()` —— 这个函数把两种 `None` 合并了。
    """
    d = diagnose(path)
    return d.repair if d is not None else None


__all__ = [
    "TEXT_BLOCKS", "COMMON_HANZI", "COMMON_PUNCT", "Repair", "Verdict", "Diagnosis",
    "find_offsets", "build_decoder", "apply_rules", "judge",
    "broken_fonts", "census", "diagnose", "repair_plan", "short_font_name",
    "raw_char_text",
]


#: 对外也留一个不以下划线开头的别名（`parse_pdf` 要用它匹配 span 的字体名）。
short_font_name = _short_font_name
