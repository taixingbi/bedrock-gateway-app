import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from .auth_fixtures import get_auth_fixture
from .fakes import FakeConverseClient


class HealthzTests(unittest.TestCase):
    def setUp(self):
        settings = load_settings()
        self.app = create_app(
            settings=settings,
            converse_client=FakeConverseClient(),
            token_verifier=get_auth_fixture().verifier,
        )
        self.client = TestClient(self.app)

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_request_id_header_present(self):
        resp = self.client.get("/healthz")
        self.assertIn("x-request-id", resp.headers)

    def test_successful_healthz_is_not_logged(self):
        """Polled every 10-15s by the ALB and the container's own Docker
        HEALTHCHECK, forever -- logging every 200 would drown out real
        request logs for no benefit (see telemetry/middleware.py)."""
        import logging

        logger = logging.getLogger("gateway.access")
        with self.assertRaises(AssertionError):
            with self.assertLogs(logger, level="INFO"):
                self.client.get("/healthz")

    def test_non_healthz_requests_are_still_logged(self):
        """The suppression is scoped to exactly path == "/healthz" and
        status == 200 -- everything else (including a 401 on a real
        route) is unaffected."""
        with self.assertLogs("gateway.access", level="INFO") as cm:
            self.client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].path, "/v1/chat")


if __name__ == "__main__":
    unittest.main()
