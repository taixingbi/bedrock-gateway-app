"""Application onboarding request domain model (M11, plan section 22).

An OnboardingRequest is what a team submits to get a new application
provisioned -- a principal mapping (AWS_IAM path) and, if its tenant_id
doesn't already exist, a new TenantPolicy. Deliberately *not* a general
tenant-config editor: an existing hand-managed tenant's policy is never
silently mutated by a request (see onboarding/provisioning.py).

MVP status machine (plan section 22.2, collapsed for a single-level
approval flow -- no DRAFT/SUBMITTED distinction, no multi-stage
approval chain):

    PENDING_APPROVAL -> APPROVED -> PROVISIONING -> ACTIVE
                      \\-> REJECTED         \\-> FAILED

ACTIVE/decommission afterwards reuses the kill switch that already
exists (TenantState.SUSPENDED, plan section 7) rather than a second
lifecycle -- an onboarded tenant is governed the same way a
hand-configured one already is once it exists.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class OnboardingStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class AuthType(str, Enum):
    IAM = "iam"
    # OAuth-client (JWT-path) applications authenticate against an
    # external IdP the platform doesn't provision app clients for
    # itself yet (see plan section 22.6's MVP scope) -- recognized so a
    # request can at least record the intent, but provisioning.py
    # raises rather than silently doing nothing for one of these.
    OAUTH = "oauth"


class OnboardingError(Exception):
    """Raised for an invalid state transition or an unknown request_id."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class OnboardingRequestNotFoundError(OnboardingError):
    def __init__(self, request_id: str):
        super().__init__(f"onboarding request '{request_id}' not found", code="ONBOARDING_REQUEST_NOT_FOUND")
        self.request_id = request_id


class InvalidTransitionError(OnboardingError):
    def __init__(self, request_id: str, *, from_status: OnboardingStatus, action: str):
        super().__init__(
            f"onboarding request '{request_id}' cannot be {action} from status {from_status.value}",
            code="INVALID_ONBOARDING_TRANSITION",
        )


@dataclass(frozen=True)
class OnboardingRequest:
    request_id: str
    tenant_id: str
    application_id: str
    environment: str
    auth_type: AuthType
    principal_arn: Optional[str]  # required when auth_type == IAM
    requested_models: List[str] = field(default_factory=list)
    rpm_limit: int = 60
    monthly_budget: Optional[float] = None
    guardrail_policy: str = "standard-v1"
    data_classification: str = "internal"
    requested_by: str = ""
    status: OnboardingStatus = OnboardingStatus.PENDING_APPROVAL
    reason: Optional[str] = None  # rejection/failure reason
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"
