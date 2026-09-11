import json
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..inference.bedrock_client import BedrockInvocationError, StreamChunk
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..routing.circuit_breaker import BreakerState, CircuitBreaker
from ..streaming import stream_chat_response
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, guardrail_policy="standard-v1")
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class _AlwaysConnected:
    async def __call__(self) -> bool:
        return False


class _DisconnectAfterN:
    def __init__(self, n: int):
        self.n = n
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.n


def _make_chunk_iter(client: FakeConverseClient):
    return client.converse_stream(model_id="model-a", messages=[], max_tokens=100, temperature=0.5)


async def _collect(agen):
    return [chunk async for chunk in agen]


def _parse_data_lines(raw_chunks):
    events = []
    for raw in raw_chunks:
        text = raw.decode("utf-8")
        for block in text.strip("\n").split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: "):]))
    return events


class StreamChatResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion_yields_all_deltas_and_done(self):
        fake = FakeConverseClient(stream_chunks=["a", "b", "c"])
        breaker = CircuitBreaker()

        raw = await _collect(
            stream_chat_response(
                _make_chunk_iter(fake),
                model_id="model-a",
                request_id="req-1",
                tenant_id="acme",
                circuit_breaker=breaker,
                is_disconnected=_AlwaysConnected(),
            )
        )
        events = _parse_data_lines(raw)

        deltas = [e["delta"] for e in events if "delta" in e]
        self.assertEqual(deltas, ["a", "b", "c"])
        done_event = events[-1]
        self.assertTrue(done_event["done"])
        self.assertFalse(done_event["aborted"])
        self.assertEqual(breaker.state_of("model-a"), BreakerState.CLOSED)

    async def test_disconnect_mid_stream_cancels_generator(self):
        fake = FakeConverseClient(stream_chunks=["a", "b", "c", "d", "e"])
        breaker = CircuitBreaker()
        disconnect_after = _DisconnectAfterN(2)

        raw = await _collect(
            stream_chat_response(
                _make_chunk_iter(fake),
                model_id="model-a",
                request_id="req-1",
                tenant_id="acme",
                circuit_breaker=breaker,
                is_disconnected=disconnect_after,
            )
        )
        events = _parse_data_lines(raw)

        deltas = [e["delta"] for e in events if "delta" in e]
        self.assertLess(len(deltas), 5)  # stopped before the full stream
        done_event = events[-1]
        self.assertTrue(done_event["aborted"])
        self.assertTrue(fake.stream_was_cancelled)  # generator.close() reached the fake

    async def test_upstream_error_mid_stream_records_failure_and_emits_error_event(self):
        fake = FakeConverseClient(
            stream_error=BedrockInvocationError("boom", code="InternalServerException", retryable=False)
        )
        breaker = CircuitBreaker(failure_threshold=1)

        raw = await _collect(
            stream_chat_response(
                _make_chunk_iter(fake),
                model_id="model-a",
                request_id="req-1",
                tenant_id="acme",
                circuit_breaker=breaker,
                is_disconnected=_AlwaysConnected(),
            )
        )
        events = _parse_data_lines(raw)

        self.assertIn("error", events[0])
        self.assertEqual(events[0]["error"]["code"], "UPSTREAM_ERROR")
        self.assertEqual(breaker.state_of("model-a"), BreakerState.OPEN)


class StreamingIntegrationTests(unittest.TestCase):
    def _app(self, *, converse_client, policy_store=None, circuit_breaker=None):
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = policy_store or InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})
        app = create_app(
            settings=settings,
            converse_client=converse_client,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            circuit_breaker=circuit_breaker,
        )
        return TestClient(app), fixture

    def test_stream_true_returns_sse_with_deltas(self):
        fake = FakeConverseClient(stream_chunks=["Hello", " world"])
        client, fixture = self._app(converse_client=fake)
        token = fixture.token(tenant_id="acme")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        self.assertIn('"delta": "Hello"', resp.text)
        self.assertIn('"done": true', resp.text)

    def test_open_circuit_rejects_stream_before_starting(self):
        breaker = CircuitBreaker(failure_threshold=1)
        fake = FakeConverseClient(
            error=BedrockInvocationError("throttled", code="ThrottlingException", retryable=True)
        )
        client, fixture = self._app(converse_client=fake, circuit_breaker=breaker)
        token = fixture.token(tenant_id="acme")

        # Trip the breaker via a normal (non-streaming) failing call first.
        client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertEqual(len(fake.stream_calls), 0)  # never even attempted


if __name__ == "__main__":
    unittest.main()
