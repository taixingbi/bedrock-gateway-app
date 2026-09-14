"""Application onboarding API (M11, plan section 22): submit -> approve
-> provision -> ACTIVE, all through HTTP rather than a platform admin
hand-editing DynamoDB or a policy YAML file. Separate module from
admin_routes.py since this grows its own approval-workflow surface
independently of tenant state control / usage reporting.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver, ProvisionedIamTenantResolver
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..onboarding.audit import AuditEvent, AuditStore
from ..onboarding.models import (
    AuthType,
    OnboardingRequest,
    OnboardingRequestNotFoundError,
    OnboardingStatus,
    new_request_id,
)
from ..onboarding.provisioning import ApplicationAlreadyExistsError, provision
from ..onboarding.store import OnboardingStore
from ..policy.store import PolicyStore, ProvisionedPolicyStore
from ..telemetry.logging import get_logger, log_event
from .errors import error_response as _error
from .schemas import OnboardingRequestBody, RejectOnboardingRequestBody

_logger = get_logger("gateway.onboarding")


def build_onboarding_router(
    *,
    onboarding_store: OnboardingStore,
    audit_store: AuditStore,
    policy_store: PolicyStore,
    policy_store_primary: ProvisionedPolicyStore,
    iam_tenant_resolver: IamTenantResolver,
    iam_tenant_resolver_primary: ProvisionedIamTenantResolver,
    settings: Settings,
    token_verifier: TokenVerifier,
) -> APIRouter:
    api_router = APIRouter()

    def _authenticate(request: Request, *, required_role: str):
        identity = pipeline.authenticate(
            request.headers.get("authorization"),
            token_verifier=token_verifier,
            iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
            iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
            iam_tenant_resolver=iam_tenant_resolver,
        )
        pipeline.authorize(identity, required_role=required_role)
        return identity

    @api_router.post("/v1/admin/onboarding-requests")
    async def submit(body: OnboardingRequestBody, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate(request, required_role=settings.chat_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            auth_type = AuthType(body.auth_type)
        except ValueError:
            valid = [t.value for t in AuthType]
            return _error(400, "INVALID_REQUEST", f"'auth_type' must be one of {valid}", request_id)

        if auth_type == AuthType.IAM and not body.principal_arn:
            return _error(400, "INVALID_REQUEST", "principal_arn is required when auth_type='iam'", request_id)

        onboarding_request = OnboardingRequest(
            request_id=new_request_id(),
            tenant_id=body.tenant_id,
            application_id=body.application_id,
            environment=body.environment,
            auth_type=auth_type,
            principal_arn=body.principal_arn,
            requested_models=body.requested_models,
            rpm_limit=body.rpm_limit,
            monthly_budget=body.monthly_budget,
            guardrail_policy=body.guardrail_policy,
            data_classification=body.data_classification,
            requested_by=identity.sub,
        )
        onboarding_store.put(onboarding_request)
        audit_store.record(
            AuditEvent(request_id=onboarding_request.request_id, event="ONBOARDING_SUBMITTED", actor=identity.sub)
        )

        log_event(
            _logger, "INFO", "onboarding request submitted",
            request_id=request_id, onboarding_request_id=onboarding_request.request_id,
            tenant_id=body.tenant_id, application_id=body.application_id, actor=identity.sub,
        )

        return JSONResponse(
            {"request_id": onboarding_request.request_id, "status": onboarding_request.status.value},
            status_code=201,
        )

    @api_router.get("/v1/admin/onboarding-requests")
    async def list_requests(request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            _authenticate(request, required_role=settings.admin_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        return JSONResponse({"requests": [_serialize(r) for r in onboarding_store.list_all()]})

    @api_router.get("/v1/admin/onboarding-requests/{onboarding_request_id}")
    async def get_request(onboarding_request_id: str, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            _authenticate(request, required_role=settings.admin_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            found = onboarding_store.get(onboarding_request_id)
        except OnboardingRequestNotFoundError as exc:
            return _error(404, exc.code, str(exc), request_id)

        history = [
            {"event": e.event, "actor": e.actor, "timestamp": e.timestamp, "reason": e.reason}
            for e in audit_store.list_for_request(onboarding_request_id)
        ]
        return JSONResponse({**_serialize(found), "history": history})

    @api_router.post("/v1/admin/onboarding-requests/{onboarding_request_id}/approve")
    async def approve(onboarding_request_id: str, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate(request, required_role=settings.admin_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            found = onboarding_store.get(onboarding_request_id)
        except OnboardingRequestNotFoundError as exc:
            return _error(404, exc.code, str(exc), request_id)

        if found.status != OnboardingStatus.PENDING_APPROVAL:
            return _error(
                409, "INVALID_ONBOARDING_TRANSITION",
                f"onboarding request '{onboarding_request_id}' is {found.status.value}, not PENDING_APPROVAL",
                request_id,
            )

        approved = _with_status(found, OnboardingStatus.APPROVED)
        onboarding_store.put(approved)
        audit_store.record(AuditEvent(request_id=onboarding_request_id, event="APPLICATION_APPROVED", actor=identity.sub))

        provisioning = _with_status(approved, OnboardingStatus.PROVISIONING)
        onboarding_store.put(provisioning)

        try:
            provision(
                found,
                policy_store=policy_store,
                policy_store_primary=policy_store_primary,
                iam_tenant_resolver_primary=iam_tenant_resolver_primary,
            )
        except ApplicationAlreadyExistsError as exc:
            failed = _with_status(provisioning, OnboardingStatus.FAILED, reason=str(exc))
            onboarding_store.put(failed)
            audit_store.record(
                AuditEvent(request_id=onboarding_request_id, event="PROVISIONING_FAILED", actor=identity.sub, reason=str(exc))
            )
            return _error(409, "PROVISIONING_FAILED", str(exc), request_id)

        active = _with_status(provisioning, OnboardingStatus.ACTIVE)
        onboarding_store.put(active)
        audit_store.record(AuditEvent(request_id=onboarding_request_id, event="APPLICATION_ACTIVATED", actor=identity.sub))

        log_event(
            _logger, "INFO", "onboarding request provisioned",
            request_id=request_id, onboarding_request_id=onboarding_request_id,
            tenant_id=found.tenant_id, application_id=found.application_id, actor=identity.sub,
        )

        return JSONResponse(_serialize(active))

    @api_router.post("/v1/admin/onboarding-requests/{onboarding_request_id}/reject")
    async def reject(onboarding_request_id: str, body: RejectOnboardingRequestBody, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate(request, required_role=settings.admin_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            found = onboarding_store.get(onboarding_request_id)
        except OnboardingRequestNotFoundError as exc:
            return _error(404, exc.code, str(exc), request_id)

        if found.status != OnboardingStatus.PENDING_APPROVAL:
            return _error(
                409, "INVALID_ONBOARDING_TRANSITION",
                f"onboarding request '{onboarding_request_id}' is {found.status.value}, not PENDING_APPROVAL",
                request_id,
            )

        rejected = _with_status(found, OnboardingStatus.REJECTED, reason=body.reason)
        onboarding_store.put(rejected)
        audit_store.record(
            AuditEvent(request_id=onboarding_request_id, event="APPLICATION_REJECTED", actor=identity.sub, reason=body.reason)
        )

        return JSONResponse(_serialize(rejected))

    return api_router


def _with_status(req: OnboardingRequest, status: OnboardingStatus, *, reason: "str | None" = None) -> OnboardingRequest:
    import dataclasses
    import time

    return dataclasses.replace(req, status=status, reason=reason, updated_at=time.time())


def _serialize(req: OnboardingRequest) -> dict:
    return {
        "request_id": req.request_id,
        "tenant_id": req.tenant_id,
        "application_id": req.application_id,
        "environment": req.environment,
        "auth_type": req.auth_type.value,
        "principal_arn": req.principal_arn,
        "requested_models": req.requested_models,
        "rpm_limit": req.rpm_limit,
        "monthly_budget": req.monthly_budget,
        "guardrail_policy": req.guardrail_policy,
        "data_classification": req.data_classification,
        "requested_by": req.requested_by,
        "status": req.status.value,
        "reason": req.reason,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
    }
