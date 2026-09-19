"""Plan section 34.7: cost governance beyond the pre-existing hard
monthly cap (test_usage.py's BudgetEnforcementTests) -- daily budget,
per-application budget/attribution, soft-threshold warning, and the
usage anomalies heuristic endpoint.
"""
import unittest

from starlette.testclient import TestClient

from .. import pipeline
from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..usage.store import (
    InMemoryUsageStore,
    add_and_get_application,
    current_day,
    current_month,
    get_application,
    trailing_days,
)
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class ApplicationUsageCompositionTests(unittest.TestCase):
    """usage/store.py's add_and_get_application/get_application compose
    over the existing store rather than adding a real third key
    dimension -- these confirm the composition doesn't collide with
    plain tenant-level tracking."""

    def test_application_spend_is_tracked_independently_of_tenant_total(self):
        store = InMemoryUsageStore()

        add_and_get_application(store, "acme", "app1", current_month(), 10.0)

        self.assertEqual(get_application(store, "acme", "app1", current_month()), 10.0)
        self.assertEqual(store.get("acme", current_month()), 0.0)  # tenant-level untouched

    def test_two_applications_under_one_tenant_are_isolated(self):
        store = InMemoryUsageStore()

        add_and_get_application(store, "acme", "app1", current_month(), 10.0)
        add_and_get_application(store, "acme", "app2", current_month(), 5.0)

        self.assertEqual(get_application(store, "acme", "app1", current_month()), 10.0)
        self.assertEqual(get_application(store, "acme", "app2", current_month()), 5.0)

    def test_same_application_name_under_different_tenants_is_isolated(self):
        store = InMemoryUsageStore()

        add_and_get_application(store, "acme", "shared-app-name", current_month(), 10.0)
        add_and_get_application(store, "other", "shared-app-name", current_month(), 3.0)

        self.assertEqual(get_application(store, "acme", "shared-app-name", current_month()), 10.0)
        self.assertEqual(get_application(store, "other", "shared-app-name", current_month()), 3.0)


class TrailingDaysTests(unittest.TestCase):
    def test_returns_requested_count_oldest_first(self):
        days = trailing_days(7, clock=lambda: 1_700_000_000.0)

        self.assertEqual(len(days), 7)
        self.assertEqual(days, sorted(days))  # oldest first

    def test_excludes_today(self):
        today = current_day(clock=lambda: 1_700_000_000.0)
        days = trailing_days(7, clock=lambda: 1_700_000_000.0)

        self.assertNotIn(today, days)


class EnforceBudgetDailyTests(unittest.TestCase):
    def test_daily_budget_exceeded_rejects(self):
        policy = _policy(daily_budget=1.0)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_day(), 2.0)

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_budget(
                policy, usage_store=usage_store, month=current_month(), day=current_day()
            )
        self.assertEqual(ctx.exception.code, "DAILY_BUDGET_EXCEEDED")

    def test_daily_budget_none_skips_check(self):
        policy = _policy(daily_budget=None)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_day(), 1_000_000.0)

        pipeline.enforce_budget(
            policy, usage_store=usage_store, month=current_month(), day=current_day()
        )  # no raise

    def test_day_omitted_skips_check_even_with_daily_budget_set(self):
        policy = _policy(daily_budget=1.0)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_day(), 2.0)

        pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())  # no raise


class EnforceBudgetApplicationTests(unittest.TestCase):
    def test_application_budget_exceeded_rejects(self):
        policy = _policy(application_budgets={"claims-agent": 5.0})
        usage_store = InMemoryUsageStore()
        add_and_get_application(usage_store, "acme", "claims-agent", current_month(), 6.0)

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.enforce_budget(
                policy, usage_store=usage_store, month=current_month(), application_id="claims-agent"
            )
        self.assertEqual(ctx.exception.code, "APPLICATION_BUDGET_EXCEEDED")

    def test_unbudgeted_application_is_unaffected(self):
        policy = _policy(application_budgets={"claims-agent": 5.0})
        usage_store = InMemoryUsageStore()
        add_and_get_application(usage_store, "acme", "summarizer", current_month(), 1_000_000.0)

        pipeline.enforce_budget(
            policy, usage_store=usage_store, month=current_month(), application_id="summarizer"
        )  # no raise -- summarizer has no configured budget

    def test_two_applications_are_isolated(self):
        policy = _policy(application_budgets={"claims-agent": 5.0, "summarizer": 100.0})
        usage_store = InMemoryUsageStore()
        add_and_get_application(usage_store, "acme", "claims-agent", current_month(), 6.0)

        with self.assertRaises(pipeline.PipelineError):
            pipeline.enforce_budget(
                policy, usage_store=usage_store, month=current_month(), application_id="claims-agent"
            )
        pipeline.enforce_budget(
            policy, usage_store=usage_store, month=current_month(), application_id="summarizer"
        )  # summarizer nowhere near its own budget


class EnforceBudgetSoftThresholdTests(unittest.TestCase):
    def test_crossing_soft_threshold_returns_warning_without_blocking(self):
        policy = _policy(monthly_budget=100.0, monthly_budget_soft_threshold_pct=0.8)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 85.0)

        warning = pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())

        self.assertIsNotNone(warning)
        self.assertIn("80%", warning)

    def test_below_soft_threshold_no_warning(self):
        policy = _policy(monthly_budget=100.0, monthly_budget_soft_threshold_pct=0.8)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 10.0)

        warning = pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())

        self.assertIsNone(warning)

    def test_no_soft_threshold_configured_no_warning(self):
        policy = _policy(monthly_budget=100.0, monthly_budget_soft_threshold_pct=None)
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 99.0)

        warning = pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())

        self.assertIsNone(warning)


class ApplicationBudgetIntegrationTests(unittest.TestCase):
    """End-to-end through /v1/chat -- confirms enforce_budget is
    actually wired with the right application_id/day at the route
    layer, not just unit-tested in isolation."""

    def _app(self, *, policy_store, usage_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier,
            policy_store=policy_store, usage_store=usage_store,
        )
        return TestClient(app), fixture

    def test_request_rejected_once_application_budget_exhausted(self):
        policy_store = InMemoryPolicyStore(
            {"acme": _policy(application_budgets={"risk-chat": 0.000001})}
        )
        usage_store = InMemoryUsageStore()
        add_and_get_application(usage_store, "acme", "risk-chat", current_month(), 1.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        # auth_fixtures' default application_id is "risk-chat" -- see auth_fixtures.py
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "APPLICATION_BUDGET_EXCEEDED")

    def test_successful_request_records_per_application_spend(self):
        policy_store = InMemoryPolicyStore({"acme": _policy()})
        usage_store = InMemoryUsageStore()
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)
        self.assertGreater(get_application(usage_store, "acme", "risk-chat", current_month()), 0.0)
        self.assertGreater(usage_store.get("acme", current_day()), 0.0)


class UsageAnomaliesEndpointTests(unittest.TestCase):
    def _app(self, *, policy_store, usage_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier,
            policy_store=policy_store, usage_store=usage_store,
        )
        return TestClient(app), fixture

    def test_flags_tenant_with_spike_above_threshold(self):
        policy_store = InMemoryPolicyStore({"acme": _policy()})
        usage_store = InMemoryUsageStore()
        for day in trailing_days(7):
            usage_store.add_and_get("acme", day, 1.0)  # trailing avg = 1.0/day
        usage_store.add_and_get("acme", current_day(), 10.0)  # today: 10x the average
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        admin_token = fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"])

        resp = client.get("/v1/admin/usage/anomalies", headers=auth_header(admin_token))

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["method"], "heuristic")
        flagged = {a["tenant_id"] for a in body["anomalies"]}
        self.assertIn("acme", flagged)

    def test_normal_spend_not_flagged(self):
        policy_store = InMemoryPolicyStore({"acme": _policy()})
        usage_store = InMemoryUsageStore()
        for day in trailing_days(7):
            usage_store.add_and_get("acme", day, 5.0)
        usage_store.add_and_get("acme", current_day(), 5.5)  # roughly normal
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        admin_token = fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"])

        resp = client.get("/v1/admin/usage/anomalies", headers=auth_header(admin_token))

        flagged = {a["tenant_id"] for a in resp.json()["anomalies"]}
        self.assertNotIn("acme", flagged)

    def test_no_trailing_history_is_never_flagged(self):
        """A brand-new tenant's first day of spend must not
        divide-by-zero into a trivial 'infinite' anomaly."""
        policy_store = InMemoryPolicyStore({"acme": _policy()})
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_day(), 500.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        admin_token = fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"])

        resp = client.get("/v1/admin/usage/anomalies", headers=auth_header(admin_token))

        flagged = {a["tenant_id"] for a in resp.json()["anomalies"]}
        self.assertNotIn("acme", flagged)

    def test_manager_sees_only_their_own_tenant(self):
        policy_store = InMemoryPolicyStore(
            {"finance": _policy(tenant_id="finance"), "other": _policy(tenant_id="other")}
        )
        usage_store = InMemoryUsageStore()
        for tenant_id in ("finance", "other"):
            for day in trailing_days(7):
                usage_store.add_and_get(tenant_id, day, 1.0)
            usage_store.add_and_get(tenant_id, current_day(), 100.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        manager_token = fixture.token(sub="mgr-1", tenant_id="finance", roles=["manager"])

        resp = client.get("/v1/admin/usage/anomalies", headers=auth_header(manager_token))

        flagged = {a["tenant_id"] for a in resp.json()["anomalies"]}
        self.assertEqual(flagged, {"finance"})

    def test_non_admin_or_manager_is_forbidden(self):
        policy_store = InMemoryPolicyStore({"finance": _policy(tenant_id="finance")})
        usage_store = InMemoryUsageStore()
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        dev_token = fixture.token(sub="dev-1", tenant_id="finance", roles=["developer"])

        resp = client.get("/v1/admin/usage/anomalies", headers=auth_header(dev_token))

        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
