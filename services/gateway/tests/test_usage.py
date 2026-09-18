import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..usage.store import InMemoryUsageStore, current_month
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


class BudgetEnforcementTests(unittest.TestCase):
    def _app(self, *, policy_store, usage_store, fake=None):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings,
            converse_client=fake or FakeConverseClient(),
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            usage_store=usage_store,
        )
        return TestClient(app), fixture

    def test_request_allowed_when_under_budget(self):
        policy_store = InMemoryPolicyStore({"acme": _policy(monthly_budget=10.0)})
        usage_store = InMemoryUsageStore()
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)
        self.assertGreater(usage_store.get("acme", current_month()), 0.0)

    def test_request_rejected_once_budget_exhausted(self):
        policy_store = InMemoryPolicyStore({"acme": _policy(monthly_budget=0.000001)})
        usage_store = InMemoryUsageStore()
        # Already at/over the tiny budget before the request even starts.
        usage_store.add_and_get("acme", current_month(), 1.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["error"]["code"], "BUDGET_EXCEEDED")

    def test_no_budget_configured_means_unlimited(self):
        policy_store = InMemoryPolicyStore({"acme": _policy(monthly_budget=None)})
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("acme", current_month(), 1_000_000.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        token = fixture.token(tenant_id="acme", roles=["developer"])

        resp = client.post(
            "/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth_header(token)
        )

        self.assertEqual(resp.status_code, 200)


class UsageReportEndpointTests(unittest.TestCase):
    def _app(self, *, policy_store, usage_store):
        settings = load_settings()
        fixture = get_auth_fixture()
        app = create_app(
            settings=settings,
            converse_client=FakeConverseClient(),
            token_verifier=fixture.verifier,
            policy_store=policy_store,
            usage_store=usage_store,
        )
        return TestClient(app), fixture

    def test_reports_spend_and_utilization_per_tenant(self):
        policy_store = InMemoryPolicyStore(
            {
                "finance": _policy(tenant_id="finance", monthly_budget=100.0),
                "sandbox": _policy(tenant_id="sandbox", monthly_budget=None),
            }
        )
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("finance", current_month(), 25.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        admin_token = fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"])

        resp = client.get("/v1/admin/usage", headers=auth_header(admin_token))

        self.assertEqual(resp.status_code, 200)
        by_tenant = {t["tenant_id"]: t for t in resp.json()["tenants"]}
        self.assertEqual(by_tenant["finance"]["spend"], 25.0)
        self.assertEqual(by_tenant["finance"]["monthly_budget"], 100.0)
        self.assertEqual(by_tenant["finance"]["utilization"], 0.25)
        self.assertEqual(by_tenant["sandbox"]["spend"], 0.0)
        self.assertIsNone(by_tenant["sandbox"]["utilization"])

    def test_non_admin_role_is_forbidden(self):
        policy_store = InMemoryPolicyStore({"finance": _policy(tenant_id="finance")})
        usage_store = InMemoryUsageStore()
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        dev_token = fixture.token(sub="dev-1", tenant_id="finance", roles=["developer"])

        resp = client.get("/v1/admin/usage", headers=auth_header(dev_token))

        self.assertEqual(resp.status_code, 403)

    def test_manager_sees_only_their_own_tenant(self):
        """plan section 30.3: a list endpoint has no single resource to
        gate on, so a manager gets a filtered result set instead of a
        403 -- unlike platform_admin, who still sees every tenant."""
        policy_store = InMemoryPolicyStore(
            {
                "finance": _policy(tenant_id="finance", monthly_budget=100.0),
                "sandbox": _policy(tenant_id="sandbox", monthly_budget=None),
            }
        )
        usage_store = InMemoryUsageStore()
        usage_store.add_and_get("finance", current_month(), 25.0)
        client, fixture = self._app(policy_store=policy_store, usage_store=usage_store)
        manager_token = fixture.token(sub="mgr-1", tenant_id="finance", roles=["manager"])

        resp = client.get("/v1/admin/usage", headers=auth_header(manager_token))

        self.assertEqual(resp.status_code, 200)
        tenant_ids = [t["tenant_id"] for t in resp.json()["tenants"]]
        self.assertEqual(tenant_ids, ["finance"])


if __name__ == "__main__":
    unittest.main()
