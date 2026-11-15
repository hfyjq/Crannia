"""纯文本的公共形状 —— 切段 · 判断像不像 markdown。

两个 IM 通道都要这两件（飞书 / 钉钉），而且判据必须一致：
同一段回复在两条通道上不该"一边切成三段、一边不切"。

这里不发送、不渲染 —— 只回答两个问题：
  ① 这段话一次发得完吗（切到哪）？
  ② 要不要走 rich 那条路（`text` 消息不渲染 markdown）？
"""

from __future__ import annotations

#: 一次发多少字符。取保守值：中文按 UTF-8 是 3 字节，而各家上限按字节算的居多。
TEXT_CHUNK_CHARS = 4000

#: 看着像 markdown 的记号 —— 有它就值得走 rich（卡片 / markdown 消息）。
_MD_HINTS = ("**", "```", "](", "## ")


def split_text(text: str, limit: int = TEXT_CHUNK_CHARS) -> list[str]:
    """按段落/句子切段，保证每段 ≤ *limit* 字符。

    判据：先在换行处切，再退到句号，都不行才硬切 —— 硬切会把
    一个表格行或一句命令拦腰截断，而那种"少半句"的回复看起来像模型说错了。
    """
    s = str(text or "")
    if not s:
        return []
    if len(s) <= limit:
        return [s]
    out: list[str] = []
    rest = s
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            for punct in ("。", "！", "？", "\n", ".", "!", "?"):
                pos = rest.rfind(punct, 0, limit)
                if pos > limit // 2:
                    cut = pos + 1
                    break
        if cut <= 0:
            cut = limit
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out


def looks_like_markdown(text: str) -> bool:
    """要不要按 rich 发。

    判据：有 markdown 记号就走 —— `text` 消息会把 `加粗` 原样打出来，
    而那正是模型最常用的写法。不认 `__`（Python 标识符里到处都是），
    也不设长度门槛：一句 14 字的 `已发送` 同样会被打回原形。
    """
    s = str(text or "")
    return any(h in s for h in _MD_HINTS)
