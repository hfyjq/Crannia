"""
TokenCalibrator — self-calibrating token estimator.

Every LLM API call returns `usage.prompt_tokens` — the ground truth.
By comparing this against our pre-flight estimate (tiktoken or heuristic),
we maintain an exponential moving average (EMA) correction factor per
provider.  After just 3 turns the factor converges and the compressor's
`should_compress` check receives near-accurate token counts.

Persistence: `data/runtime/calibration.json` — survives restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict


class TokenCalibrator:
    """
    Per-provider calibration tracker.

    Usage::

        cal = TokenCalibrator("deepseek")
        est = provider.estimate_message_tokens(messages)  # raw estimate

        # … after API call …
        cal.feed(estimated=est, actual=response.usage.prompt_tokens)

        # Next pre-flight check:
        corrected = int(est * cal.factor)
    """

    #: 估算器"量纲版本" —— 改一次 `estimate_request_tokens` 的计算口径就 +1。
    #:
    #: 为什么必须有这个号：因子是"我们的估算 → API 实数"的乘数，
    #: 它只对产生它的那个估算器有意义。估算器漏算一部分时（例如漏算 tools），
    #: EMA 会被推到一个补偿性的值并落盘；修好估算器之后，那批旧样本
    #: 是另一个量的样本 —— 拿它们乘新估算值，等于用一个为错误量纲
    #: 校准的尺子去量正确的数。所以旧口径的样本一律作废、重新累积。
    #:
    #: 1 = 只算 messages + system（旧）
    #: 2 = messages + system + tools，一处应用因子
    #: 3 = **`messages` 那一格里补上 assistant 消息上 `content` 之外的**
    #:     两样（`reasoning_content` + `tool_calls` 参数全文）——
    #:     它们一直在 wire 上、一直没被算过，实际占请求的 2/3。
    #:口径再一次变了，scheme-2 那批样本是另一个量的样本。
    ESTIMATOR_SCHEME = 3

    _CALIBRATION_PATH = Path("data/runtime/calibration.json")

    def __init__(self, provider_name: str) -> None:
        self._provider = provider_name
        self._ema: float = 1.0       # exponential moving average of ratio
        self._samples: int = 0       # number of feed() calls
        self._alpha: float = 0.3     # EMA smoothing (0.3 = fast adaptation)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, estimated: int, actual: int) -> float:
        """
        Feed one ground-truth pair into the calibrator.

        *estimated* is the pre-flight token count from our estimator.
        *actual* is `usage.prompt_tokens` from the API response.

        Returns the unclamped ratio, so callers can log an outlier
        instead of only its clamped effect.  (Both ends must come from
        `estimate_request_tokens` — a pair whose two ends measure
        different things is not a sample of anything.)
        """
        if estimated <= 0 or actual <= 0:
            return 0.0

        ratio = actual / estimated
        # Clamp outliers: a single anomalous turn shouldn't throw off the EMA
        clamped = max(0.5, min(ratio, 3.0))

        self._ema = self._alpha * clamped + (1.0 - self._alpha) * self._ema
        self._samples += 1
        return ratio

    @property
    def factor(self) -> float:
        """
        Current calibration factor.

        Returns 1.0 until at least 3 samples have been collected (cold-start
        safety — an uncalibrated factor would be worse than the raw estimate).
        """
        if self._samples < 3:
            return 1.0
        return round(self._ema, 4)

    @property
    def is_calibrated(self) -> bool:
        """True once enough samples have been collected."""
        return self._samples >= 3

    @property
    def samples(self) -> int:
        return self._samples

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist calibration state to disk."""
        all_data = self._load_all()
        all_data[self._provider] = {
            "ema": self._ema,
            "samples": self._samples,
            "scheme": self.ESTIMATOR_SCHEME,
        }
        self._CALIBRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._CALIBRATION_PATH.write_text(
            json.dumps(all_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self) -> bool:
        """
        Restore calibration state from disk.

        Returns True if previous calibration data was found **and is
        comparable** — see :attr:`ESTIMATOR_SCHEME`.  A file written by an
        older estimator is discarded with a printed reason rather than
        silently reused: its numbers are samples of a different quantity,
        so a stale compensation factor would keep multiplying every estimate
        after the estimator was fixed.
        """
        all_data = self._load_all()
        entry = all_data.get(self._provider)
        if entry is None:
            return False
        scheme = entry.get("scheme")
        if scheme != self.ESTIMATOR_SCHEME:
            print(
                f"[calibrator] 丢弃 {self._provider} 的旧样本 "
                f"({entry.get('samples')} 个, ema={entry.get('ema')}, "
                f"scheme={scheme!r} != {self.ESTIMATOR_SCHEME}): "
                f"估算器口径变了，旧样本是另一个量的样本"
            )
            return False
        self._ema = float(entry.get("ema", 1.0))
        self._samples = int(entry.get("samples", 0))
        return self._samples > 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @classmethod
    def _load_all(cls) -> dict:
        if not cls._CALIBRATION_PATH.exists():
            return {}
        try:
            return json.loads(
                cls._CALIBRATION_PATH.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            return {}

    def __repr__(self) -> str:
        return (
            f"TokenCalibrator({self._provider!r}, "
            f"factor={self.factor:.4f}, samples={self._samples})"
        )
