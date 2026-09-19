"""PDP-style authorization decisions (plan section 34.3).

`auth/rbac.py`'s `require_role`/`require_any_role`/
`require_tenant_match_or_role` are the *correct, tested* RBAC/ABAC
implementation (30+ existing tests exercise them, including a live-
verified exact error message in plan section 30) -- this module does
not replace them. It wraps them behind one entry point that returns a
structured `Decision` (allow/reason/policy_version/decision_id)
instead of raise-or-not, so a `decision_id` exists exactly once per
access check and can be threaded into the unified audit event (plan
section 34.4) rather than reconstructed ad hoc per log line.

`decide()` deliberately does NOT invent an action-name registry
("model.invoke" -> some handler looked up by string) -- every real
authorization check in this codebase today is one of three shapes
(single-role RBAC, any-of-roles RBAC, tenant-match-or-role ABAC), and
`decide()` takes the same parameters `pipeline.py`'s existing
`authorize`/`authorize_any`/`authorize_tenant_match` already take,
generalized into one function. `action` is a free-form label (e.g.
"tenant.state.write", "model.invoke") carried through into the
Decision purely for audit/log readability -- it does not select
different enforcement logic.

Exactly one of `required_role`, `required_roles`, `resource_tenant_id`
must be given; `decide()` calls straight into the matching rbac.py
function so the denial `reason` text is byte-identical to what those
functions have always raised (no message drift for anything already
documented as live-verified).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..auth import rbac
from ..auth.identity import AuthorizationError, Identity


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    decision_id: str
    action: str
    # AuthorizationError's own .code (currently always "FORBIDDEN" --
    # every rbac.py violation raises the same code today) -- carried
    # through rather than hardcoded so a future distinct code doesn't
    # need a second translation layer here. "ALLOWED" when allow=True;
    # there's no error code to report.
    code: str = "ALLOWED"
    # The resource's own version at decision time (e.g. a tenant's
    # TenantPolicy.policy_epoch) -- None when the caller didn't supply
    # one (e.g. a check that runs before any policy has been fetched).
    # Not invented when unavailable; see module docstring.
    policy_version: Optional[int] = None
    context: Dict[str, Any] = field(default_factory=dict)


def decide(
    identity: Identity,
    *,
    action: str,
    required_role: Optional[str] = None,
    required_roles: Optional[List[str]] = None,
    resource_tenant_id: Optional[str] = None,
    override_role: Optional[str] = None,
    policy_version: Optional[int] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Decision:
    provided = [
        name
        for name, value in (
            ("required_role", required_role),
            ("required_roles", required_roles),
            ("resource_tenant_id", resource_tenant_id),
        )
        if value is not None
    ]
    if len(provided) != 1:
        raise ValueError(
            "decide() requires exactly one of required_role, required_roles, "
            f"resource_tenant_id -- got {provided or 'none'}"
        )

    decision_id = str(uuid.uuid4())
    try:
        if required_role is not None:
            rbac.require_role(identity, required_role)
        elif required_roles is not None:
            rbac.require_any_role(identity, *required_roles)
        else:
            if override_role is None:
                raise ValueError("resource_tenant_id requires override_role")
            rbac.require_tenant_match_or_role(identity, resource_tenant_id, override_role=override_role)
    except AuthorizationError as exc:
        return Decision(
            allow=False, reason=str(exc), decision_id=decision_id, action=action, code=exc.code,
            policy_version=policy_version, context=context or {},
        )

    return Decision(
        allow=True, reason="allowed", decision_id=decision_id, action=action,
        policy_version=policy_version, context=context or {},
    )
