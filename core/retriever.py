"""BM25 关键词检索（jieba 分词），不依赖向量库。

索引全在内存里 —— 每次 `build()` 都是一次全量重建，不落盘。
"""

from typing import Dict, List

import jieba
from rank_bm25 import BM25Okapi


def jieba_tokenizer(text: str) -> List[str]:
    """
    Tokenize *text* with jieba.

    For Chinese, jieba performs dictionary-based word segmentation.
    For pure-ASCII / English text, jieba effectively falls back to
    whitespace-delimited tokenization, which is correct for BM25.
    """
    return list(jieba.cut(text))


class BM25Retriever:
    """
    A lightweight in-memory retriever that indexes chunk dicts and
    returns the top-K most relevant chunks for a query using BM25.

    Hard constraint: retrieve() returns at most *top_k* chunks.
    The remaining chunks are never exposed. This enforces context isolation.
    """

    def __init__(self) -> None:
        self._chunks: List[Dict] = []
        self._bm25: BM25Okapi | None = None
        self._tokenized_corpus: List[List[str]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index(self, chunks: List[Dict]) -> None:
        """
        从 *chunks* 建（或重建）BM25 索引。

        每个 chunk 必须有 `"text"` 键。页面/位置元数据保留，但不参与排序。

        每次都整表重建 —— 旧索引直接丢弃。一份文档的分块是一次性收齐的，
        所以这个代价可以接受。
        """
        self._chunks = chunks
        self._tokenized_corpus = [jieba_tokenizer(c["text"]) for c in chunks]
        if self._tokenized_corpus:
            self._bm25 = BM25Okapi(self._tokenized_corpus)
        else:
            self._bm25 = None

    def retrieve(self, query: str, top_k: int = 3) -> List[Dict]:
        """
        Return up to *top_k* most relevant chunks for *query*.

        Returns an empty list if no chunks have been indexed or if
        no chunk has a positive BM25 score for the query.

        Hard constraint: only chunks with score > 0 are returned.
        This prevents irrelevant chunks from polluting the LLM context.
        """
        if self._bm25 is None:
            return []

        tokenized_query = jieba_tokenizer(query)
        scores = self._bm25.get_scores(tokenized_query)

        # Rank indices by descending score
        ranked = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )

        # Take top_k, but only those with positive scores
        # v1.0.0: When the corpus is very small (1-2 chunks), BM25 IDF
        # can be 0.0 for every term — all scores are zero.  In that
        # case fall back to returning the first top_k chunks rather
        # than returning nothing.
        result = []
        all_zero = all(s <= 0 for s in scores)

        for idx in ranked:
            if not all_zero and scores[idx] <= 0:
                break
            result.append(self._chunks[idx])
            if len(result) >= top_k:
                break

        return result

    def chunk_count(self) -> int:
        """Return the number of indexed chunks."""
        return len(self._chunks)
