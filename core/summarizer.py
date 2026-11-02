"""
Iterative chunk-aggregation summarizer.

NEVER feeds the full document text to the LLM.  Instead:
  Round 1: batch chunks into groups → group summary.
  Round 2+: merge adjacent summaries → intermediate summary.
  Repeat until only 1 summary remains → final output.

For N chunks, the maximum LLM input in any single call is:
  - Round 1: chunks_per_batch * max_chars (adaptive, max ~7500 chars)
  - Merge rounds: merge_batch_size * intermediate_summary_chars (≤ 3×500=1500)

This is orders of magnitude faster than per-chunk summarization for
large documents (47 chunks: 17 calls vs 72 calls originally).
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional


# ----------------------------------------------------------------------
# 摘要结果缓存
# ----------------------------------------------------------------------
#
# 判据来自同一份文档在多次独立运行里被反复完整摘要 ——
# 而摘要是纯读、确定性输入、贵（最坏 45 次串行 LLM 调用）。
#
# 键按"内容哈希 + 影响输出的全部参数"，不按文件名。
#    · 按文件名：同一个文件改一个字节就漏掉重算（用旧摘要 = 撒谎）；
#    · 内容哈希：不同路径的同一份内容只算一次。
#    模型名必须进键 —— 换个模型给的就是另一段摘要，
#       而这在缓存里看不出来（两份都"对"，只是不是同一个回答）。
#
# 三条不许回退：
#   ① 空/失败的结果不许进缓存 —— 缓存的是一条"上次算出来的空"，
#      而用户看到的是"这文档没内容"（兜底不能静默失效）。
#   ② 缓存坏了按没缓存算，出声，不抛 —— 它是加速器，不是正确性的一部分。
#   ③ 有上界 —— 不设的话它会跟着用户解析过的每一份文档长下去
#      （而"跑久了才发作"的都不容易被发现）。
#: 不放 `data/memory/` 下面 —— 那里是记忆层；而运行数据
#: （可重生的缓存、校准、跨区缓存……）一律进 `data/runtime/`
#: （不是记忆层的东西。）
#: 这里是一条派生的、可重生的缓存，删掉只是慢一次，不是丢东西。
CACHE_DIR = Path("data/runtime/summarizer")
CACHE_MAX_ENTRIES = 200


def cache_key(chunks, summary_prompt: str, chunk_summary_max_chars: int,
              merge_batch_size: int, model: str) -> str:
    """内容 + 所有影响输出的参数 → 一个十六进制键。纯函数。"""
    h = hashlib.sha256()
    h.update(b"summarizer-cache-v1\x00")
    h.update((model or "").encode("utf-8"))
    h.update(b"\x00")
    h.update(summary_prompt.encode("utf-8"))
    h.update(b"\x00")
    h.update(("%d\x00%d\x00" % (chunk_summary_max_chars, merge_batch_size)).encode("utf-8"))
    for c in chunks:
        h.update((c.get("text") or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / (key + ".json")


def load_cached(key: str) -> Optional[str]:
    """命中就返回摘要，否则 `None`。任何异常都按"没缓存"算。"""
    p = _cache_path(key)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # 出声但继续 —— 缓存是加速器，不是正确性的一部分（判据 ②）。
        print(f"[summarizer] 缓存读不动（{type(e).__name__}: {e}）—— 按没缓存算")
        return None
    text = data.get("text") if isinstance(data, dict) else None
    if isinstance(text, str) and text.strip():
        return text
    return None


def save_cached(key: str, text: str) -> None:
    """写一条。空/失败的结果直接不写（判据 ①）。"""
    if not isinstance(text, str) or not text.strip():
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        final = _cache_path(key)
        tmp = final.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"text": text, "created_at": time.time(),
                        "chars": len(text)}, ensure_ascii=False),
            encoding="utf-8",
        )
        # 原子换名 —— 直接写目标文件的话，进程被打断会留下半截 JSON，
        #    而它读起来是一条"缓存坏了"（判据 ② 会兜住，但那是一次白跑）。
        os.replace(tmp, final)
        _evict()
    except Exception as e:
        print(f"[summarizer] 缓存写不下去（{type(e).__name__}: {e}）—— 这次不缓存")


def _evict() -> None:
    """按 mtime 从旧到新丢，留下最新的 `CACHE_MAX_ENTRIES` 条。"""
    try:
        files = sorted(CACHE_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime)
        excess = len(files) - max(0, int(CACHE_MAX_ENTRIES))
        for p in files[:excess]:
            try:
                p.unlink()
            except OSError:
                pass
    except Exception:
        pass


#: 单次摘要调用允许占用的上下文窗口比例。
#: 剩余 45% 留给系统提示、工具 schema、输出与安全余量。
_SINGLE_CALL_WINDOW_RATIO = 0.55

#: 折算字符数时故意保守使用 1.5 字符/token。
#:
#: 数据（deepseek-v4-flash）：一批 5 块 ≈7,500 中文字符，
#: API 返回的 prompt_tokens ≈4,000 —— 真实约为 1.9 字符/token。
#: 注意 tiktoken 的 cl100k 对中文只有 0.86 字符/token（同一段估成 8,881，
#: 高估 2.2 倍），所以不能用 cl100k 的估算值来定阈值。
#:
#: 它是公开常量：凡是"token 预算 → 字符额度"的折算
#: 一律用这一个，不要再写第二个常数。
CONSERVATIVE_CHARS_PER_TOKEN = 1.5
_CONSERVATIVE_CHARS_PER_TOKEN = CONSERVATIVE_CHARS_PER_TOKEN  # 向后兼容


def _single_call_char_budget(llm_client) -> int:
    """单次 LLM 调用能容纳的文档字符数上限。0 = 无法判断，走 map-reduce。

    131,072 窗口 → 131072 × 0.55 × 1.5 ≈ 108,000 字符。
    一份 94,009 字符的文档因此能一次装下，同时给系统提示、工具 schema
    和输出留出约 59,000 tokens。
    """
    window = getattr(llm_client, "context_window", 0) or 0
    if window <= 0:
        return 0
    return int(window * _SINGLE_CALL_WINDOW_RATIO * _CONSERVATIVE_CHARS_PER_TOKEN)


def iterative_summarize(
    chunks: List[Dict],
    llm_client,  # Provider client with .chat() method
    summary_prompt: str = "You are a document summarizer. Summarize the given text concisely and accurately.",
    chunk_summary_max_chars: int = 200,
    merge_batch_size: int = 3,
) -> str:
    """
    Generate a full-document summary via iterative aggregation.

    Args:
        chunks: List of chunk dicts, each with a `"text"` key.
        llm_client: Provider client instance with a `chat(messages)` method.
        summary_prompt: System prompt for the summarization LLM calls.
        chunk_summary_max_chars: Target max chars for round-1 per-batch summaries.
        merge_batch_size: Number of adjacent summaries to merge per round.

    Returns:
        Final summary string.

    先判断能不能一次读完。

    无条件走"分批 + 多轮归并"会让小文档也付多轮归并的钱 ——
    多轮归并还额外损失信息（每轮都是"摘要的摘要"）。

    一份 36.9 KB / 94,009 字符的实训日志：
        65 chunks → 13 批 → 13→5→2→1 四轮归并 = 21 次串行 LLM 调用、53.4 秒
    而这份文档全量只有 ~49,500 真实 tokens（窗口的 38%），一次就能读完。

    现在：装得下就一次调用，装不下才走 map-reduce。

    两条出口外面都包了一层结果缓存（按内容哈希 +
    影响输出的全部参数，见 `cache_key`）。
    缓存只管"要不要跑"，真正的逻辑在 `_summarize_uncached` ——
    拆出来就是因为缓存必须包住两条出口，塞进其中任意一条另一条就永远不缓存。
    """
    if not chunks:
        return "(no content to summarize)"

    # 先看缓存。它买的是"同一份文档不重复付 LLM 的钱"
    _model = getattr(llm_client, "_model", "") or ""
    _key = cache_key(chunks, summary_prompt, chunk_summary_max_chars,
                     merge_batch_size, _model)
    _hit = load_cached(_key)
    if _hit is not None:
        print(f"[summarizer] 命中缓存（{_key[:12]}…）—— **0 次 LLM 调用**")
        return _hit
    _result = _summarize_uncached(
        chunks, llm_client, summary_prompt,
        chunk_summary_max_chars, merge_batch_size,
    )
    save_cached(_key, _result)
    return _result


def _summarize_uncached(
    chunks: List[Dict],
    llm_client,
    summary_prompt: str,
    chunk_summary_max_chars: int,
    merge_batch_size: int,
) -> str:
    """真正干活的那一半 —— 缓存层只负责"要不要跑它"。

    拆出来的理由：缓存那两行必须包住两条出口（一次调用那条 /
    map-reduce 那条）。把缓存塞进任意一条里，另一条就永远不缓存 ——
    而它看起来是对的。
    """
    # 能一次读完就一次读完
    _budget = _single_call_char_budget(llm_client)
    _total_chars = sum(len(c.get("text", "")) for c in chunks)
    if _budget and _total_chars <= _budget:
        print(f"[summarizer] 文档 {_total_chars:,} 字符 ≤ 单次预算 {_budget:,} "
              f"—— 一次调用完成（跳过 map-reduce）")
        return _summarize_single(
            llm_client,
            system_prompt=summary_prompt,
            text="\n\n".join(c["text"] for c in chunks),
            max_chars=max(chunk_summary_max_chars * 8, 1200),
        )

    # ------------------------------------------------------------------
    # Adaptive batch sizing for Round 1
    #   ≤10 chunks → 1 chunk/batch  (original per-chunk behavior)
    #   11-30     → 3 chunks/batch   (~4500 chars per LLM call)
    #   31+       → 5 chunks/batch   (~7500 chars per LLM call)
    # This keeps Round-1 LLM calls bounded while never feeding
    # the full document to the model.
    # ------------------------------------------------------------------
    total = len(chunks)
    if total <= 10:
        round1_batch = 1
    elif total <= 30:
        round1_batch = 3
    else:
        round1_batch = 5

    # Round 1: batch chunks, summarize each batch
    batch_count = (total + round1_batch - 1) // round1_batch
    print(f"[summarizer] Round 1: {total} chunks → {batch_count} batches "
          f"({round1_batch} chunks/batch)")

    summaries: List[str] = []
    for i in range(0, total, round1_batch):
        batch = chunks[i : i + round1_batch]
        combined = "\n\n".join(c["text"] for c in batch)
        # Scale max_chars with batch size — more text needs longer summary
        batch_max_chars = chunk_summary_max_chars * min(round1_batch, 3)
        summary = _summarize_single(
            llm_client,
            system_prompt=summary_prompt,
            text=combined,
            max_chars=batch_max_chars,
        )
        summaries.append(summary)
        print(f"[summarizer]   batch {len(summaries)}/{batch_count} done")

    # ------------------------------------------------------------------
    # Round 2+: iterative merge
    # ------------------------------------------------------------------
    round_num = 2
    while len(summaries) > 1:
        group_count = (len(summaries) + merge_batch_size - 1) // merge_batch_size
        print(f"[summarizer] Round {round_num}: {len(summaries)} summaries "
              f"→ {group_count} groups (merge {merge_batch_size} at a time)")

        next_summaries: List[str] = []
        for i in range(0, len(summaries), merge_batch_size):
            group = summaries[i : i + merge_batch_size]
            if len(group) == 1:
                next_summaries.append(group[0])
            else:
                merged = _merge_summaries(
                    llm_client,
                    system_prompt=summary_prompt,
                    summaries=group,
                )
                next_summaries.append(merged)
        summaries = next_summaries
        round_num += 1

    print(f"[summarizer] Done — {round_num - 1} rounds total")
    return summaries[0]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _summarize_single(
    client,
    system_prompt: str,
    text: str,
    max_chars: int,
) -> str:
    """Ask the LLM to summarize a batch of chunk text."""
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Summarize the following text in under {max_chars} characters. "
                f"Focus on key facts, entities, and conclusions:\n\n{text}"
            ),
        },
    ]
    response = client.chat(messages)
    return (response.content or "").strip()


def _merge_summaries(
    client,
    system_prompt: str,
    summaries: List[str],
) -> str:
    """Merge multiple adjacent summaries into one coherent summary."""
    combined = "\n\n---\n\n".join(summaries)
    messages = [
        {
            "role": "system",
            "content": (
                f"{system_prompt}\n"
                "Merge the following summaries into one coherent, "
                "non-redundant summary. Eliminate duplicate information."
            ),
        },
        {"role": "user", "content": combined},
    ]
    response = client.chat(messages)
    return (response.content or "").strip()
