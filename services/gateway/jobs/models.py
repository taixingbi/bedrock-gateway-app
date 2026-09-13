"""Job record shape (M7, plan section 15) shared by the API (submit/read,
api/jobs_routes.py) and the worker (jobs/processor.py, services/worker).

Plain dataclasses, not Pydantic -- these cross a DynamoDB boundary, not
an HTTP one (api/schemas.py's JobRequest/JobStatusResponse are the
Pydantic side, translated to/from a Job at the route handlers).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class JobMessage:
    role: str
    content: str


@dataclass(frozen=True)
class Job:
    job_id: str
    tenant_id: str
    application_id: str
    status: JobStatus
    model: str
    messages: List[JobMessage]
    max_tokens: int
    temperature: float
    created_at: float  # epoch seconds, time.time()

    output: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None


class JobNotFoundError(Exception):
    def __init__(self, job_id: str):
        super().__init__(f"no job '{job_id}'")
        self.job_id = job_id
