# -*- coding: utf-8 -*-
"""ICE 网络侧 —— 本地启发式。

## 它的定位只有一句话：弱信号，如实标注，只报不拦

它的判据：
> 域名熵 · 同形字/品牌仿冒 · 超新域名 · 异常 TLD · 无 HTTPS。
> 覆盖不了「被挂马的正规站」（域名信誉完全正常，本地零信号）。

而它这一节自己还写着：本地启发式「可后补（弱信号 + 误报会消耗信任）」。
那个"误报消耗信任"是本模块全部设计取舍的来源：

  · 不拦（拦住一个正经站点，比漏报更贵 —— 用户会去关掉整个网络层）；
  · 阈值往保守那头压（宁可少报）；
  · 每条命中必须说得出它凭什么（一句话，见 `Signal.why`），
    而不是一个"可疑"的形容词 —— 后者用户没法判断，只能无视。

## 五条里只做了三条，另外两条明说不做

| 那一条 | 做了吗 | 凭据 |
|---|---|---|
| 域名熵 | ✅ | `_entropy` —— 算出来的，不需要任何名单 |
| 同形字 | ✅ | `_mixed_script` —— 检测脚本混用（拉丁域名里混进西里尔/希腊字母） |
| 无 HTTPS | ✅ | `scheme == "http"` |
| 异常 TLD | ❌ | 那是一张会烂的名单，而"枚举域名/TLD"的判据是必然漏。更要紧的是误报方向：`.zip` / `.mov` / `.xyz` 今天都有正经站点在用，拿它们当信号消耗的正是信任。不做，不是忘了。 |
| 超新域名 | ❌ | 它压根不是"本地"能算的 —— 要么查 WHOIS（要出网，与"纯本地、不外接任何 feed"这条根判据冲突），要么维护一张注册时间表（那还是外接）。这条不该做，不是"以后补"。 |

`同形字` 为什么不带品牌名单：带名单就得维护 `apple` / `paypal` / `微信` …
—— 又一个会烂的清单。脚本混用是零名单的：`аpple.com`（首字母是西里尔 `а`，
U+0430）看一眼就与 `apple.com` 无异，但它的字符集里同时有拉丁和西里尔
正经域名几乎不会这样（少数多语言域名例外，所以它也只是弱信号）。
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import List, Tuple
from urllib.parse import urlsplit

__all__ = ["Signal", "assess", "note_signals", "ENTROPY_MIN", "ENTROPY_MIN_LEN"]


@dataclass(frozen=True)
class Signal:
    """一条弱信号。`key` 是机读的，`why` 是给用户看的一句话。

    判据：`why` 必须说清"我凭什么这么说" —— 一个只写「可疑」的信号，
    用户没法据此判断，只能学会无视它（那就是这张表烂掉的方式）。
    """
    key: str
    why: str


#: 熵阈值是量出来的，不是拍的 —— 而量完发现熵单独判不出来：
#:
#:     github 2.58 · baidu 2.32 · openai 2.58 · wikipedia 2.64
#:     stackoverflow 3.55  ← 正经域名
#:     x7fq2k9mvz3p   3.58  ← 随机串
#:
#:`stackoverflow` 与随机串的熵几乎一样。只卡熵 = 对长英文域名误报
#: （而误报正是这个模块的全部风险 —— "误报会消耗信任"）。
#: 真正分得开的是"字母与数字混着" —— 自动生成的域名几乎总带数字，
#: 而正经英文单词不会。三条一起卡：长度 + 熵 + 同时有字母和数字。
ENTROPY_MIN = 3.5
ENTROPY_MIN_LEN = 12

#: 参与"脚本混用"检测的脚本。只认这三个：它们是同形字攻击的主要来源，
#: 而且都是字母表（不会误伤中文/日文域名那种本来就混着的形态）。
_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK")


def _registrable_label(host: str) -> str:
    """拿主标签（`a.b.example.com` -> `example`）。不查公共后缀表 ——

    一份 PSL 是会烂的名单，而这里要的只是"哪个词看起来像随机串"。
    取倒数第二段在 `example.co.uk` 上会取到 `co`（低熵，不报），
    代价是漏报而不是误报 —— 与"弱信号宁可少报"同一方向。
    """
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else ""


def _entropy(s: str) -> float:
    """香农熵（每字符位数）。空串，`0.0`（不是 `None`：这里没有"读不到"）。"""
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _mixed_script(s: str) -> Tuple[str, ...]:
    """这个串里同时出现了哪几种字母表（>= 2 才可疑）。"""
    seen = set()
    for ch in s:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for sc in _SCRIPTS:
            if name.startswith(sc):
                seen.add(sc)
                break
    return tuple(sorted(seen))


def assess(url: str) -> List[Signal]:
    """URL → 弱信号列表（空列表是常态）。纯函数，不读设置、不出网。

    它从不判断"这个站点坏不坏" —— 它只说"这个 URL 有几处
    *在正常站点上不该出现* 的形态"。两者的差别就是误报的来源，也是本模块
    只报不拦的理由。
    """
    out: List[Signal] = []
    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
    except ValueError:
        return out
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        return out

    # ① 无 HTTPS —— 只认"明写 http://"的。
    #    裸主机名（`github.com`）在 `urlsplit` 里 scheme 是空串，而上面为了拿到
    #    hostname 给它补了个 `http://` —— 那个补出来的 scheme 不是用户的意图
    #拿它报"没有加密"是凭空造出来的误报
    #    （拿补出来的 scheme 去报，`github.com` 会被误报）。
    #    措辞如实：这是「没有加密」，不是「这是个坏站」。
    if url.lower().lstrip().startswith("http://"):
        out.append(Signal("no_https",
                          "明文 http（这条链路上没有加密）"))

    # ② 脚本混用（同形字的主要形态）
    scripts = _mixed_script(host)
    if len(scripts) >= 2:
        out.append(Signal("mixed_script",
                          "域名里混了不同字母表（%s）—— 同形字假冒的典型形态"
                          % "/".join(scripts)))

    # ③ 高熵主标签（随机串域名）。三条一起卡 —— 见上面 `ENTROPY_MIN` 那段
    #    （熵单独判不出来：`stackoverflow` 3.55 vs 随机串 3.58）。
    label = _registrable_label(host)
    if (len(label) >= ENTROPY_MIN_LEN
            and _entropy(label) >= ENTROPY_MIN
            and any(c.isdigit() for c in label)
            and any(c.isalpha() for c in label)):
        out.append(Signal("high_entropy",
                          "域名主体像随机串（%s，熵 %.2f）—— 自动生成的域名常这样"
                          % (label, _entropy(label))))

    return out


def note_signals(url: str, *, where: str, session_key: str = "") -> List[Signal]:
    """出站那一刻记一笔（控制台一行 + 事件流一条）。绝不阻断。

    与 `canary.check_outbound` 的区别就是这一句：**那个返回阻断回执，
    这个只返回信号**。调用方拿到空列表，什么都不做（常态）。

    开关复用网络层那个总闸（`net_protection_enabled`）—— 与金丝雀同一格：
    关掉网络层，这一整套都不识别。
    """
    try:
        from core.ice.guard import net_protection_enabled
        if not net_protection_enabled():
            return []
    except Exception:  # noqa: BLE001
        return []
    sigs = assess(url)
    if not sigs:
        return []
    shown = "；".join(s.why for s in sigs)
    print("[ice] 网络侧弱信号（%s）：%s" % (where, shown))

    key = str(session_key or "")
    if not key:
        try:
            from core import request_snapshot
            snap = request_snapshot.get_current()
            key = str(getattr(snap, "session_id", "") or "")
        except Exception:  # noqa: BLE001
            key = ""
    if not key:
        return sigs
    try:
        from core.events import EVT_ICE_WEAK, get_event_bus
        get_event_bus().emit(EVT_ICE_WEAK, session_key=key,
                             where=str(where),
                             keys=[s.key for s in sigs],
                             reasons=[s.why for s in sigs])
    except Exception as exc:  # noqa: BLE001
        print("[ice] 弱信号事件发失败（%s: %s）" % (type(exc).__name__, exc))
    return sigs
