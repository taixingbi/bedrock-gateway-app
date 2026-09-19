"""Plan section 34.6: pipeline.admission_decision() -- one reported
decision over kill-switch/rate-limit/budget instead of three separate
raise-or-continue calls. Calls the exact same enforce_* functions
test_policy.py/test_cost_governance.py already cover individually;
these tests check the wrapping (stage attribution, warning
passthrough) and the end-to-end /v1/chat wiring.
"""
import unittest

from starlette.testclient import TestClient

from .. import pipeline
from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..policy.store import InMemoryPolicyStore
from ..usage.store import InMemoryUsageStore, current_day, current_month
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class AdmissionDecisionUnitTests(unittest.TestCase):
    def test_allowed_when_everything_passes(self):
        decision = pipeline.admission_decision(
            _policy(), rate_limiter=TokenBucketRateLimiter(), usage_store=InMemoryUsageStore(),
            month=current_month(),
        )

        self.assertTrue(decision.allowed)
        self.assertIsNone(decision.stage)
        self.assertIsNone(decision.error)

    def test_kill_switch_stage_attributed(self):
        decision = pipeline.admission_decision(
            _policy(state=TenantState.SUSPENDED), rate_limiter=TokenBucketRateLimiter(),
            usage_store=InMemoryUsageStore(), month=current_month(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stage, "kill_switch")
        self.assertEqual(decision.error.code, "TENANT_BLOCKED")

    def test_rate_limit_stage_attributed(self):
        limiter = TokenBucketRateLimiter()
        policy = _policy(rpm_limit=1)
        pipeline.enforce_rate_limit(policy, rate_limiter=limiter)  # consume the only token

        decision = pipeline.admission_decision(
            policy, rate_limiter=limiter, usage_store=InMemoryUsageStore(), month=current_month(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stage, "rate_limit")
        self.assertEqual(decision.error.code, "QUOTA_EXCEEDED")

    def test_budget_stage_attributed(self):
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 100.0)
        policy = _policy(monthly_budget=1.0)

        decision = pipeline.admission_decision(
            policy, rate_limiter=TokenBucketRateLimiter(), usage_store=usage_store, month=current_month(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.stage, "budget")
        self.assertEqual(decision.error.code, "BUDGET_EXCEEDED")

    def test_kill_switch_checked_before_rate_limit(self):
        """A blocked tenant is rejected at kill_switch even if it would
        also have failed rate limiting -- same stage ORDER the
        pre-34.6 sequential calls always had, just now reported."""
        limiter = TokenBucketRateLimiter()
        policy = _policy(state=TenantState.SUSPENDED, rpm_limit=1)
        pipeline.enforce_rate_limit(_policy(rpm_limit=1), rate_limiter=limiter)  # exhaust it too

        decision = pipeline.admission_decision(
            policy, rate_limiter=limiter, usage_store=InMemoryUsageStore(), month=current_month(),
        )

        self.assertEqual(decision.stage, "kill_switch")

    def test_soft_budget_warning_passed_through_on_allow(self):
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 85.0)
        policy = _policy(monthly_budget=100.0, monthly_budget_soft_threshold_pct=0.8)

        decision = pipeline.admission_decision(
            policy, rate_limiter=TokenBucketRateLimiter(), usage_store=usage_store, month=current_month(),
        )

        self.assertTrue(decision.allowed)
        self.assertIsNotNone(decision.warning)


class AdmissionDecisionIntegrationTests(unittest.TestCase):
    def _app(self, *, policy_store, usage_store=None):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier,
            policy_store=policy_store, usage_store=usage_store or InMemoryUsageStore(),
        )
        return TestClient(app), fixture

    def test_suspended_tenant_rejected_via_admission_control(self):
        """Confirms admission_decision is actually wired into
        /v1/chat, not just unit-tested in isolation -- same assertion
        test_policy.py's KillSwitchIntegrationTests already makes,
        kept here as a regression guard on the 34.6 refactor
        specifically."""
        policy_store = InMemoryPolicyStore(
            {"acme": _policy(state=TenantState.SUSPENDED)}
        )
        client, fixture = self._app(policy_store=policy_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "TENANT_BLOCKED")


if __name__ == "__main__":
    unittest.main()
