import unittest

from starlette.testclient import TestClient

from ..cache.store import InMemoryResponseCache
from ..config import load_settings
from ..inference.bedrock_client import BedrockInvocationError
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..routing.circuit_breaker import BreakerState, CircuitBreaker
from ..routing.router import AllRoutesUnavailableError, CertifiedRouter, RouteSet
from .auth_fixtures import auth_header, get_auth_fixture
from .fake_clock import FakeClock
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, guardrail_policy="standard-v1")
    defaults.update(overrides)
    return TenantPolicy(**defaults)


def _throttled_error():
    return BedrockInvocationError("throttled", code="ThrottlingException", retryable=True)


class CircuitBreakerTests(unittest.TestCase):
    def test_starts_closed_and_allows(self):
        breaker = CircuitBreaker()
        self.assertTrue(breaker.allow("model-a"))
        self.assertEqual(breaker.state_of("model-a"), BreakerState.CLOSED)

    def test_opens_after_failure_threshold(self):
        breaker = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            breaker.record_failure("model-a")
        self.assertEqual(breaker.state_of("model-a"), BreakerState.OPEN)
        self.assertFalse(breaker.allow("model-a"))

    def test_success_resets_failure_count(self):
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure("model-a")
        breaker.record_failure("model-a")
        breaker.record_success("model-a")
        breaker.record_failure("model-a")
        breaker.record_failure("model-a")
        self.assertEqual(breaker.state_of("model-a"), BreakerState.CLOSED)  # only 2 consecutive since reset

    def test_transitions_to_half_open_after_reset_timeout(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=30.0, clock=clock)
        breaker.record_failure("model-a")
        self.assertFalse(breaker.allow("model-a"))

        clock.advance(31.0)
        self.assertTrue(breaker.allow("model-a"))
        self.assertEqual(breaker.state_of("model-a"), BreakerState.HALF_OPEN)

    def test_half_open_failure_reopens(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=30.0, clock=clock)
        breaker.record_failure("model-a")
        clock.advance(31.0)
        breaker.allow("model-a")  # transitions to HALF_OPEN
        breaker.record_failure("model-a")
        self.assertEqual(breaker.state_of("model-a"), BreakerState.OPEN)

    def test_half_open_success_closes(self):
        clock = FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=30.0, clock=clock)
        breaker.record_failure("model-a")
        clock.advance(31.0)
        breaker.allow("model-a")
        breaker.record_success("model-a")
        self.assertEqual(breaker.state_of("model-a"), BreakerState.CLOSED)

    def test_models_are_isolated(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure("model-a")
        self.assertFalse(breaker.allow("model-a"))
        self.assertTrue(breaker.allow("model-b"))


class CertifiedRouterTests(unittest.TestCase):
    def test_single_candidate_success_no_fallback(self):
        fake = FakeConverseClient(response_text="ok")
        router = CertifiedRouter(converse_client=fake, circuit_breaker=CircuitBreaker(), route_sets={})

        routed = router.converse(
            primary_model_id="model-a", route_set_name=None, messages=[], max_tokens=100, temperature=0.5
        )

        self.assertEqual(routed.model_id, "model-a")
        self.assertFalse(routed.fallback)
        self.assertEqual(len(fake.calls), 1)

    def test_falls_back_to_certified_fallback_on_primary_failure(self):
        fake = FailNTimesThenSucceed(fail_models={"model-a"})
        route_sets = {"rs1": RouteSet(name="rs1", primary="model-a", fallbacks=["model-b"])}
        router = CertifiedRouter(converse_client=fake, circuit_breaker=CircuitBreaker(), route_sets=route_sets)

        routed = router.converse(
            primary_model_id="model-a", route_set_name="rs1", messages=[], max_tokens=100, temperature=0.5
        )

        self.assertEqual(routed.model_id, "model-b")
        self.assertTrue(routed.fallback)

    def test_fallback_only_from_certified_route_set_never_arbitrary(self):
        """A model not listed in the route set's fallbacks is never tried,
        even if the primary fails -- fallback cannot bypass certification
        (plan section 13's core invariant)."""
        fake = FailNTimesThenSucceed(fail_models={"model-a"})
        route_sets = {"rs1": RouteSet(name="rs1", primary="model-a", fallbacks=["model-b"])}
        router = CertifiedRouter(converse_client=fake, circuit_breaker=CircuitBreaker(), route_sets=route_sets)

        router.converse(
            primary_model_id="model-a", route_set_name="rs1", messages=[], max_tokens=100, temperature=0.5
        )

        called_models = {c["model_id"] for c in fake.calls}
        self.assertEqual(called_models, {"model-a", "model-b"})
        self.assertNotIn("model-uncertified", called_models)

    def test_all_candidates_fail_raises_last_error(self):
        fake = FakeConverseClient(error=_throttled_error())
        route_sets = {"rs1": RouteSet(name="rs1", primary="model-a", fallbacks=["model-b"])}
        router = CertifiedRouter(converse_client=fake, circuit_breaker=CircuitBreaker(), route_sets=route_sets)

        with self.assertRaises(BedrockInvocationError):
            router.converse(
                primary_model_id="model-a", route_set_name="rs1", messages=[], max_tokens=100, temperature=0.5
            )

    def test_all_candidates_circuit_open_raises_all_routes_unavailable(self):
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure("model-a")
        breaker.record_failure("model-b")
        fake = FakeConverseClient()
        route_sets = {"rs1": RouteSet(name="rs1", primary="model-a", fallbacks=["model-b"])}
        router = CertifiedRouter(converse_client=fake, circuit_breaker=breaker, route_sets=route_sets)

        with self.assertRaises(AllRoutesUnavailableError):
            router.converse(
                primary_model_id="model-a", route_set_name="rs1", messages=[], max_tokens=100, temperature=0.5
            )
        self.assertEqual(len(fake.calls), 0)

    def test_no_route_set_configured_behaves_like_direct_call(self):
        fake = FakeConverseClient()
        router = CertifiedRouter(converse_client=fake, circuit_breaker=CircuitBreaker(), route_sets={})

        routed = router.converse(
            primary_model_id="model-a", route_set_name="unknown-route-set", messages=[], max_tokens=1, temperature=0.1
        )

        self.assertEqual(routed.model_id, "model-a")
        self.assertFalse(routed.fallback)


class FailNTimesThenSucceed:
    """Fails once per model in fail_models, then succeeds -- used to
    exercise the fallback path deterministically."""

    def __init__(self, *, fail_models):
        self._fail_models = set(fail_models)
        self._failed_once = set()
        self.calls = []

    def converse(self, *, model_id, messages, max_tokens, temperature):
        self.calls.append({"model_id": model_id})
        if model_id in self._fail_models and model_id not in self._failed_once:
            self._failed_once.add(model_id)
            raise _throttled_error()
        from ..inference.bedrock_client import ConverseResult

        return ConverseResult(
            text=f"response from {model_id}", input_tokens=1, output_tokens=1, stop_reason="end_turn", latency_ms=1.0
        )


class RoutingIntegrationTests(unittest.TestCase):
    def test_breaker_opens_and_fallback_is_used_at_app_level(self):
        # Pinned via the tenant's own models allowlist rather than relying
        # on settings.bedrock_model_id's default -- keeps this test
        # correct regardless of what the gateway's default model is
        # configured to at any given time.
        primary_model = "test-primary-model"
        fallback_model = "test-fallback-model"

        fake = FailNTimesThenSucceed(fail_models={primary_model})
        policy_store = InMemoryPolicyStore(
            {
                "finance": _policy(
                    tenant_id="finance", route_set="finance-chat-v1", models=[primary_model]
                )
            }
        )
        route_sets = {
            "finance-chat-v1": RouteSet(
                name="finance-chat-v1",
                primary=primary_model,
                fallbacks=[fallback_model],
            )
        }
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings,
            converse_client=fake,
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            route_sets=route_sets,
            response_cache=InMemoryResponseCache(),  # fresh, unshared cache
        )
        client = TestClient(app)
        token = fixture.token(tenant_id="finance")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["fallback"])
        self.assertEqual(body["model"], fallback_model)


if __name__ == "__main__":
    unittest.main()
