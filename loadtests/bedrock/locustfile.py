"""Locust scenario: baseline throughput + Bedrock throttling against a
*running* gateway instance (plan section 17's "baseline capacity" and
"Bedrock 429" scenarios).

This is the human-facing, real-HTTP counterpart to test_scenario.py's
automated in-process version. Point it at a local instance running with
a fault-injecting ConverseClient for a repeatable throttling test, or --
deliberately, by hand, never automated -- at a real Bedrock-backed
instance to see actual quota behavior. See docs/LOAD_TESTING.md.

Usage:
    export GATEWAY_TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id finance)
    locust -f loadtests/bedrock/locustfile.py --headless \
        --host http://localhost:8080 -u 20 -r 5 -t 60s \
        --exit-code-on-error 1
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
            json={"messages": [{"role": "user", "content": "Say hello in one sentence."}]},
            headers={"authorization": f"Bearer {TOKEN}"},
            name="/v1/chat",
        )
