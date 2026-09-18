"""Admin API (M2): tenant state control -- the kill switch's admin
surface (plan section 4's `PUT /v1/admin/tenants/{id}/state`).

Separate module from routes.py's public `/v1/*` endpoints since admin
actions need a different role (`ADMIN_REQUIRED_ROLE`, default
`platform_admin`) and will grow independently (quota/model/guardrail
updates in later milestones) without touching the chat pipeline.
"""
from __future__ import annotations

import uuid
from typing import Dict, Set

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..policy.cache import PolicySnapshotCache
from ..policy.models import TenantState, UnknownTenantError
from ..policy.store import MutablePolicyStore
from ..routing.router import RouteSet
from ..telemetry.logging import get_logger, log_event
from ..usage.store import UsageStore, current_month
from .errors import error_response as _error
from .schemas import SetTenantStateBody

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
            pipeline.authorize_tenant_match(identity, tenant_id, override_role=settings.admin_required_role)
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

    return api_router
