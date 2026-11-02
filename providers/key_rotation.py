"""
KeyRotator — thread-safe API key rotation with fault-marking.

Fault-marking keeps a failing key out of rotation for a cooldown
window; the design is simplified for Crannia's single-process
architecture.

Usage::

    rotator = KeyRotator(["key-a", "key-b", "key-c"], cooldown_seconds=60)
    for attempt in range(3):
        try:
            key = rotator.current()
            response = make_api_call(api_key=key)
            rotator.mark_success()
            break
        except AuthError:
            rotator.mark_fault(key)     # skip this key for cooldown period
        except RateLimitError:
            rotator.mark_fault(key)     # same — let it cool down

"""

import threading
import time
from typing import Dict, List, Optional


class KeyRotator:
    """Thread-safe round-robin key rotation with fault cooldown.

    When a key returns 401 / 429 / 403 it is temporarily marked
    "faulty" and skipped for *cooldown_seconds*.  After the cooldown
    expires it is automatically re-added to the rotation.
    """

    def __init__(
        self,
        api_keys: List[str],
        cooldown_seconds: float = 60.0,
    ) -> None:
        if not api_keys:
            raise ValueError("api_keys must be a non-empty list")
        self._keys = list(api_keys)
        self._cooldown = cooldown_seconds
        self._lock = threading.RLock()
        self._index: int = 0
        self._faulted: Dict[str, float] = {}  # key -> faulted_at timestamp

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def current(self) -> str:
        """Return the next healthy key (round-robin), skipping faulted keys.

        If ALL keys are faulted, returns the key with the oldest fault
        timestamp (best-effort fallback).
        """
        with self._lock:
            now = time.monotonic()

            # Expire any fault whose cooldown has passed
            expired = [
                k for k, ts in self._faulted.items()
                if now - ts >= self._cooldown
            ]
            for k in expired:
                del self._faulted[k]

            # Try to find a healthy key
            n = len(self._keys)
            for _ in range(n):
                key = self._keys[self._index]
                self._index = (self._index + 1) % n
                if key not in self._faulted:
                    return key

            # All keys faulted — pick the one with oldest fault timestamp
            return min(self._faulted, key=lambda k: self._faulted[k])

    def mark_fault(self, key: str) -> None:
        """Mark *key* as faulty — it will be skipped until cooldown expires."""
        with self._lock:
            self._faulted[key] = time.monotonic()

    def mark_success(self) -> None:
        """No-op in this implementation (reserved for future metrics)."""

    @property
    def healthy_count(self) -> int:
        """Number of non-faulted keys."""
        with self._lock:
            now = time.monotonic()
            faulted = sum(
                1 for ts in self._faulted.values()
                if now - ts < self._cooldown
            )
            return len(self._keys) - faulted

    @property
    def total_keys(self) -> int:
        return len(self._keys)

    def snapshot(self) -> dict:
        """Debug: return current rotation state."""
        with self._lock:
            return {
                "total": len(self._keys),
                "healthy": self.healthy_count,
                "index": self._index,
                "faulted": dict(self._faulted),
                "cooldown_s": self._cooldown,
            }
