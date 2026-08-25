"""Connector-local per-destination token bucket (FAR-411).

The REST connector's per-destination outbound rate limit lives here rather than
reaching into ``modulo.core`` — the import-linter contract forbids connectors
importing core, so ``modulo.core.rate_limiter.TokenBucket`` cannot be reused.
Semantics mirror the core bucket: continuous refill up to ``burst``, guarded by
an ``asyncio.Lock`` so concurrent tasks never go below zero tokens.

Best-effort per-process: each uvicorn/SAQ worker owns its own bucket; Redis
backing is deferred (future work) — see ``connectors/rest`` module docstring.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    """In-memory, per-process, async-safe token bucket."""

    def __init__(self, rate: float, burst: int, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = int(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()
        self._lock = asyncio.Lock()

    async def consume(self, tokens: float = 1.0) -> bool:
        """Consume ``tokens`` (default one). Returns False when the bucket is low."""
        if tokens <= 0:
            raise ValueError("tokens must be > 0")
        async with self._lock:
            now = self._clock()
            self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True
