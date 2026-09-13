"""Entrypoint for the async job worker (M7, plan section 15).

Long-polls SQS, hands each message to jobs/processor.py's process_one()
(the actual "do the job" logic, kept there so it's unit-testable without
a real SQS client), and deletes the message only once processing
returned without raising -- both SUCCEEDED and a handled FAILED are
terminal states that should be deleted; only an unexpected exception
leaves a message for redelivery/eventual DLQ (bedrock-gateway-infra's
jobs_dlq, maxReceiveCount=3).

Same container image as gateway-api (see bedrock-gateway-infra's
modules/worker_service) -- this is the same codebase, just run with a
different command:
    python -m services.worker.main
"""
from __future__ import annotations

from typing import Any, Callable, Dict

from ..gateway.config import load_settings
from ..gateway.guardrails.basic_guardrail import BasicGuardrailClient
from ..gateway.inference.bedrock_client import BedrockClient
from ..gateway.jobs.processor import process_one
from ..gateway.jobs.store import DynamoDbJobStore
from ..gateway.policy.cache import PolicySnapshotCache
from ..gateway.policy.store import FilePolicyStore
from ..gateway.routing.circuit_breaker import CircuitBreaker
from ..gateway.routing.router import CertifiedRouter, load_route_sets_from_yaml
from ..gateway.telemetry.logging import configure_logging, get_logger, log_event
from ..gateway.usage.store import DynamoDbUsageStore, InMemoryUsageStore

_logger = get_logger("gateway.worker.main")


def run_forever(
    *,
    sqs_client: Any,
    queue_url: str,
    process: Callable[..., None] = process_one,
    should_continue: Callable[[], bool] = lambda: True,
    **process_kwargs: Dict[str, Any],
) -> None:
    while should_continue():
        response = sqs_client.receive_message(
            QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=20
        )
        for message in response.get("Messages", []):
            try:
                process(message["Body"], **process_kwargs)
            except Exception as exc:  # noqa: BLE001 - leave message for redelivery, keep polling
                log_event(
                    _logger, "ERROR", "job processing raised, leaving message for redelivery",
                    error=str(exc),
                )
                continue
            sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def main() -> None:
    settings = load_settings()
    configure_logging(settings.service_name, settings.log_level)

    if not settings.jobs_queue_url or not settings.jobs_table_name:
        raise RuntimeError(
            "JOBS_QUEUE_URL and JOBS_TABLE_NAME must both be set to run the worker "
            "(see bedrock-gateway-infra's modules/worker_service container_env)"
        )

    import boto3

    job_store = DynamoDbJobStore(table_name=settings.jobs_table_name, region=settings.aws_region)
    policy_cache = PolicySnapshotCache(
        store=FilePolicyStore(settings.tenant_policy_path), ttl_s=settings.policy_cache_ttl_s
    )
    guardrail_client = BasicGuardrailClient()
    converse_client = BedrockClient(
        region=settings.aws_region,
        timeout_s=settings.bedrock_timeout_s,
        max_retries=settings.bedrock_max_retries,
    )
    circuit_breaker = CircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        reset_timeout_s=settings.circuit_breaker_reset_timeout_s,
    )
    router = CertifiedRouter(
        converse_client=converse_client,
        circuit_breaker=circuit_breaker,
        route_sets=load_route_sets_from_yaml(settings.route_set_config_path),
    )
    usage_store = (
        DynamoDbUsageStore(table_name=settings.usage_table_name, region=settings.aws_region)
        if settings.usage_table_name
        else InMemoryUsageStore()
    )
    sqs_client = boto3.client("sqs", region_name=settings.aws_region)

    log_event(_logger, "INFO", "worker started", queue_url=settings.jobs_queue_url)
    run_forever(
        sqs_client=sqs_client,
        queue_url=settings.jobs_queue_url,
        job_store=job_store,
        policy_cache=policy_cache,
        guardrail_client=guardrail_client,
        router=router,
        usage_store=usage_store,
    )


if __name__ == "__main__":
    main()
