import io
import json
import logging
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..telemetry.cost import DEFAULT_PRICING, estimate_cost
from ..telemetry.debug_capture import DebugCaptureStore, redact
from ..telemetry.logging import JsonFormatter
from ..telemetry.slo import slo_breached
from .auth_fixtures import auth_header, get_auth_fixture
from .fake_clock import FakeClock
from .fakes import FakeConverseClient
from .otel_fixtures import make_test_tracer


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, guardrail_policy="standard-v1")
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class CostEstimationTests(unittest.TestCase):
    def test_known_model_uses_its_own_rates(self):
        model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        rates = DEFAULT_PRICING[model_id]
        cost = estimate_cost(model_id, input_tokens=1000, output_tokens=1000)
        self.assertAlmostEqual(cost, rates.input_per_1k + rates.output_per_1k)

    def test_zero_tokens_costs_nothing(self):
        self.assertEqual(estimate_cost("any-model", input_tokens=0, output_tokens=0), 0.0)

    def test_unknown_model_falls_back_to_default_rates_not_zero(self):
        cost = estimate_cost("some-brand-new-model", input_tokens=1000, output_tokens=1000)
        self.assertGreater(cost, 0.0)


class SloBreachTests(unittest.TestCase):
    def test_no_slo_configured_never_breaches(self):
        policy = _policy()  # slo.p95_latency_ms defaults to None
        self.assertFalse(slo_breached(policy, latency_ms=999999))

    def test_under_threshold_does_not_breach(self):
        from ..policy.models import TenantSlo

        policy = _policy(slo=TenantSlo(p95_latency_ms=3000))
        self.assertFalse(slo_breached(policy, latency_ms=1000))

    def test_over_threshold_breaches(self):
        from ..policy.models import TenantSlo

        policy = _policy(slo=TenantSlo(p95_latency_ms=3000))
        self.assertTrue(slo_breached(policy, latency_ms=5000))


class DebugCaptureTests(unittest.TestCase):
    def test_redacts_email(self):
        self.assertIn("[REDACTED_EMAIL]", redact("contact me at a@b.com please"))

    def test_redacts_ssn(self):
        self.assertIn("[REDACTED_SSN]", redact("my ssn is 123-45-6789"))

    def test_capture_then_get_round_trips_redacted(self):
        store = DebugCaptureStore()
        store.capture(
            request_id="req-1", tenant_id="acme",
            input_text="my email is a@b.com", output_text="ok, noted: a@b.com",
        )
        record = store.get("req-1")
        self.assertIsNotNone(record)
        self.assertNotIn("a@b.com", record.redacted_input)
        self.assertNotIn("a@b.com", record.redacted_output)

    def test_ttl_expiry(self):
        clock = FakeClock()
        store = DebugCaptureStore(ttl_s=60.0, clock=clock)
        store.capture(request_id="req-1", tenant_id="acme", input_text="hi", output_text="hello")

        clock.advance(30.0)
        self.assertIsNotNone(store.get("req-1"))

        clock.advance(31.0)
        self.assertIsNone(store.get("req-1"))


class SpanAttributeTests(unittest.TestCase):
    def _app(self, *, converse_client, policy_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        tracer, exporter = make_test_tracer()
        app = create_app(
            settings=settings,
            converse_client=converse_client,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            tracer=tracer,
        )
        return TestClient(app), fixture, exporter

    def test_successful_chat_span_carries_full_attribute_set(self):
        fake = FakeConverseClient(response_text="hi", input_tokens=5, output_tokens=3)
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})
        client, fixture, exporter = self._app(converse_client=fake, policy_store=policy_store)
        token = fixture.token(tenant_id="acme")

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )
        self.assertEqual(resp.status_code, 200)

        spans = exporter.get_finished_spans()
        self.assertEqual(len(spans), 1)
        attrs = spans[0].attributes

        self.assertEqual(attrs["tenant_id"], "acme")
        self.assertEqual(attrs["status"], 200)
        self.assertEqual(attrs["input_tokens"], 5)
        self.assertEqual(attrs["output_tokens"], 3)
        self.assertIn("estimated_cost", attrs)
        self.assertIn("slo_breach", attrs)
        self.assertFalse(attrs["cache_hit"])

    def test_blocked_request_span_carries_error_status(self):
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme", state=TenantState.SUSPENDED)})
        client, fixture, exporter = self._app(converse_client=FakeConverseClient(), policy_store=policy_store)
        token = fixture.token(tenant_id="acme")

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )
        self.assertEqual(resp.status_code, 403)

        spans = exporter.get_finished_spans()
        self.assertEqual(spans[0].attributes["status"], 403)


class PiiSafeLoggingTests(unittest.TestCase):
    def test_operational_logs_never_contain_raw_message_content(self):
        """The chat message content is a unique, easy-to-grep marker;
        none of the JSON emitted by the structured logging handler
        (telemetry/logging.py) should ever contain it."""
        marker = "TOTALLY-UNIQUE-SECRET-MARKER-8f3ac21"
        fake = FakeConverseClient(response_text=f"the answer involves {marker} too")
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})
        tracer, _exporter = make_test_tracer()
        app = create_app(
            settings=settings, converse_client=fake, token_verifier=fixture.verifier,
            policy_store=policy_store, tracer=tracer,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="acme")

        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        handler.setFormatter(JsonFormatter())
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            resp = client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": marker}]},
                headers=auth_header(token),
            )
        finally:
            root.removeHandler(handler)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(marker, resp.json()["output"])  # sanity: it really was in the response

        log_output = captured.getvalue()
        self.assertNotIn(marker, log_output)  # but never in operational telemetry

    def test_debug_capture_disabled_by_default_leaves_no_record(self):
        fake = FakeConverseClient(response_text="hello")
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore({"acme": _policy(tenant_id="acme")})  # debug_capture_enabled=False
        tracer, _exporter = make_test_tracer()
        debug_store = DebugCaptureStore()
        app = create_app(
            settings=settings, converse_client=fake, token_verifier=fixture.verifier,
            policy_store=policy_store, tracer=tracer, debug_capture_store=debug_store,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="acme")

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(debug_store.get(resp.json()["request_id"]))

    def test_debug_capture_enabled_tenant_stores_record(self):
        """Proves the wiring: an enabled tenant's interaction is captured
        end-to-end. Redaction itself (the interesting PII-handling logic)
        is unit-tested directly against DebugCaptureStore/redact() above
        -- content that contains PII patterns would be blocked by the
        output/input guardrail before ever reaching debug capture (both
        use the same pattern set), so there's no content that is both
        guardrail-clean and PII-bearing to round-trip through the real
        HTTP path here."""
        fake = FakeConverseClient(response_text="sure, happy to help with that")
        settings = load_settings()
        fixture = get_auth_fixture()
        policy_store = InMemoryPolicyStore(
            {"acme": _policy(tenant_id="acme", debug_capture_enabled=True)}
        )
        tracer, _exporter = make_test_tracer()
        debug_store = DebugCaptureStore()
        app = create_app(
            settings=settings, converse_client=fake, token_verifier=fixture.verifier,
            policy_store=policy_store, tracer=tracer, debug_capture_store=debug_store,
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="acme")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "what's a good way to reach support?"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 200)
        record = debug_store.get(resp.json()["request_id"])
        self.assertIsNotNone(record)
        self.assertEqual(record.tenant_id, "acme")
        self.assertIn("reach support", record.redacted_input)
        self.assertIn("happy to help", record.redacted_output)


if __name__ == "__main__":
    unittest.main()
