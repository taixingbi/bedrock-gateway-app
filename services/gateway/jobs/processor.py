"""Async job execution (M7) -- the worker-side half of the request
pipeline. A pure function (no SQS client, no polling loop) so it's
unit-testable the same way pipeline.py's stages are; services/worker/
main.py is the thin adapter that actually long-polls SQS and calls this.

Submission (api/jobs_routes.py) already ran auth/authz/kill-switch/
rate-limit/model-allowlist/input-guardrail before the job record was
even written -- a rejected submission never reaches this module.
process_one() re-checks the kill switch against the *current* policy
snapshot (not the one in effect at submission time), since a tenant can
be suspended in the gap between a job being queued and a worker picking
it up (Policy Invariant, plan section 1). Nothing else the pipeline
enforces at submission is re-checked here -- rate limiting and the input
guardrail already ran once and aren't meaningful to re-run against a
queued job.
"""
from __future__ import annotations

import dataclasses
import json

from .. import pipeline
from ..guardrails.client import GuardrailClient
from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError
from ..policy.cache import PolicySnapshotCache
from ..policy.models import BLOCKING_STATES
from ..routing.router import AllRoutesUnavailableError, CertifiedRouter
from ..telemetry.cost import estimate_cost
from ..telemetry.logging import get_logger, log_event
from ..usage.store import UsageStore, current_month
from .models import JobNotFoundError, JobStatus
from .store import JobStore

_logger = get_logger("gateway.worker")


def process_one(
    message_body: str,
    *,
    job_store: JobStore,
    policy_cache: PolicySnapshotCache,
    guardrail_client: GuardrailClient,
    router: CertifiedRouter,
    usage_store: UsageStore,
) -> None:
    job_id = json.loads(message_body)["job_id"]

    try:
        job = job_store.get(job_id)
    except JobNotFoundError:
        log_event(_logger, "ERROR", "job record missing for queued message", job_id=job_id)
        return

    if job.status != JobStatus.QUEUED:
        # SQS is at-least-once -- a redelivered message for an already
        # SUCCEEDED/FAILED job must not re-run it.
        return

    policy = policy_cache.get(job.tenant_id)
    if policy.state in BLOCKING_STATES:
        job_store.put(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code="TENANT_BLOCKED",
            error_message=f"tenant '{job.tenant_id}' is {policy.state.value}",
        ))
        return

    job_store.put(dataclasses.replace(job, status=JobStatus.RUNNING))

    messages = [BedrockChatMessage(role=m.role, text=m.content) for m in job.messages]
    try:
        routed = router.converse(
            primary_model_id=job.model,
            route_set_name=policy.route_set,
            messages=messages,
            max_tokens=job.max_tokens,
            temperature=job.temperature,
        )
    except BedrockInvocationError as exc:
        job_store.put(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code=exc.code, error_message=str(exc),
        ))
        return
    except AllRoutesUnavailableError as exc:
        job_store.put(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code="ALL_ROUTES_UNAVAILABLE", error_message=str(exc),
        ))
        return

    result = routed.result
    try:
        pipeline.check_output_guardrail(result.text, policy=policy, guardrail_client=guardrail_client)
    except pipeline.PipelineError as exc:
        job_store.put(dataclasses.replace(
            job, status=JobStatus.FAILED, error_code=exc.code, error_message=str(exc),
        ))
        return

    job_store.put(dataclasses.replace(
        job,
        status=JobStatus.SUCCEEDED,
        output=result.text,
        usage_input_tokens=result.input_tokens,
        usage_output_tokens=result.output_tokens,
    ))
    estimated_cost = estimate_cost(
        routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens
    )
    usage_store.add_and_get(job.tenant_id, current_month(), estimated_cost)
    log_event(
        _logger, "INFO", "job completed",
        job_id=job_id, tenant_id=job.tenant_id, model=routed.model_id, status=JobStatus.SUCCEEDED.value,
    )
