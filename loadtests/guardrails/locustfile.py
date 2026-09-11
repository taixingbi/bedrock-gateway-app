"""Locust scenario: baseline guardrail latency against a running gateway
(plan section 17's "Guardrail 429" / "Guardrail timeout" scenarios).

Injecting an actually-flaky guardrail backend into a real running server
needs a custom entrypoint (guardrail faults are a test concern -- there's
no production env-toggle for it in main.py, deliberately, since a real
deployment shouldn't have a "make my safety checks randomly fail" knob).
The automated, assertion-based fail-closed-under-concurrency proof lives
in test_scenario.py instead. This locustfile is for measuring real
guardrail-check latency's contribution to overall /v1/chat latency under
load, which is a legitimate thing to watch against a real deployment.

Usage:
    export GATEWAY_TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id finance)
    locust -f loadtests/guardrails/locustfile.py --headless \
        --host http://localhost:8080 -u 20 -r 5 -t 60s
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
