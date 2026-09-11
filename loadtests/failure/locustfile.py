"""Locust scenario: kill-switch / policy update while traffic is in
flight (plan section 17's "kill-switch propagation" and "policy update
under load" scenarios). The automated, assertion-based version lives in
test_scenario.py; this is for watching it happen against a real running
server.

Usage:
    export GATEWAY_TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id finance)
    export GATEWAY_ADMIN_TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id platform --roles platform_admin)
    locust -f loadtests/failure/locustfile.py --headless \
        --host http://localhost:8080 -u 20 -r 5 -t 120s

    # In a second terminal, partway through the run, flip the tenant:
    curl -X PUT http://localhost:8080/v1/admin/tenants/finance/state \
        -H "authorization: Bearer $GATEWAY_ADMIN_TOKEN" \
        -H 'content-type: application/json' -d '{"state": "SUSPENDED"}'

Watch the /v1/chat error rate jump to ~100% immediately (push
invalidation -- the gateway process that served the admin call sees it
right away) rather than drifting up over up to POLICY_CACHE_TTL_S
seconds (the bounded-staleness fallback if a push were ever lost).
"""
from __future__ import annotations

import os

from locust import HttpUser, between, task

TOKEN = os.environ.get("GATEWAY_TOKEN", "")


class ChatUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task
    def chat(self):
        self.client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": f"Bearer {TOKEN}"},
            name="/v1/chat",
        )
