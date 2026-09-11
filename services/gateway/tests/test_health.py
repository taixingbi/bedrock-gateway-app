import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from .fakes import FakeConverseClient


class HealthzTests(unittest.TestCase):
    def setUp(self):
        settings = load_settings()
        self.app = create_app(settings=settings, converse_client=FakeConverseClient())
        self.client = TestClient(self.app)

    def test_healthz_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_request_id_header_present(self):
        resp = self.client.get("/healthz")
        self.assertIn("x-request-id", resp.headers)


if __name__ == "__main__":
    unittest.main()
