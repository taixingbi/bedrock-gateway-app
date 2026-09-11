"""Per-tenant token-bucket rate limiter (M2's "Rate Limit" pipeline stage,
plan section 2).

Scoped strictly per tenant_id so one tenant's burst cannot consume
another's budget -- the isolation invariant (plan section 1) applied to
request capacity, not just data. This is deliberately a single-process,
in-memory bucket (no Redis) for the same reason the response cache and
policy store are in-memory for now: it's a real, swappable interface
(nothing else depends on the implementation) rather than provisioned
infra. Full TPM/dollar-budget tracking is FinOps (M8) -- this is just
requests-per-minute, the cheapest thing that gives capacity isolation now.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class TokenBucketRateLimiter:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, tenant_id: str, *, rpm_limit: int) -> bool:
        """Returns True and consumes one token if tenant_id is within its
        rpm_limit budget; returns False (consuming nothing) otherwise."""
        capacity = float(max(rpm_limit, 0))
        refill_rate_per_s = capacity / 60.0
        now = self._clock()

        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None:
                bucket = _Bucket(tokens=capacity, last_refill=now)
                self._buckets[tenant_id] = bucket

            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_rate_per_s)
            bucket.last_refill = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False
