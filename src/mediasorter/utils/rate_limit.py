"""Token bucket rate limiter for TMDB API.

TMDB allows 40 requests per 10 seconds. This module provides a simple
synchronous token bucket that blocks (sleeps) when the bucket is empty.
"""

from __future__ import annotations

import time
import threading


class TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, rate: float = 40, per: float = 10.0):
        """Create a token bucket.

        Args:
            rate: Number of tokens (requests) allowed.
            per: Time window in seconds.
        """
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Acquire a token, blocking if necessary until one is available."""
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                # Calculate wait time for next token
                wait = self.per / self.rate
            time.sleep(wait)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time. Must be called under lock."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        new_tokens = elapsed * (self.rate / self.per)
        self.tokens = min(self.rate, self.tokens + new_tokens)
        self.last_refill = now
