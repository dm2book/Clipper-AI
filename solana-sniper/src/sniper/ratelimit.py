"""Token-bucket rate limiting, one bucket per external service."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Allows ``rate`` acquisitions per second with bursts up to ``burst``.

    Waiters are served in arrival order: the lock is held while sleeping, so a
    later caller cannot overtake an earlier one.
    """

    def __init__(self, rate: float, burst: float | None = None, *, clock=time.monotonic):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = burst if burst is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self.capacity:
            raise ValueError("cost exceeds bucket capacity")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)
