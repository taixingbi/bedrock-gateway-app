"""Bounded-TTL, push-invalidated policy snapshot cache (M2, plan section 8).

Two mechanisms combine:

- push invalidation: `invalidate(tenant_id)` (called by the admin
  set-tenant-state endpoint) evicts the cached snapshot immediately --
  this is what gives low propagation latency in the common case.
- bounded TTL/lease: even without a push, a snapshot older than `ttl_s`
  is treated as expired and refetched from the PolicyStore.

Together these give: "maximum stale-policy lifetime <= ttl_s", which
holds even if a push is lost, dropped, or never sent. This is also why
the data plane never calls the control plane synchronously per request
(plan section 9) -- `get()` only reaches the underlying PolicyStore on a
cache miss/expiry, not on every call.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict

from .models import TenantPolicy
from .store import PolicyStore


@dataclass
class _Snapshot:
    policy: TenantPolicy
    cached_at: float


class PolicySnapshotCache:
    def __init__(
        self,
        *,
        store: PolicyStore,
        ttl_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._store = store
        self._ttl_s = ttl_s
        self._clock = clock
        self._snapshots: Dict[str, _Snapshot] = {}
        self._lock = threading.Lock()

    def get(self, tenant_id: str) -> TenantPolicy:
        with self._lock:
            snapshot = self._snapshots.get(tenant_id)
            if snapshot is not None and (self._clock() - snapshot.cached_at) < self._ttl_s:
                return snapshot.policy

        # Miss (never cached, invalidated, or TTL-expired) -- refetch.
        # Deliberately outside the lock: PolicyStore.get() may do file/
        # network I/O and shouldn't block other tenants' cache hits.
        policy = self._store.get(tenant_id)
        with self._lock:
            self._snapshots[tenant_id] = _Snapshot(policy=policy, cached_at=self._clock())
        return policy

    def invalidate(self, tenant_id: str) -> None:
        """Push invalidation: evict immediately so the next get() refetches
        rather than waiting up to ttl_s."""
        with self._lock:
            self._snapshots.pop(tenant_id, None)
