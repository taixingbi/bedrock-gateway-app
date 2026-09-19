"""HTTP handlers for async jobs (M7, plan section 15): POST /v1/jobs,
GET /v1/jobs/{job_id}.

Interactive /v1/chat blocks on Bedrock; jobs let a caller submit work and
poll for the result later via SQS + a worker process (services/worker),
without holding an HTTP connection open. Submission runs the exact same
auth/policy/rate-limit/model-allowlist/input-guardrail stages as
/v1/chat (pipeline.py) -- a rejected job never reaches the queue -- but
the model call itself happens out of process (jobs/processor.py), so
this module never touches CertifiedRouter/BedrockClient at all.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional, Set

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver
from ..auth.enterprise_groups import EnterpriseGroupResolver
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..guardrails.client import GuardrailClient
from ..jobs.models import Job, JobMessage, JobNotFoundError, JobStatus
from ..jobs.queue import JobQueue
from ..jobs.store import JobStore
from ..policy.cache import PolicySnapshotCache
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..telemetry.logging import get_logger, log_event
from ..usage.store import UsageStore, current_month
from .errors import error_response as _error
from .schemas import JobRequest, JobResponse, JobStatusResponse, Usage

_logger = get_logger("gateway.jobs")


def build_jobs_router(
    *,
    settings: Settings,
    token_verifier: TokenVerifier,
    iam_tenant_resolver: IamTenantResolver,
    policy_cache: PolicySnapshotCache,
    rate_limiter: TokenBucketRateLimiter,
    guardrail_client: GuardrailClient,
    job_store: JobStore,
    job_queue: JobQueue,
    usage_store: UsageStore,
    certified_model_ids: Set[str],
    enterprise_group_resolver: Optional[EnterpriseGroupResolver] = None,
) -> APIRouter:
    api_router = APIRouter()

    def _authenticate(request: Request):
        return pipeline.authenticate(
            request.headers.get("authorization"),
            token_verifier=token_verifier,
            iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
            iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
            iam_tenant_resolver=iam_tenant_resolver,
            enterprise_group_resolver=enterprise_group_resolver,
        )

    @api_router.post("/v1/jobs", status_code=202, response_model=JobResponse)
    async def submit_job(request: Request, job_request: JobRequest) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate(request)
            pipeline.authorize(identity, required_role=settings.chat_required_role)
            policy = pipeline.resolve_policy(identity, policy_cache=policy_cache)
            pipeline.enforce_kill_switch(policy)
            pipeline.enforce_rate_limit(policy, rate_limiter=rate_limiter)
            pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            model_id = pipeline.enforce_model_allowlist(
                policy, requested_model=job_request.model, default_model=settings.bedrock_model_id
            )
            pipeline.enforce_model_certification(model_id, certified_model_ids=certified_model_ids)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        combined_input_text = "\n".join(m.content for m in job_request.messages)
        try:
            pipeline.check_input_guardrail(
                combined_input_text, policy=policy, guardrail_client=guardrail_client
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        job_id = str(uuid.uuid4())
        job = Job(
            job_id=job_id,
            tenant_id=identity.tenant_id,
            application_id=identity.application_id,
            status=JobStatus.QUEUED,
            model=model_id,
            messages=[JobMessage(role=m.role, content=m.content) for m in job_request.messages],
            max_tokens=job_request.max_tokens,
            temperature=job_request.temperature,
            created_at=time.time(),
        )
        # Store before enqueueing: the worker looks the job up by id from
        # the message it receives, so the record must already exist by
        # the time any consumer could possibly see it on the queue.
        job_store.put(job)
        job_queue.send(job_id)

        log_event(
            _logger, "INFO", "job submitted",
            request_id=request_id, job_id=job_id, tenant_id=identity.tenant_id,
            application_id=identity.application_id, model=model_id, policy_epoch=policy.policy_epoch,
        )

        return JSONResponse(
            JobResponse(job_id=job_id, status=JobStatus.QUEUED.value).model_dump(), status_code=202
        )

    @api_router.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_job(job_id: str, request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = _authenticate(request)
            pipeline.authorize(identity, required_role=settings.chat_required_role)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            job = job_store.get(job_id)
        except JobNotFoundError:
            return _error(404, "JOB_NOT_FOUND", f"no job '{job_id}'", request_id)

        # Isolation invariant (plan section 1): a tenant must not be able
        # to tell that another tenant's job even exists -- 404, not 403.
        if job.tenant_id != identity.tenant_id:
            return _error(404, "JOB_NOT_FOUND", f"no job '{job_id}'", request_id)

        usage = None
        if job.usage_input_tokens is not None and job.usage_output_tokens is not None:
            usage = Usage(input_tokens=job.usage_input_tokens, output_tokens=job.usage_output_tokens)

        return JSONResponse(
            JobStatusResponse(
                job_id=job.job_id,
                status=job.status.value,
                model=job.model,
                output=job.output,
                usage=usage,
                error_code=job.error_code,
                error_message=job.error_message,
            ).model_dump()
        )

    return api_router
