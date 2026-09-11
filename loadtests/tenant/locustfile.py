"""Locust scenario: two tenants under simultaneous load (plan section
17's "tenant noisy neighbor" scenario). The automated, assertion-based
version lives in test_scenario.py; this is for watching real per-tenant
throughput/error-rate via Locust's own stats (grouped by the `name=`
tag on each request).

Usage:
    export GATEWAY_TOKEN_A=$(python scripts/generate_dev_token.py -q --tenant-id tenant-a)
    export GATEWAY_TOKEN_B=$(python scripts/generate_dev_token.py -q --tenant-id tenant-b)
    locust -f loadtests/tenant/locustfile.py --headless \
        --host http://localhost:8080 -u 40 -r 10 -t 60s

Requires tenant-a and tenant-b to exist in the running gateway's
policies/tenants.yaml, with tenant-a given a noticeably tighter
rpm_limit than tenant-b so the isolation is visible in the stats.
"""
from __future__ import annotations

import os

from locust import HttpUser, between, task

TOKEN_A = os.environ.get("GATEWAY_TOKEN_A", "")
TOKEN_B = os.environ.get("GATEWAY_TOKEN_B", "")


class NoisyTenantUser(HttpUser):
    """Expect this tenant's request group to show a rising 429 rate once
    its rpm_limit is exhausted."""

    weight = 1
    wait_time = between(0.01, 0.05)

    @task
    def chat(self):
        self.client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {TOKEN_A}"},
            name="/v1/chat [tenant-a, noisy]",
        )


class QuietTenantUser(HttpUser):
    """Expect this tenant's request group to stay at ~0% errors
    regardless of how hard tenant-a is hammering the gateway at the same
    time -- that's the isolation invariant this scenario is watching."""

    weight = 1
    wait_time = between(0.5, 1.0)

    @task
    def chat(self):
        self.client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {TOKEN_B}"},
            name="/v1/chat [tenant-b, quiet]",
        )
