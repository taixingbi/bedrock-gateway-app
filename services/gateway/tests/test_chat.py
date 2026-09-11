import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..inference.bedrock_client import BedrockInvocationError
from ..main import create_app
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _client(fake: FakeConverseClient) -> TestClient:
    settings = load_settings()
    fixture = get_auth_fixture()
    app = create_app(settings=settings, converse_client=fake, token_verifier=fixture.verifier)
    return TestClient(app)


def _auth_headers(**token_kwargs) -> dict:
    return auth_header(get_auth_fixture().token(**token_kwargs))


class ChatEndpointTests(unittest.TestCase):
    def test_success_returns_model_output_and_usage(self):
        fake = FakeConverseClient(response_text="hi there", input_tokens=5, output_tokens=3)
        client = _client(fake)

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hello"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["output"], "hi there")
        self.assertEqual(body["usage"], {"input_tokens": 5, "output_tokens": 3})
        self.assertTrue(body["request_id"])
        self.assertEqual(len(fake.calls), 1)

    def test_model_override_is_passed_through(self):
        fake = FakeConverseClient()
        client = _client(fake)

        client.post(
            "/v1/chat",
            json={
                "model": "anthropic.claude-3-haiku",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=_auth_headers(),
        )

        self.assertEqual(fake.calls[0]["model_id"], "anthropic.claude-3-haiku")

    def test_default_model_used_when_omitted(self):
        fake = FakeConverseClient()
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(settings=settings, converse_client=fake, token_verifier=fixture.verifier)
        client = TestClient(app)

        client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(fake.calls[0]["model_id"], settings.bedrock_model_id)

    def test_request_id_echoed_from_header(self):
        fake = FakeConverseClient()
        client = _client(fake)

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={**_auth_headers(), "x-request-id": "req-fixed-123"},
        )

        self.assertEqual(resp.headers["x-request-id"], "req-fixed-123")
        self.assertEqual(resp.json()["request_id"], "req-fixed-123")

    def test_empty_messages_is_rejected(self):
        client = _client(FakeConverseClient())

        resp = client.post("/v1/chat", json={"messages": []}, headers=_auth_headers())

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")

    def test_last_message_must_be_user(self):
        client = _client(FakeConverseClient())

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "assistant", "content": "hi"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 400)

    def test_invalid_json_body(self):
        client = _client(FakeConverseClient())

        resp = client.post(
            "/v1/chat",
            content=b"not json",
            headers={**_auth_headers(), "content-type": "application/json"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_JSON")

    def test_throttling_maps_to_429(self):
        fake = FakeConverseClient(
            error=BedrockInvocationError(
                "throttled", code="ThrottlingException", retryable=True
            )
        )
        client = _client(fake)

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "UPSTREAM_THROTTLED")

    def test_unknown_upstream_error_maps_to_502(self):
        fake = FakeConverseClient(
            error=BedrockInvocationError("boom", code="SomethingElse", retryable=False)
        )
        client = _client(fake)

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=_auth_headers(),
        )

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"]["code"], "UPSTREAM_ERROR")


if __name__ == "__main__":
    unittest.main()
