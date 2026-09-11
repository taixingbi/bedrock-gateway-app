"""Per-request SLO breach flag (M5, plan section 6/18).

Real p95 latency is a percentile over a *window* of requests -- that
aggregation belongs in the metrics backend (Prometheus/Grafana, plan
section 18), not computed request-by-request in the gateway process. What
the gateway *can* do per request is flag when that single request's
latency exceeded the tenant's configured p95 target, which is what this
does; it's a leading indicator ("this one was slow"), not a replacement
for real percentile aggregation downstream.
"""
from __future__ import annotations

from ..policy.models import TenantPolicy


def slo_breached(policy: TenantPolicy, latency_ms: float) -> bool:
    threshold = policy.slo.p95_latency_ms
    if threshold is None:
        return False
    return latency_ms > threshold
