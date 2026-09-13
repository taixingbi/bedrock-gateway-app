import time
import unittest

from ..guardrails.basic_guardrail import BasicGuardrailClient
from ..inference.bedrock_client import BedrockInvocationError
from ..jobs.models import Job, JobMessage, JobStatus
from ..jobs.processor import process_one
from ..jobs.store import InMemoryJobStore
from ..policy.cache import PolicySnapshotCache
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.router import CertifiedRouter
from .fakes import FakeConverseClient


def _job(**overrides) -> Job:
    defaults = dict(
        job_id="job-1",
        tenant_id="finance",
        application_id="risk-chat",
        status=JobStatus.QUEUED,
        model="us.amazon.nova-micro-v1:0",
        messages=[JobMessage(role="user", content="hello")],
        max_tokens=256,
        temperature=0.2,
        created_at=time.time(),
    )
    defaults.update(overrides)
    return Job(**defaults)


def _policy_cache(**policies: TenantPolicy) -> PolicySnapshotCache:
    return PolicySnapshotCache(store=InMemoryPolicyStore(policies), ttl_s=30.0)


def _router(fake: FakeConverseClient) -> CertifiedRouter:
    return CertifiedRouter(
        converse_client=fake,
        circuit_breaker=CircuitBreaker(failure_threshold=5, reset_timeout_s=30.0),
        route_sets={},
    )


class ProcessOneTests(unittest.TestCase):
    def test_success_marks_job_succeeded_with_output(self):
        job_store = InMemoryJobStore()
        job = _job()
        job_store.put(job)
        fake = FakeConverseClient(response_text="hi there", input_tokens=5, output_tokens=3)

        process_one(
            f'{{"job_id": "{job.job_id}"}}',
            job_store=job_store,
            policy_cache=_policy_cache(finance=TenantPolicy(tenant_id="finance")),
            guardrail_client=BasicGuardrailClient(),
            router=_router(fake),
        )

        updated = job_store.get(job.job_id)
        self.assertEqual(updated.status, JobStatus.SUCCEEDED)
        self.assertEqual(updated.output, "hi there")
        self.assertEqual(updated.usage_input_tokens, 5)
        self.assertEqual(updated.usage_output_tokens, 3)

    def test_suspended_tenant_fails_closed_without_calling_bedrock(self):
        job_store = InMemoryJobStore()
        job = _job()
        job_store.put(job)
        fake = FakeConverseClient()

        process_one(
            f'{{"job_id": "{job.job_id}"}}',
            job_store=job_store,
            policy_cache=_policy_cache(
                finance=TenantPolicy(tenant_id="finance", state=TenantState.SUSPENDED)
            ),
            guardrail_client=BasicGuardrailClient(),
            router=_router(fake),
        )

        updated = job_store.get(job.job_id)
        self.assertEqual(updated.status, JobStatus.FAILED)
        self.assertEqual(updated.error_code, "TENANT_BLOCKED")
        self.assertEqual(fake.calls, [])

    def test_bedrock_failure_marks_job_failed(self):
        job_store = InMemoryJobStore()
        job = _job()
        job_store.put(job)
        fake = FakeConverseClient(
            error=BedrockInvocationError("boom", code="ThrottlingException", retryable=True)
        )

        process_one(
            f'{{"job_id": "{job.job_id}"}}',
            job_store=job_store,
            policy_cache=_policy_cache(finance=TenantPolicy(tenant_id="finance")),
            guardrail_client=BasicGuardrailClient(),
            router=_router(fake),
        )

        updated = job_store.get(job.job_id)
        self.assertEqual(updated.status, JobStatus.FAILED)
        self.assertEqual(updated.error_code, "ThrottlingException")

    def test_already_processed_job_is_not_rerun(self):
        job_store = InMemoryJobStore()
        job = _job(status=JobStatus.SUCCEEDED, output="already done")
        job_store.put(job)
        fake = FakeConverseClient()

        process_one(
            f'{{"job_id": "{job.job_id}"}}',
            job_store=job_store,
            policy_cache=_policy_cache(finance=TenantPolicy(tenant_id="finance")),
            guardrail_client=BasicGuardrailClient(),
            router=_router(fake),
        )

        self.assertEqual(fake.calls, [])
        self.assertEqual(job_store.get(job.job_id).output, "already done")


if __name__ == "__main__":
    unittest.main()
