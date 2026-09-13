"""Async job queue (M7). `JobQueue` is the seam -- same pattern as
`JobStore`/`ResponseCache`. The queue only ever carries a job_id: the
actual job (messages, model, tenant_id, ...) is already durably written
to the JobStore by the submitting request before this is called, so the
message body stays tiny and there's exactly one place (the store) that
holds the job's real content.
"""
from __future__ import annotations

import json
from typing import List, Protocol


class JobQueue(Protocol):
    def send(self, job_id: str) -> None: ...


class InMemoryJobQueue:
    """Stands in for SQS in tests -- records sends, delivers nothing (the
    worker side is tested directly against jobs/processor.py instead of
    through a real queue)."""

    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, job_id: str) -> None:
        self.sent.append(job_id)


class SqsJobQueue:
    """Real SQS-backed JobQueue. boto3 imported lazily, same reasoning as
    DynamoDbJobStore/BedrockClient."""

    def __init__(self, *, queue_url: str, region: str):
        import boto3

        self._client = boto3.client("sqs", region_name=region)
        self._queue_url = queue_url

    def send(self, job_id: str) -> None:
        self._client.send_message(QueueUrl=self._queue_url, MessageBody=json.dumps({"job_id": job_id}))
