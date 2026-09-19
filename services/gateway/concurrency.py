"""Bounded thread offload + concurrency limiting for blocking boto3
calls made from inside async route handlers (plan section 16 -- a real,
live-confirmed gap: this deployment runs uvicorn with its default
single worker, so a synchronous Bedrock/ApplyGuardrail call made
directly from `async def chat(...)` blocks the *entire* process's event
loop, starving every other tenant's concurrent request, not just the
caller's own. `TokenBucketRateLimiter`/kill-switch/ABAC all gate
*admission*; none of them protect an already-admitted request from
being starved by another tenant's in-flight blocking call).

Two parts:
  - `BlockingCallRunner` -- offloads a synchronous call to a bounded
    ThreadPoolExecutor (not asyncio's unbounded default executor) with
    a total timeout.
  - `ConcurrencyLimiter` -- a fast-reject (not queue-and-wait) counting
    semaphore, global cap and per-tenant cap, checked before a request
    is admitted to the thread pool at all. Rejecting immediately at the
    gate is what keeps a saturated thread pool from becoming its own
    unbounded queue.

A timed-out call is NOT cancelled -- Python threads can't be forcibly
killed. `timeout_s` bounds how long the *caller* waits, not how long
the orphaned thread keeps running; the thread pool's own fixed size is
what actually prevents unbounded resource growth from a pile-up of
orphaned calls, not the timeout itself. Callers must still release the
concurrency slot they held even after a timeout (see routes.py's
`finally`), or a timed-out request would leak a permanently-held slot.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


class ConcurrencyLimiter:
    """threading.Lock, not asyncio.Lock -- acquire/release happen around
    calls that run in worker threads (via BlockingCallRunner), not just
    on the event loop thread, so this must be safe across real OS
    threads, not just concurrent coroutines."""

    def __init__(self, *, global_max: int, default_tenant_max: int):
        self._global_max = global_max
        self._default_tenant_max = default_tenant_max
        self._global_count = 0
        self._tenant_counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, tenant_id: str, *, tenant_max: Optional[int] = None) -> bool:
        """Non-blocking: returns False immediately (consuming nothing)
        rather than waiting, so a saturated limiter surfaces as a fast
        429 instead of queueing requests behind an already-slow backend."""
        limit = tenant_max if tenant_max is not None else self._default_tenant_max
        with self._lock:
            if self._global_count >= self._global_max:
                return False
            if self._tenant_counts.get(tenant_id, 0) >= limit:
                return False
            self._global_count += 1
            self._tenant_counts[tenant_id] = self._tenant_counts.get(tenant_id, 0) + 1
            return True

    def release(self, tenant_id: str) -> None:
        with self._lock:
            if self._global_count > 0:
                self._global_count -= 1
            remaining = self._tenant_counts.get(tenant_id, 0) - 1
            if remaining > 0:
                self._tenant_counts[tenant_id] = remaining
            else:
                self._tenant_counts.pop(tenant_id, None)

    def current_global_count(self) -> int:
        with self._lock:
            return self._global_count


class BlockingCallTimeoutError(Exception):
    pass


class BlockingCallRunner:
    def __init__(self, *, max_workers: int, default_timeout_s: float):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="blocking-call")
        self._default_timeout_s = default_timeout_s

    async def run(
        self, func: Callable[..., T], *args: Any, timeout_s: Optional[float] = None, **kwargs: Any
    ) -> T:
        loop = asyncio.get_running_loop()
        bound = partial(func, *args, **kwargs)
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, bound),
                timeout=timeout_s if timeout_s is not None else self._default_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise BlockingCallTimeoutError(
                f"blocking call exceeded {timeout_s if timeout_s is not None else self._default_timeout_s}s"
            ) from exc
