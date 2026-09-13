"""Job record storage (M7). `JobStore` is the seam -- same pattern as
`PolicyStore`/`ResponseCache`/`ConverseClient`. `InMemoryJobStore` is what
tests and any environment without JOBS_TABLE_NAME configured get;
`DynamoDbJobStore` is the real backend, wired in by main.py/worker/main.py
when settings.jobs_table_name is set.
"""
from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, Dict, Protocol

from .models import Job, JobMessage, JobNotFoundError, JobStatus


class JobStore(Protocol):
    def put(self, job: Job) -> None:
        """Create or fully overwrite a job record."""
        ...

    def get(self, job_id: str) -> Job:
        """Raises JobNotFoundError if job_id doesn't exist."""
        ...


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def put(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Job:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError:
                raise JobNotFoundError(job_id) from None


def _job_to_item(job: Job) -> Dict[str, Any]:
    # DynamoDB's Number type rejects Python float directly (boto3 raises
    # "Float types are not supported. Use Decimal types instead.") --
    # only temperature/created_at need this, max_tokens/usage_* are ints.
    item: Dict[str, Any] = {
        "job_id": job.job_id,
        "tenant_id": job.tenant_id,
        "application_id": job.application_id,
        "status": job.status.value,
        "model": job.model,
        "messages": [{"role": m.role, "content": m.content} for m in job.messages],
        "max_tokens": job.max_tokens,
        "temperature": Decimal(str(job.temperature)),
        "created_at": Decimal(str(job.created_at)),
    }
    for key, value in (
        ("output", job.output),
        ("error_code", job.error_code),
        ("error_message", job.error_message),
        ("usage_input_tokens", job.usage_input_tokens),
        ("usage_output_tokens", job.usage_output_tokens),
    ):
        if value is not None:
            item[key] = value
    return item


def _item_to_job(item: Dict[str, Any]) -> Job:
    return Job(
        job_id=item["job_id"],
        tenant_id=item["tenant_id"],
        application_id=item["application_id"],
        status=JobStatus(item["status"]),
        model=item["model"],
        messages=[JobMessage(role=m["role"], content=m["content"]) for m in item.get("messages", [])],
        max_tokens=int(item["max_tokens"]),
        temperature=float(item["temperature"]),
        created_at=float(item["created_at"]),
        output=item.get("output"),
        error_code=item.get("error_code"),
        error_message=item.get("error_message"),
        usage_input_tokens=int(item["usage_input_tokens"]) if "usage_input_tokens" in item else None,
        usage_output_tokens=int(item["usage_output_tokens"]) if "usage_output_tokens" in item else None,
    )


class DynamoDbJobStore:
    """Real DynamoDB-backed JobStore. boto3 imported lazily (only when
    actually constructed) so this module stays importable without boto3
    installed -- same reasoning as inference/bedrock_client.py's
    BedrockClient."""

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, job: Job) -> None:
        self._table.put_item(Item=_job_to_item(job))

    def get(self, job_id: str) -> Job:
        response = self._table.get_item(Key={"job_id": job_id})
        item = response.get("Item")
        if item is None:
            raise JobNotFoundError(job_id)
        return _item_to_job(item)
