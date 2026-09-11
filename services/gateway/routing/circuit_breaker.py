"""Per-model circuit breaker (M4, plan section 16).

States:
  CLOSED     -> calls pass through; consecutive failures are counted.
  OPEN       -> calls are rejected immediately (no call to the model)
                until reset_timeout_s has elapsed.
  HALF_OPEN  -> one trial call is let through; success closes the
                breaker, failure re-opens it and restarts the timeout.

In-memory only, per gateway process -- the same "real, swappable
interface, not provisioned infra" pattern as the policy cache and rate
limiter. A multi-instance deployment would want shared breaker state
(e.g. Redis); nothing here prevents swapping this class for one later.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass
class _ModelBreaker:
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._failure_threshold = failure_threshold
        self._reset_timeout_s = reset_timeout_s
        self._clock = clock
        self._breakers: Dict[str, _ModelBreaker] = {}
        self._lock = threading.Lock()

    def _get(self, model_id: str) -> _ModelBreaker:
        breaker = self._breakers.get(model_id)
        if breaker is None:
            breaker = _ModelBreaker()
            self._breakers[model_id] = breaker
        return breaker

    def allow(self, model_id: str) -> bool:
        """Returns True if a call to model_id may proceed right now. An
        OPEN breaker past its reset_timeout_s transitions to HALF_OPEN
        and allows exactly the one trial call that triggered the check."""
        with self._lock:
            breaker = self._get(model_id)
            if breaker.state == BreakerState.OPEN:
                if (self._clock() - breaker.opened_at) >= self._reset_timeout_s:
                    breaker.state = BreakerState.HALF_OPEN
                    return True
                return False
            return True

    def record_success(self, model_id: str) -> None:
        with self._lock:
            breaker = self._get(model_id)
            breaker.state = BreakerState.CLOSED
            breaker.consecutive_failures = 0

    def record_failure(self, model_id: str) -> None:
        with self._lock:
            breaker = self._get(model_id)
            breaker.consecutive_failures += 1
            if breaker.state == BreakerState.HALF_OPEN or breaker.consecutive_failures >= self._failure_threshold:
                breaker.state = BreakerState.OPEN
                breaker.opened_at = self._clock()

    def state_of(self, model_id: str) -> BreakerState:
        with self._lock:
            return self._get(model_id).state
