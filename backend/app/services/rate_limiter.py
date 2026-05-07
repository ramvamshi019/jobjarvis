"""Per-domain async rate limiter with token bucket algorithm."""
import asyncio
import time
from collections import defaultdict
from typing import Dict
import structlog

logger = structlog.get_logger(__name__)


class DomainBucket:
    def __init__(self, rps: float = 1.0):
        self.rps = rps
        self.tokens = 1.0
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(1.0, self.tokens + elapsed * self.rps)
            self.last_update = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            wait = (1.0 - self.tokens) / self.rps
            await asyncio.sleep(wait)
            self.tokens = 0.0


class RateLimiter:
    """Global singleton rate limiter keyed by domain."""
    _buckets: Dict[str, DomainBucket] = defaultdict(lambda: DomainBucket(rps=1.0))
    _domain_rps: Dict[str, float] = {}

    def set_domain_rps(self, domain: str, rps: float):
        self._domain_rps[domain] = rps
        self._buckets[domain] = DomainBucket(rps=rps)

    async def acquire(self, domain: str):
        if domain not in self._buckets:
            rps = self._domain_rps.get(domain, 1.0)
            self._buckets[domain] = DomainBucket(rps=rps)
        await self._buckets[domain].acquire()

    def reset(self, domain: str):
        if domain in self._buckets:
            del self._buckets[domain]
