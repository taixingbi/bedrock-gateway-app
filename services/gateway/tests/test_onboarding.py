import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


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


def _dev_headers(fixture) -> dict:
    return auth_header(fixture.token(sub="dev-1", tenant_id="finance", roles=["developer"]))


def _admin_headers(fixture) -> dict:
    return auth_header(fixture.token(sub="admin-1", tenant_id="platform", roles=["platform_admin"]))


def _submit(client, fixture, **overrides) -> str:
    body = {
        "tenant_id": "search",
        "application_id": "search-rag-prod",
        "auth_type": "iam",
        "principal_arn": "arn:aws:iam::123456789012:role/SearchRagProd",
        "requested_models": ["us.amazon.nova-lite-v1:0"],
        "rpm_limit": 500,
        "monthly_budget": 10000,
    }
    body.update(overrides)
    resp = client.post("/v1/admin/onboarding-requests", json=body, headers=_dev_headers(fixture))
    assert resp.status_code == 201, resp.text
    return resp.json()["request_id"]


class SubmitOnboardingRequestTests(unittest.TestCase):
    def test_submit_returns_pending_approval(self):
        client, fixture = _app()

        request_id = _submit(client, fixture)

        detail = client.get(f"/v1/admin/onboarding-requests/{request_id}", headers=_admin_headers(fixture))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "PENDING_APPROVAL")
        self.assertEqual(detail.json()["history"][0]["event"], "ONBOARDING_SUBMITTED")

    def test_iam_auth_type_requires_principal_arn(self):
        client, fixture = _app()

        resp = client.post(
            "/v1/admin/onboarding-requests",
            json={"tenant_id": "search", "application_id": "x", "auth_type": "iam"},
            headers=_dev_headers(fixture),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_REQUEST")

    def test_invalid_auth_type_rejected(self):
        client, fixture = _app()

        resp = client.post(
            "/v1/admin/onboarding-requests",
            json={"tenant_id": "search", "application_id": "x", "auth_type": "saml"},
            headers=_dev_headers(fixture),
        )

        self.assertEqual(resp.status_code, 400)


class ApproveOnboardingRequestTests(unittest.TestCase):
    def test_approve_existing_tenant_only_adds_principal_mapping(self):
        policy_store = InMemoryPolicyStore(
            {"search": TenantPolicy(tenant_id="search", state=TenantState.ACTIVE, rpm_limit=120)}
        )
        client, fixture = _app(policy_store=policy_store)
        request_id = _submit(client, fixture)

        resp = client.post(f"/v1/admin/onboarding-requests/{request_id}/approve", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ACTIVE")
        # The pre-existing tenant's own policy (rpm_limit=120) must be
        # untouched -- onboarding never overwrites a hand-managed tenant.
        self.assertEqual(policy_store.get("search").rpm_limit, 120)

        apps = client.get("/v1/admin/applications", headers=_admin_headers(fixture)).json()["applications"]
        arns = {a["principal_arn"]: a for a in apps}
        self.assertIn("arn:aws:iam::123456789012:role/SearchRagProd", arns)
        self.assertEqual(arns["arn:aws:iam::123456789012:role/SearchRagProd"]["tenant_id"], "search")

    def test_approve_new_tenant_creates_policy(self):
        client, fixture = _app()
        request_id = _submit(
            client, fixture, tenant_id="marketing", application_id="copy-gen",
            principal_arn="arn:aws:iam::123456789012:role/CopyGen",
        )

        resp = client.post(f"/v1/admin/onboarding-requests/{request_id}/approve", headers=_admin_headers(fixture))
        self.assertEqual(resp.status_code, 200)

        tenants = {t["tenant_id"]: t for t in client.get("/v1/admin/tenants", headers=_admin_headers(fixture)).json()["tenants"]}
        self.assertIn("marketing", tenants)
        self.assertEqual(tenants["marketing"]["rpm_limit"], 500)
        self.assertEqual(tenants["marketing"]["monthly_budget"], 10000.0)

    def test_approve_duplicate_principal_arn_fails_and_marks_request_failed(self):
        client, fixture = _app()
        first = _submit(client, fixture)
        client.post(f"/v1/admin/onboarding-requests/{first}/approve", headers=_admin_headers(fixture))

        second = _submit(client, fixture, application_id="search-rag-prod-2")
        resp = client.post(f"/v1/admin/onboarding-requests/{second}/approve", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "PROVISIONING_FAILED")
        detail = client.get(f"/v1/admin/onboarding-requests/{second}", headers=_admin_headers(fixture)).json()
        self.assertEqual(detail["status"], "FAILED")

    def test_approve_is_admin_only(self):
        client, fixture = _app()
        request_id = _submit(client, fixture)

        resp = client.post(f"/v1/admin/onboarding-requests/{request_id}/approve", headers=_dev_headers(fixture))

        self.assertEqual(resp.status_code, 403)

    def test_double_approve_is_rejected(self):
        client, fixture = _app()
        request_id = _submit(client, fixture)
        client.post(f"/v1/admin/onboarding-requests/{request_id}/approve", headers=_admin_headers(fixture))

        resp = client.post(f"/v1/admin/onboarding-requests/{request_id}/approve", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_ONBOARDING_TRANSITION")

    def test_unknown_request_id_404s(self):
        client, fixture = _app()

        resp = client.post("/v1/admin/onboarding-requests/req-doesnotexist/approve", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 404)


class RejectOnboardingRequestTests(unittest.TestCase):
    def test_reject_records_reason(self):
        client, fixture = _app()
        request_id = _submit(client, fixture)

        resp = client.post(
            f"/v1/admin/onboarding-requests/{request_id}/reject",
            json={"reason": "needs security review"},
            headers=_admin_headers(fixture),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "REJECTED")
        self.assertEqual(resp.json()["reason"], "needs security review")

    def test_reject_is_admin_only(self):
        client, fixture = _app()
        request_id = _submit(client, fixture)

        resp = client.post(
            f"/v1/admin/onboarding-requests/{request_id}/reject",
            json={"reason": "x"},
            headers=_dev_headers(fixture),
        )

        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
