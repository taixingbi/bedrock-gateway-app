"""Admin API (M2): tenant state control -- the kill switch's admin
surface (plan section 4's `PUT /v1/admin/tenants/{id}/state`).

Separate module from routes.py's public `/v1/*` endpoints since admin
actions need a different role (`ADMIN_REQUIRED_ROLE`, default
`platform_admin`) and will grow independently (quota/model/guardrail
updates in later milestones) without touching the chat pipeline.
"""
from __future__ import annotations

import uuid
from typing import Dict, Optional, Set

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver
from ..auth.enterprise_groups import EnterpriseGroupResolver
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..onboarding.audit import AuditEvent, AuditStore
from ..policy.cache import PolicySnapshotCache
from ..policy.change_requests import (
    PolicyChangeNotFoundError,
    PolicyChangeRequest,
    PolicyChangeStatus,
    PolicyChangeStore,
    new_change_id,
)
from ..policy.models import (
    NoPriorPolicyVersionError,
    PolicyEpochConflictError,
    TenantState,
    UnknownTenantError,
)
from ..policy.store import MutablePolicyStore, ProvisionedPolicyStore
from ..policy.validation import PolicyValidationError, validate_policy_changes
from ..routing.router import RouteSet
from ..telemetry.logging import get_logger, log_event
from ..usage.store import UsageStore, current_day, current_month, trailing_days
from .errors import error_response as _error
from .schemas import (
    ProposePolicyChangeBody,
    RejectPolicyChangeBody,
    RollbackPolicyBody,
    SetTenantStateBody,
)

_logger = get_logger("gateway.admin")


def build_admin_router(
    *,
    policy_store: MutablePolicyStore,
    policy_cache: PolicySnapshotCache,
    settings: Settings,
    token_verifier: TokenVerifier,
    iam_tenant_resolver: IamTenantResolver,
    usage_store: UsageStore,
    route_sets: Dict[str, RouteSet],
    certified_model_ids: Set[str],
    policy_store_primary: ProvisionedPolicyStore,
    policy_change_store: PolicyChangeStore,
    audit_store: AuditStore,
    enterprise_group_resolver: Optional[EnterpriseGroupResolver] = None,
) -> APIRouter:
    api_router = APIRouter()

    def _authenticate_admin(request: Request):
        """Global-admin-only endpoints (route-sets, applications) --
        cross-tenant reference data by design, not something a
        tenant-scoped manager should see filtered or unfiltered
        (plan section 30.3)."""
        identity = pipeline.authenticate(
            request.headers.get("authorization"),
            token_verifier=token_verifier,
            iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
            iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
            iam_tenant_resolver=iam_tenant_resolver,
            enterprise_group_resolver=enterprise_group_resolver,
        )
        pipeline.authorize(identity, required_role=settings.admin_required_role)
        return identity

    def _authenticate_admin_or_manager(request: Request):
        """Tenant-owned-data endpoints (plan section 30): reachable by
        either a tenant-scoped manager or a global platform_admin.
        Callers still need their own tenant-ownership check
        (authorize_tenant_match) or result filtering on top of this --
        this only establishes identity + role tier, not which tenant's
        data they may see."""
        identity = pipeline.authenticate(
            request.headers.get("authorization"),
            token_verifier=token_verifier,
            iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
            iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
            iam_tenant_resolver=iam_tenant_resolver,
            enterprise_group_resolver=enterprise_group_resolver,
        )
        pipeline.authorize_any(
            identity, required_roles=[settings.admin_required_role, settings.manager_required_role]
        )
        return identity

    @api_router.put("/v1/admin/tenants/{tenant_id}/state")
    async def set_tenant_state(tenant_id: str, body: SetTenantStateBody, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
            pipeline.authorize_tenant_match(
                identity, tenant_id, override_role=settings.admin_required_role, action="tenant.state.write"
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            new_state = TenantState(body.state)
        except ValueError:
            valid = [s.value for s in TenantState]
            return _error(400, "INVALID_REQUEST", f"'state' must be one of {valid}", request_id)

        try:
            updated = policy_store.set_state(tenant_id, new_state)
        except UnknownTenantError as exc:
            return _error(404, "TENANT_NOT_FOUND", str(exc), request_id)

        # Push invalidation (plan section 8): the next request for this
        # tenant refetches immediately instead of serving a stale snapshot
        # for up to policy_cache_ttl_s.
        policy_cache.invalidate(tenant_id)

        log_event(
            _logger, "INFO", "tenant state changed",
            request_id=request_id, tenant_id=tenant_id, actor=identity.sub,
            new_state=new_state.value, policy_epoch=updated.policy_epoch,
        )

        return JSONResponse(
            {"tenant_id": tenant_id, "state": updated.state.value, "policy_epoch": updated.policy_epoch}
        )

    @api_router.get("/v1/admin/usage")
    async def get_usage(request: Request) -> JSONResponse:
        """M8 FinOps showback/chargeback (plan section 20): every known
        tenant's current-month spend against its monthly_budget (None ==
        unlimited, reported as null utilization rather than a divide by
        zero)."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        # A list endpoint has no single "resource tenant_id" to gate on
        # like set_tenant_state does -- a manager sees a filtered
        # result set instead of a 403 (plan section 30.3).
        tenant_ids = policy_store.list_tenant_ids()
        if not identity.has_role(settings.admin_required_role):
            tenant_ids = [t for t in tenant_ids if t == identity.tenant_id]

        month = current_month()
        tenants = []
        for tenant_id in tenant_ids:
            policy = policy_cache.get(tenant_id)
            spend = usage_store.get(tenant_id, month)
            utilization = (
                round(spend / policy.monthly_budget, 4)
                if policy.monthly_budget and policy.monthly_budget > 0
                else None
            )
            tenants.append(
                {
                    "tenant_id": tenant_id,
                    "month": month,
                    "spend": round(spend, 6),
                    "monthly_budget": policy.monthly_budget,
                    "utilization": utilization,
                }
            )

        return JSONResponse({"tenants": tenants})

    @api_router.get("/v1/admin/usage/anomalies")
    async def get_usage_anomalies(request: Request, threshold_multiplier: float = 3.0) -> JSONResponse:
        """Plan section 34.7: a HEURISTIC tripwire, not ML-based anomaly
        detection -- flags a tenant whose today's spend exceeds
        `threshold_multiplier` times its trailing-7-day average daily
        spend. Says so explicitly in the response body, not just this
        docstring, so it's never mistaken for more than it is. A
        tenant with no trailing spend at all (average == 0) is skipped
        rather than flagged -- any nonzero spend on a brand-new tenant
        would otherwise trivially divide-by-zero into "infinite
        anomaly," which isn't a meaningful signal."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        tenant_ids = policy_store.list_tenant_ids()
        if not identity.has_role(settings.admin_required_role):
            tenant_ids = [t for t in tenant_ids if t == identity.tenant_id]

        today = current_day()
        window = trailing_days(7)
        anomalies = []
        for tenant_id in tenant_ids:
            today_spend = usage_store.get(tenant_id, today)
            trailing_spend = [usage_store.get(tenant_id, day) for day in window]
            trailing_avg = sum(trailing_spend) / len(trailing_spend) if trailing_spend else 0.0
            if trailing_avg <= 0:
                continue
            ratio = today_spend / trailing_avg
            if ratio >= threshold_multiplier:
                anomalies.append(
                    {
                        "tenant_id": tenant_id,
                        "today_spend": round(today_spend, 6),
                        "trailing_7day_avg_daily_spend": round(trailing_avg, 6),
                        "ratio": round(ratio, 2),
                    }
                )

        return JSONResponse({
            "method": "heuristic",
            "note": (
                "Threshold tripwire (today's spend >= threshold_multiplier x trailing-7-day "
                "average), NOT ML-based anomaly detection -- plan section 34.7."
            ),
            "threshold_multiplier": threshold_multiplier,
            "anomalies": anomalies,
        })

    @api_router.get("/v1/admin/tenants")
    async def list_tenants(request: Request) -> JSONResponse:
        """M10 portal: full tenant policy listing (state, models,
        quota, budget, guardrail_policy, route_set) -- get_usage above
        only exposes spend/budget, this is the rest of TenantPolicy."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        tenant_ids = policy_store.list_tenant_ids()
        if not identity.has_role(settings.admin_required_role):
            tenant_ids = [t for t in tenant_ids if t == identity.tenant_id]

        tenants = []
        for tenant_id in tenant_ids:
            policy = policy_cache.get(tenant_id)
            tenants.append(
                {
                    "tenant_id": policy.tenant_id,
                    "state": policy.state.value,
                    "models": policy.models,
                    "rpm_limit": policy.rpm_limit,
                    "guardrail_policy": policy.guardrail_policy,
                    "route_set": policy.route_set,
                    "monthly_budget": policy.monthly_budget,
                    "policy_epoch": policy.policy_epoch,
                }
            )

        return JSONResponse({"tenants": tenants})

    @api_router.get("/v1/admin/route-sets")
    async def list_route_sets(request: Request) -> JSONResponse:
        """M10 portal: route_sets.yaml plus each model's certification
        status (M9) -- CertifiedRouter silently drops an uncertified
        fallback from routing at runtime, so surfacing that here is
        what lets an admin actually see why."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            _authenticate_admin(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        sets = []
        for name, route_set in route_sets.items():
            sets.append(
                {
                    "name": name,
                    "primary": route_set.primary,
                    "primary_certified": route_set.primary in certified_model_ids,
                    "fallbacks": [
                        {"model": m, "certified": m in certified_model_ids}
                        for m in route_set.fallbacks
                    ],
                }
            )

        return JSONResponse({"route_sets": sets, "certified_models": sorted(certified_model_ids)})

    @api_router.get("/v1/admin/applications")
    async def list_applications(request: Request) -> JSONResponse:
        """M10 portal: application grants configured on the AWS_IAM/
        SigV4 auth path (auth/aws_iam.py's IamTenantResolver). The JWT
        path has no equivalent registry -- any application_id embedded
        in a validly-signed token is accepted, there's nothing to list
        -- so this is necessarily a partial picture, labeled as such
        rather than presented as a complete application inventory."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            _authenticate_admin(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        applications = [
            {
                "principal_arn": arn,
                "tenant_id": grant.tenant_id,
                "application_id": grant.application_id,
                "roles": grant.roles,
            }
            for arn, grant in iam_tenant_resolver.list_grants().items()
        ]

        return JSONResponse({
            "applications": applications,
            "note": "AWS_IAM/SigV4 auth path only -- the JWT path has no application registry to list",
        })

    @api_router.post("/v1/admin/tenants/{tenant_id}/policy-changes")
    async def propose_policy_change(
        tenant_id: str, body: ProposePolicyChangeBody, request: Request
    ) -> JSONResponse:
        """Plan section 33: a manager (or admin) proposes an edit to an
        already-provisioned tenant's policy -- rpm_limit/models/budget/
        etc. -- without touching a YAML file or redeploying the gateway.
        Only reaches the *primary* (provisioned/DynamoDB) store: a
        file-managed tenant's policy changes still go through a YAML PR,
        same invariant onboarding/provisioning.py enforces for creation."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
            pipeline.authorize_tenant_match(
                identity, tenant_id, override_role=settings.admin_required_role,
                action="policy.change.propose",
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            validate_policy_changes(body.changes)
        except PolicyValidationError as exc:
            return _error(400, "INVALID_POLICY_CHANGE", str(exc), request_id)

        try:
            policy_store_primary.get(tenant_id)
        except UnknownTenantError as exc:
            return _error(404, "TENANT_NOT_FOUND", str(exc), request_id)

        change = PolicyChangeRequest(
            change_id=new_change_id(),
            tenant_id=tenant_id,
            changes=body.changes,
            base_policy_epoch=body.base_policy_epoch,
            requested_by=identity.sub,
        )
        policy_change_store.put(change)
        audit_store.record(
            AuditEvent(request_id=change.change_id, event="POLICY_CHANGE_PROPOSED", actor=identity.sub)
        )

        log_event(
            _logger, "INFO", "policy change proposed",
            request_id=request_id, change_id=change.change_id, tenant_id=tenant_id, actor=identity.sub,
        )

        return JSONResponse(_serialize_change(change), status_code=201)

    @api_router.get("/v1/admin/tenants/{tenant_id}/policy-changes")
    async def list_policy_changes(tenant_id: str, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
            pipeline.authorize_tenant_match(
                identity, tenant_id, override_role=settings.admin_required_role,
                action="policy.change.list",
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        changes = policy_change_store.list_for_tenant(tenant_id)
        return JSONResponse({"changes": [_serialize_change(c) for c in changes]})

    @api_router.post("/v1/admin/tenants/{tenant_id}/policy-changes/{change_id}/approve")
    async def approve_policy_change(tenant_id: str, change_id: str, request: Request) -> JSONResponse:
        """Admin-only, same role tier as onboarding's approve -- a
        manager may *propose* a change to their own tenant but never
        approve their own or anyone else's (separation of duties)."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            found = policy_change_store.get(change_id)
        except PolicyChangeNotFoundError as exc:
            return _error(404, exc.code, str(exc), request_id)
        if found.tenant_id != tenant_id:
            return _error(404, "POLICY_CHANGE_NOT_FOUND", f"policy change '{change_id}' not found", request_id)
        if found.status != PolicyChangeStatus.PENDING_APPROVAL:
            return _error(
                409, "INVALID_POLICY_CHANGE_TRANSITION",
                f"policy change '{change_id}' is {found.status.value}, not PENDING_APPROVAL",
                request_id,
            )

        try:
            updated_policy = policy_store_primary.apply_change(
                tenant_id, found.changes, expected_epoch=found.base_policy_epoch
            )
        except PolicyEpochConflictError as exc:
            return _error(409, "POLICY_EPOCH_CONFLICT", str(exc), request_id)

        applied = _with_change_status(found, PolicyChangeStatus.APPLIED, approved_by=identity.sub)
        policy_change_store.put(applied)
        policy_cache.invalidate(tenant_id)
        audit_store.record(
            AuditEvent(request_id=change_id, event="POLICY_CHANGE_APPLIED", actor=identity.sub)
        )

        log_event(
            _logger, "INFO", "policy change applied",
            request_id=request_id, change_id=change_id, tenant_id=tenant_id,
            actor=identity.sub, policy_epoch=updated_policy.policy_epoch,
        )

        return JSONResponse(_serialize_change(applied))

    @api_router.post("/v1/admin/tenants/{tenant_id}/policy-changes/{change_id}/reject")
    async def reject_policy_change(
        tenant_id: str, change_id: str, body: RejectPolicyChangeBody, request: Request
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            found = policy_change_store.get(change_id)
        except PolicyChangeNotFoundError as exc:
            return _error(404, exc.code, str(exc), request_id)
        if found.tenant_id != tenant_id:
            return _error(404, "POLICY_CHANGE_NOT_FOUND", f"policy change '{change_id}' not found", request_id)
        if found.status != PolicyChangeStatus.PENDING_APPROVAL:
            return _error(
                409, "INVALID_POLICY_CHANGE_TRANSITION",
                f"policy change '{change_id}' is {found.status.value}, not PENDING_APPROVAL",
                request_id,
            )

        rejected = _with_change_status(
            found, PolicyChangeStatus.REJECTED, approved_by=identity.sub, reason=body.reason
        )
        policy_change_store.put(rejected)
        audit_store.record(
            AuditEvent(request_id=change_id, event="POLICY_CHANGE_REJECTED", actor=identity.sub, reason=body.reason)
        )

        return JSONResponse(_serialize_change(rejected))

    @api_router.post("/v1/admin/tenants/{tenant_id}/rollback")
    async def rollback_policy(tenant_id: str, body: RollbackPolicyBody, request: Request) -> JSONResponse:
        """Admin-only: restores a prior policy version as a new forward
        epoch (plan section 33) -- never rewrites history in place."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin(request)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            updated = policy_store_primary.rollback(tenant_id, body.target_epoch)
        except UnknownTenantError as exc:
            return _error(404, "TENANT_NOT_FOUND", str(exc), request_id)
        except NoPriorPolicyVersionError as exc:
            return _error(404, "NO_PRIOR_POLICY_VERSION", str(exc), request_id)

        policy_cache.invalidate(tenant_id)
        audit_store.record(
            AuditEvent(
                request_id=f"rollback-{tenant_id}-{updated.policy_epoch}",
                event="POLICY_ROLLED_BACK", actor=identity.sub,
                reason=f"restored to epoch {body.target_epoch}",
            )
        )

        log_event(
            _logger, "INFO", "policy rolled back",
            request_id=request_id, tenant_id=tenant_id, actor=identity.sub,
            target_epoch=body.target_epoch, new_policy_epoch=updated.policy_epoch,
        )

        return JSONResponse({"tenant_id": tenant_id, "policy_epoch": updated.policy_epoch})

    @api_router.get("/v1/admin/tenants/{tenant_id}/policy-history")
    async def get_policy_history(tenant_id: str, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate_admin_or_manager(request)
            pipeline.authorize_tenant_match(
                identity, tenant_id, override_role=settings.admin_required_role,
                action="policy.history.read",
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            history = policy_store_primary.list_history(tenant_id)
        except UnknownTenantError as exc:
            return _error(404, "TENANT_NOT_FOUND", str(exc), request_id)

        return JSONResponse({
            "tenant_id": tenant_id,
            "history": [
                {
                    "policy_epoch": p.policy_epoch,
                    "state": p.state.value,
                    "models": p.models,
                    "rpm_limit": p.rpm_limit,
                    "guardrail_policy": p.guardrail_policy,
                    "route_set": p.route_set,
                    "monthly_budget": p.monthly_budget,
                    "max_concurrency": p.max_concurrency,
                }
                for p in history
            ],
        })

    return api_router


def _with_change_status(
    change: PolicyChangeRequest,
    status: PolicyChangeStatus,
    *,
    approved_by: str,
    reason: "str | None" = None,
) -> PolicyChangeRequest:
    import dataclasses
    import time

    return dataclasses.replace(change, status=status, approved_by=approved_by, reason=reason, updated_at=time.time())


def _serialize_change(change: PolicyChangeRequest) -> dict:
    return {
        "change_id": change.change_id,
        "tenant_id": change.tenant_id,
        "changes": change.changes,
        "base_policy_epoch": change.base_policy_epoch,
        "requested_by": change.requested_by,
        "status": change.status.value,
        "reason": change.reason,
        "approved_by": change.approved_by,
        "created_at": change.created_at,
        "updated_at": change.updated_at,
    }
