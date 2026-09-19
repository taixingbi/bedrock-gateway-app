"""M6 scenario: tenant noisy-neighbor isolation under concurrent load
(plan section 17's "tenant noisy neighbor" scenario, plan section 1's
isolation invariant applied to rate limiting).

Two tenants fire bursts concurrently: one with a tight rpm_limit, one
with a generous one. The invariant under test: tenant A exhausting its
own budget has zero effect on tenant B's requests.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loadtests.fault_injection import AlwaysAllowGuardrailClient, ThrottlingFaultConverseClient
from loadtests.harness import build_scenario_app, default_tenant_policy


class NoisyNeighborScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_tenant_a_burst_does_not_affect_tenant_b(self):
        fake = ThrottlingFaultConverseClient(throttle_rate=0.0)  # Bedrock itself is healthy
        scenario = build_scenario_app(
            tenants={
                "tenant-a": default_tenant_policy("tenant-a", rpm_limit=5),
                # max_concurrency=50, not just a generous rpm_limit: plan
                # section 16's concurrency fix added a second, independent
                # per-tenant cap (in-flight requests, not request rate) --
                # this scenario fires 30 truly simultaneous tenant-b
                # requests, which the concurrency_default_tenant_max=8
                # default would otherwise legitimately throttle on its own,
                # unrelated to tenant-a's burst. Set explicitly high here
                # for the same reason rpm_limit already is: proving
                # isolation, not exercising this tenant's own limits.
                "tenant-b": default_tenant_policy("tenant-b", rpm_limit=10_000, max_concurrency=50),
            },
            converse_client=fake,
            guardrail_client=AlwaysAllowGuardrailClient(),
        )

        async def make_request(tenant_id: str):
            return await scenario.client.post(
                "/v1/chat",
                json={"messages": [{"role": "user", "content": "hi"}]},
                headers=scenario.auth_header(tenant_id=tenant_id),
            )

        async def fire_for(tenant_id: str, n: int):
            return await asyncio.gather(*(make_request(tenant_id) for _ in range(n)))

        try:
            # 10 + 10, not 30 + 30: deliberately under Settings.
            # concurrency_global_max's default (32) -- plan section 16's
            # concurrency fix added a *shared* global cap alongside the
            # per-tenant one, and a shared resource is, by construction,
            # not fully isolable (same tension the SLO paper -- plan
            # section 31.5 -- flags for adaptive capacity estimation: a
            # global loop can legitimately let one tenant's load affect
            # another's through the shared ceiling, even with unlimited
            # per-tenant headroom). That's a real, separate concern from
            # what THIS test demonstrates -- rate-limit isolation, not
            # global-concurrency isolation -- so the burst here stays
            # small enough to not exercise that other boundary at all.
            results_a, results_b = await asyncio.gather(
                fire_for("tenant-a", 10), fire_for("tenant-b", 10)
            )
        finally:
            await scenario.aclose()

        a_statuses = [r.status_code for r in results_a]
        b_statuses = [r.status_code for r in results_b]

        # tenant-a's burst (30 requests against a 5 rpm_limit) hits its
        # own quota...
        self.assertIn(429, a_statuses)
        self.assertIn(200, a_statuses)
        # ...while tenant-b, hammering the gateway at the exact same
        # time, is completely unaffected.
        self.assertTrue(all(s == 200 for s in b_statuses), f"tenant-b statuses: {b_statuses}")


if __name__ == "__main__":
    unittest.main()
