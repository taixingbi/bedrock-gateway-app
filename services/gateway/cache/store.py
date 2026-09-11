"""Policy-aware response cache (M4, plan section 12).

Only responses that already passed the output guardrail are ever written
here (see api/routes.py) -- never raw model output. `ResponseCache` is
the seam -- same pattern as `ConverseClient`/`PolicyStore`/
`GuardrailClient`; `InMemoryResponseCache` stands in for Redis, which is
where a multi-instance deployment would move this without touching
callers.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Tuple


@dataclass(frozen=True)
class CachedResponse:
    text: str
    stop_reason: Optional[str]
    input_tokens: int
    output_tokens: int
    model_id: str  # the model that actually produced this response


class ResponseCache(Protocol):
    def get(self, key: str) -> Optional[CachedResponse]: ...
    def set(self, key: str, value: CachedResponse) -> None: ...


class InMemoryResponseCache:
    def __init__(
        self,
        *,
        ttl_s: float = 60.0,
        max_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: "OrderedDict[str, Tuple[float, CachedResponse]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[CachedResponse]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            cached_at, value = entry
            if (self._clock() - cached_at) >= self._ttl_s:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return value

    def set(self, key: str, value: CachedResponse) -> None:
        with self._lock:
            self._entries[key] = (self._clock(), value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
