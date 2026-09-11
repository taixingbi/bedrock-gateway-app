"""Admin API (M2): tenant state control -- the kill switch's admin
surface (plan section 4's `PUT /v1/admin/tenants/{id}/state`).

Separate module from routes.py's public `/v1/*` endpoints since admin
actions need a different role (`ADMIN_REQUIRED_ROLE`, default
`platform_admin`) and will grow independently (quota/model/guardrail
updates in later milestones) without touching the chat pipeline.
"""
from __future__ import annotations

import uuid
from typing import List

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import pipeline
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..policy.cache import PolicySnapshotCache
from ..policy.models import TenantState, UnknownTenantError
from ..policy.store import MutablePolicyStore
from ..telemetry.logging import get_logger, log_event
from .errors import error_response as _error

_logger = get_logger("gateway.admin")


def build_admin_router(
    *,
    policy_store: MutablePolicyStore,
    policy_cache: PolicySnapshotCache,
    settings: Settings,
    token_verifier: TokenVerifier,
) -> List[Route]:
    async def set_tenant_state(request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = pipeline.authenticate(
                request.headers.get("authorization"), token_verifier=token_verifier
            )
            pipeline.authorize(identity, required_role=settings.admin_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        tenant_id = request.path_params["tenant_id"]

        try:
            body = await request.json()
        except Exception:
            return _error(400, "INVALID_JSON", "request body must be valid JSON", request_id)

        raw_state = body.get("state") if isinstance(body, dict) else None
        try:
            new_state = TenantState(raw_state)
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

    return [Route("/v1/admin/tenants/{tenant_id}/state", set_tenant_state, methods=["PUT"])]
