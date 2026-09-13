import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..jobs.queue import InMemoryJobQueue
from ..jobs.store import InMemoryJobStore
from ..main import create_app
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _client():
    settings = load_settings()
    fixture = get_auth_fixture()
    job_store = InMemoryJobStore()
    job_queue = InMemoryJobQueue()
    app = create_app(
        settings=settings,
        converse_client=FakeConverseClient(),
        token_verifier=fixture.verifier,
        job_store=job_store,
        job_queue=job_queue,
    )
    return TestClient(app), job_store, job_queue


def _auth_headers(**token_kwargs) -> dict:
    return auth_header(get_auth_fixture().token(**token_kwargs))


class JobsEndpointTests(unittest.TestCase):
    def test_submit_returns_202_and_enqueues_by_id(self):
        client, job_store, job_queue = _client()

        resp = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["status"], "QUEUED")
        self.assertTrue(body["job_id"])
        self.assertEqual(job_queue.sent, [body["job_id"]])

        stored = job_store.get(body["job_id"])
        self.assertEqual(stored.tenant_id, "finance")
        self.assertEqual(stored.application_id, "risk-chat")

    def test_get_returns_submitted_job_status(self):
        client, _, _ = _client()

        submit = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(),
        )
        job_id = submit.json()["job_id"]

        resp = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "QUEUED")

    def test_get_unknown_job_returns_404(self):
        client, _, _ = _client()

        resp = client.get("/v1/jobs/does-not-exist", headers=_auth_headers())

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "JOB_NOT_FOUND")

    def test_cannot_read_another_tenants_job(self):
        client, _, _ = _client()

        submit = client.post(
            "/v1/jobs",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(tenant_id="finance"),
        )
        job_id = submit.json()["job_id"]

        # sandbox has an empty model allowlist -- unrelated to this check,
        # just a second real tenant in policies/tenants.yaml to submit as.
        resp = client.get(f"/v1/jobs/{job_id}", headers=_auth_headers(tenant_id="sandbox"))

        self.assertEqual(resp.status_code, 404)

    def test_missing_auth_is_rejected_before_enqueueing(self):
        client, _, job_queue = _client()

        resp = client.post(
            "/v1/jobs", json={"messages": [{"role": "user", "content": "hi"}]}
        )

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(job_queue.sent, [])


if __name__ == "__main__":
    unittest.main()
