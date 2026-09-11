"""Tenant policy data model (M2, plan section 6-7).

`TenantState` is a first-class platform primitive, not a boolean flag --
plan section 7 lists five states, of which SUSPENDED and EMERGENCY_BLOCK
are the "Emergency Gate" BLOCK states: a request from a tenant in either
must never reach the model. THROTTLED reduces the tenant's effective rate
limit instead of blocking outright (see pipeline.enforce_rate_limit).
READ_ONLY is reserved for future write-type admin/data endpoints -- there
isn't yet a "read vs write" distinction on /v1/chat to apply it to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class TenantState(str, Enum):
    ACTIVE = "ACTIVE"
    THROTTLED = "THROTTLED"
    READ_ONLY = "READ_ONLY"
    SUSPENDED = "SUSPENDED"
    EMERGENCY_BLOCK = "EMERGENCY_BLOCK"


# The kill switch's BLOCK branch (plan section 7's "Emergency Gate").
BLOCKING_STATES = frozenset({TenantState.SUSPENDED, TenantState.EMERGENCY_BLOCK})


@dataclass(frozen=True)
class TenantSlo:
    p95_latency_ms: Optional[float] = None


@dataclass(frozen=True)
class TenantPolicy:
    tenant_id: str
    state: TenantState = TenantState.ACTIVE
    # Empty allowlist == "no restriction beyond the gateway default model".
    models: List[str] = field(default_factory=list)
    rpm_limit: int = 60
    guardrail_policy: str = "standard-v1"
    route_set: Optional[str] = None
    slo: TenantSlo = field(default_factory=TenantSlo)
    policy_epoch: int = 1
    # M3: only STRICT/STANDARD guardrail failures fail closed by default;
    # this lets a tenant explicitly opt a LOW_RISK guardrail policy into
    # degrading instead, rather than the gateway silently downgrading it.
    allow_guardrail_bypass_on_error: bool = False


class UnknownTenantError(Exception):
    def __init__(self, tenant_id: str):
        super().__init__(f"no policy configured for tenant_id={tenant_id!r}")
        self.tenant_id = tenant_id
