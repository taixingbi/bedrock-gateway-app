"""M6 scenario: guardrail unavailability under concurrent load (plan
section 17's "Guardrail 429" / "Guardrail timeout" scenarios, plan
section 11's fail-closed invariant under concurrency rather than just a
single request).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loadtests.fault_injection import FlakyGuardrailClient, ThrottlingFaultConverseClient
from loadtests.harness import build_scenario_app, default_tenant_policy, fire_concurrent


class GuardrailUnavailableUnderLoadScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_flaky_guardrail_fails_closed_for_every_concurrent_request_on_strict_tenant(self):
        fake = ThrottlingFaultConverseClient(throttle_rate=0.0)  # Bedrock is healthy -- irrelevant here
        flaky_guardrail = FlakyGuardrailClient(failure_rate=1.0)  # guardrail backend always unavailable
        scenario = build_scenario_app(
            tenants={
                "strict-tenant": default_tenant_policy(
                    "strict-tenant", guardrail_policy="finance-strict-v1", rpm_limit=10_000
                )
            },
            converse_client=fake,
            guardrail_client=flaky_guardrail,
        )

        async def make_request(_i: int):
            return await scenario.client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers=scenario.auth_header(tenant_id="strict-tenant"),
            )

        try:
            responses = await fire_concurrent(make_request, 25)
        finally:
            await scenario.aclose()

        self.assertTrue(all(r.status_code == 503 for r in responses))
        self.assertTrue(all(r.json()["error"]["code"] == "AI_SAFETY_SERVICE_UNAVAILABLE" for r in responses))
        # Under no interleaving of 25 concurrent requests does a STRICT
        # tenant's traffic reach the model while its guardrail is down.
        self.assertEqual(fake.total_calls, 0)


if __name__ == "__main__":
    unittest.main()
