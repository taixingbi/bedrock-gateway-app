"""Audit trail for onboarding state transitions (M11, plan section
22.5). Every transition (submit/approve/reject/provision/activate/fail)
is one event: who, what, when, and enough context to reconstruct why --
never mutated after being written.

Keyed by (request_id, timestamp) rather than a synthetic event id: a
request's history is always read as one ordered sequence (the portal's
timeline view), never looked up by event id alone, so this is the
natural key -- same reasoning as usage/store.py keying by
(tenant_id, month) for its own always-queried-together access pattern.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    event: str  # e.g. "ONBOARDING_SUBMITTED", "APPLICATION_APPROVED"
    actor: str
    timestamp: float = field(default_factory=time.time)
    reason: Optional[str] = None


class AuditStore(Protocol):
    def record(self, event: AuditEvent) -> None: ...

    def list_for_request(self, request_id: str) -> List[AuditEvent]:
        """Oldest first -- a request's history in the order it happened."""
        ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._events: Dict[str, List[AuditEvent]] = {}
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.setdefault(event.request_id, []).append(event)

    def list_for_request(self, request_id: str) -> List[AuditEvent]:
        with self._lock:
            return sorted(self._events.get(request_id, []), key=lambda e: e.timestamp)


class DynamoDbAuditStore:
    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def record(self, event: AuditEvent) -> None:
        item: Dict[str, Any] = {
            "request_id": event.request_id,
            "timestamp": Decimal(str(event.timestamp)),
            "event": event.event,
            "actor": event.actor,
        }
        if event.reason is not None:
            item["reason"] = event.reason
        self._table.put_item(Item=item)

    def list_for_request(self, request_id: str) -> List[AuditEvent]:
        response = self._table.query(
            KeyConditionExpression="request_id = :rid",
            ExpressionAttributeValues={":rid": request_id},
            ScanIndexForward=True,  # oldest first
        )
        return [
            AuditEvent(
                request_id=item["request_id"],
                event=item["event"],
                actor=item["actor"],
                timestamp=float(item["timestamp"]),
                reason=item.get("reason"),
            )
            for item in response.get("Items", [])
        ]
