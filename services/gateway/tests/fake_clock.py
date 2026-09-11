"""A controllable clock for deterministic TTL/rate-limit tests -- no
real time.sleep() needed."""
from __future__ import annotations


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta
