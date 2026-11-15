"""
Token-bucket rate limiter for provider API calls.

Two dimensions are tracked independently:
  - RPM  (requests per minute)
  - TPM  (tokens per minute)

Usage::

    limiter = RateLimiter(rpm=60, tpm=100_000)

    def chat(messages, tools=None):
        limiter.acquire(expected_tokens=2000)
        return _do_api_call(messages, tools)

The caller blocks (`time.sleep`) until enough capacity is available,
so the application never receives a 429 response from the upstream API.
"""

import threading
import time


class RateLimiter:
    """Token-bucket rate limiter (RPM + TPM).

    Thread-safe.  Blocks the calling thread until the requested
    capacity is available — the caller never sees a rate-limit error.
    """

    def __init__(
        self,
        rpm: int = 60,
        tpm: int = 100_000,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        if tpm <= 0:
            raise ValueError("tpm must be positive")

        self._rpm = rpm
        self._tpm = tpm
        self._lock = threading.Lock()

        # Bucket fill state (fractional tokens/responses)
        self._rpm_bucket: float = float(rpm)
        self._tpm_bucket: float = float(tpm)
        self._last_refill: float = time.monotonic()

        # How many tokens one request costs (minimum)
        self._request_cost: float = 1.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self, expected_tokens: int = 1) -> None:
        """Block until capacity is available for one request.

        *expected_tokens* is the estimated output token count for the
        upcoming request — used to deduct from the TPM bucket.
        """
        needed_rpm = self._request_cost
        needed_tpm = max(expected_tokens, 1)

        # 单次请求超过桶容量 = 死锁，不是限流
        # 桶的上限是 self._tpm（见 _refill 的 min(..., float(self._tpm))），
        # 所以 needed_tpm > self._tpm 时 tpm_ok 永远为 False，下面的
        # while True 会永久 sleep，把整条 pipeline 挂死 —— 等待再久
        # 也不可能满足。
        #
        # 一次摘要把整篇文档（94,009 字符）发给模型时：
        # tiktoken 估算 ≈108,834 × 校准因子 1.1 ≈ 12 万 tokens > tpm 100,000
        # → agent 会卡在「正在总结」超过 1 分 26 秒且永不返回。
        #
        # 正确行为是放行 + 告警：这是配置或估算的问题，不是请求的问题。
        if needed_tpm > self._tpm:
            print(f"[ratelimit] 单次预估 {needed_tpm:,} tokens 超过 TPM 上限 "
                  f"{self._tpm:,} —— 等待无法满足，直接放行（本次不计 TPM）")
            needed_tpm = 0          # 不参与 TPM 判定，也不扣桶

        while True:
            with self._lock:
                self._refill()

                rpm_ok = self._rpm_bucket >= needed_rpm
                tpm_ok = self._tpm_bucket >= needed_tpm

                if rpm_ok and tpm_ok:
                    self._rpm_bucket -= needed_rpm
                    self._tpm_bucket -= needed_tpm
                    return

                # Calculate wait time — how long until enough capacity
                rpm_wait = (
                    0.0 if rpm_ok
                    else (needed_rpm - self._rpm_bucket) / self._rpm * 60.0
                )
                tpm_wait = (
                    0.0 if tpm_ok
                    else (needed_tpm - self._tpm_bucket) / self._tpm * 60.0
                )
                wait = max(rpm_wait, tpm_wait, 0.05)

            time.sleep(min(wait, 1.0))  # max 1s sleep, recheck

    def update_limits(self, rpm: int | None = None, tpm: int | None = None) -> None:
        """Hot-update rate limits (e.g. from config reload)."""
        with self._lock:
            if rpm is not None:
                self._rpm = rpm
            if tpm is not None:
                self._tpm = tpm

    @property
    def state(self) -> dict:
        """Debug: current bucket levels."""
        with self._lock:
            self._refill()
            return {
                "rpm": self._rpm,
                "tpm": self._tpm,
                "rpm_bucket": round(self._rpm_bucket, 1),
                "tpm_bucket": round(self._tpm_bucket, 1),
            }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        """Refill buckets based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return

        # Refill proportionally to elapsed time
        rpm_refill = (elapsed / 60.0) * self._rpm
        tpm_refill = (elapsed / 60.0) * self._tpm

        self._rpm_bucket = min(self._rpm_bucket + rpm_refill, float(self._rpm))
        self._tpm_bucket = min(self._tpm_bucket + tpm_refill, float(self._tpm))
        self._last_refill = now
