"""M6 scenario: Bedrock throttling under concurrent load (plan section
17's "Bedrock 429" + "circuit breaker" + "no retry storm" scenarios).

Fires a burst of concurrent /v1/chat requests at a tenant whose model is
always throttled. The invariant under test: once the circuit breaker
opens, the gateway stops calling the failing model for the rest of the
burst -- it doesn't keep hammering it request after request (a "retry
storm"). See loadtests/harness.py for why this runs as a direct ASGI
concurrency test rather than a real locust/HTTP run.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loadtests.fault_injection import AlwaysAllowGuardrailClient, ThrottlingFaultConverseClient
from loadtests.harness import build_scenario_app, default_tenant_policy, fire_concurrent
from services.gateway.routing.circuit_breaker import BreakerState, CircuitBreaker


class BedrockThrottlingScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_circuit_breaker_prevents_a_retry_storm_under_concurrent_throttling(self):
        n_requests = 40
        failure_threshold = 5

        fake = ThrottlingFaultConverseClient(throttle_rate=1.0)  # every call throttles
        breaker = CircuitBreaker(failure_threshold=failure_threshold, reset_timeout_s=3600.0)
        scenario = build_scenario_app(
            tenants={"finance": default_tenant_policy("finance", rpm_limit=10_000)},
            converse_client=fake,
            guardrail_client=AlwaysAllowGuardrailClient(),
            circuit_breaker=breaker,
        )

        async def make_request(_i: int):
            return await scenario.client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers=scenario.auth_header(tenant_id="finance"),
            )

        try:
            responses = await fire_concurrent(make_request, n_requests)
        finally:
            await scenario.aclose()

        statuses = [r.status_code for r in responses if not isinstance(r, Exception)]
        codes = [
            r.json()["error"]["code"]
            for r in responses
            if not isinstance(r, Exception) and r.status_code >= 400
        ]

        # The core invariant: the gateway did NOT call the failing model
        # for every single one of the 40 client requests -- the breaker
        # opened partway through and fast-failed the rest.
        self.assertLess(fake.total_calls, n_requests)
        self.assertEqual(breaker.state_of("anthropic.claude-3-5-sonnet-20241022-v2:0"), BreakerState.OPEN)

        # Some requests got a real (fake-)upstream throttling error...
        self.assertIn("UPSTREAM_THROTTLED", codes)
        # ...and once open, the rest were fast-failed by the breaker
        # itself, never reaching the fake at all.
        self.assertIn("ALL_ROUTES_UNAVAILABLE", codes)
        self.assertTrue(all(s >= 400 for s in statuses))  # nothing succeeded -- every call throttles


if __name__ == "__main__":
    unittest.main()
