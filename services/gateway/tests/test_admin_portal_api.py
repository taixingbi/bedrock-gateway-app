import unittest

from starlette.testclient import TestClient

from ..auth.aws_iam import IamPrincipalGrant
from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from ..routing.router import RouteSet
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


class FakeIamTenantResolver:
    def __init__(self, grants):
        self._grants = grants

    def resolve(self, principal_arn, *, request_id=None):
        return self._grants[principal_arn]

    def list_grants(self):
        return dict(self._grants)


def _policy(**overrides) -> TenantPolicy:
    defaults = dict(tenant_id="acme", state=TenantState.ACTIVE, rpm_limit=60)
    defaults.update(overrides)
    return TenantPolicy(**defaults)


def _app(**overrides):
    settings = load_settings()
    fixture = get_auth_fixture()
    app = create_app(
        settings=settings,
        converse_client=FakeConverseClient(),
        token_verifier=fixture.verifier,
        **overrides,
    )
    return TestClient(app), fixture


def _admin_headers(fixture) -> dict:
    return auth_header(fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"]))


class ListTenantsTests(unittest.TestCase):
    def test_returns_full_policy_for_every_tenant(self):
        policy_store = InMemoryPolicyStore(
            {"finance": _policy(tenant_id="finance", monthly_budget=100.0, route_set="finance-chat-v1")}
        )
        client, fixture = _app(policy_store=policy_store)

        resp = client.get("/v1/admin/tenants", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        tenants = {t["tenant_id"]: t for t in resp.json()["tenants"]}
        self.assertEqual(tenants["finance"]["monthly_budget"], 100.0)
        self.assertEqual(tenants["finance"]["route_set"], "finance-chat-v1")
        self.assertEqual(tenants["finance"]["state"], "ACTIVE")

    def test_non_admin_is_forbidden(self):
        client, fixture = _app(policy_store=InMemoryPolicyStore({"finance": _policy(tenant_id="finance")}))
        token = fixture.token(tenant_id="finance", roles=["developer"])

        resp = client.get("/v1/admin/tenants", headers=auth_header(token))

        self.assertEqual(resp.status_code, 403)


class ListRouteSetsTests(unittest.TestCase):
    def test_flags_uncertified_fallback(self):
        route_sets = {
            "rs1": RouteSet(name="rs1", primary="model-a", fallbacks=["model-b", "model-uncertified"])
        }
        client, fixture = _app(
            route_sets=route_sets, certified_model_ids={"model-a", "model-b"}
        )

        resp = client.get("/v1/admin/route-sets", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        rs1 = next(r for r in body["route_sets"] if r["name"] == "rs1")
        self.assertTrue(rs1["primary_certified"])
        by_model = {f["model"]: f["certified"] for f in rs1["fallbacks"]}
        self.assertTrue(by_model["model-b"])
        self.assertFalse(by_model["model-uncertified"])
        self.assertEqual(set(body["certified_models"]), {"model-a", "model-b"})


class ListApplicationsTests(unittest.TestCase):
    def test_lists_configured_iam_grants(self):
        resolver = FakeIamTenantResolver(
            {
                "arn:aws:iam::123:role/team-a-ai-client": IamPrincipalGrant(
                    tenant_id="team-a", application_id="team-a-ai-client", roles=["developer"]
                )
            }
        )
        client, fixture = _app(iam_tenant_resolver=resolver)

        resp = client.get("/v1/admin/applications", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        apps = resp.json()["applications"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["application_id"], "team-a-ai-client")
        self.assertEqual(apps[0]["tenant_id"], "team-a")


if __name__ == "__main__":
    unittest.main()
