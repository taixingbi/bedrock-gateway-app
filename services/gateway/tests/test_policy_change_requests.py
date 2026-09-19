"""Plan section 33: /v1/admin/tenants/{id}/policy-changes,
.../rollback, .../policy-history -- the HTTP surface for versioned
policy edits with approval and rollback. Store-level logic is covered
in test_policy_versioning.py; this file covers routing, ABAC, and the
manager/admin role split (propose is manager-or-admin, everything else
-- approve/reject/rollback -- is admin-only, separation of duties).
"""
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from ..policy.models import TenantPolicy, TenantState
from ..policy.store import InMemoryPolicyStore
from .auth_fixtures import auth_header, get_auth_fixture
from .fakes import FakeConverseClient


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


def _manager_headers(fixture, tenant_id="acme") -> dict:
    return auth_header(fixture.token(sub="mgr-1", tenant_id=tenant_id, roles=["manager"]))


def _propose(client, fixture, *, tenant_id="acme", headers=None, changes=None, base_policy_epoch=1):
    body = {"changes": changes or {"rpm_limit": 500}, "base_policy_epoch": base_policy_epoch}
    resp = client.post(
        f"/v1/admin/tenants/{tenant_id}/policy-changes", json=body, headers=headers
    )
    return resp


class ProposePolicyChangeTests(unittest.TestCase):
    def test_manager_can_propose_for_their_own_tenant(self):
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = _propose(client, fixture, headers=_manager_headers(fixture))

        self.assertEqual(resp.status_code, 201, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "PENDING_APPROVAL")
        self.assertEqual(body["changes"], {"rpm_limit": 500})
        # nothing applied yet
        self.assertEqual(primary.get("acme").rpm_limit, 60)

    def test_manager_cannot_propose_for_another_tenant(self):
        primary = InMemoryPolicyStore(
            {"acme": _policy(tenant_id="acme", policy_epoch=1), "other": _policy(tenant_id="other", policy_epoch=1)}
        )
        client, fixture = _app(policy_store_primary=primary)

        resp = _propose(client, fixture, tenant_id="other", headers=_manager_headers(fixture, "acme"))

        self.assertEqual(resp.status_code, 403)

    def test_admin_can_propose_for_any_tenant(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = _propose(client, fixture, headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 201)

    def test_unauthenticated_role_forbidden(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        dev_token = fixture.token(sub="dev-1", tenant_id="acme", roles=["developer"])

        resp = _propose(client, fixture, headers=auth_header(dev_token))

        self.assertEqual(resp.status_code, 403)

    def test_invalid_field_is_400(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = _propose(
            client, fixture, headers=_manager_headers(fixture), changes={"tenant_id": "other"}
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_POLICY_CHANGE")

    def test_unknown_tenant_is_404(self):
        primary = InMemoryPolicyStore({})
        client, fixture = _app(policy_store_primary=primary)

        resp = _propose(client, fixture, tenant_id="ghost", headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 404)

    def test_file_managed_tenant_not_in_primary_is_404(self):
        """A hand-managed (YAML/file) tenant that isn't in the
        *primary* provisioned store must not be proposable-against --
        same invariant onboarding/provisioning.py enforces for create()."""
        client, fixture = _app()  # default tenants.yaml fixture, empty primary
        tenant_ids = client.get(
            "/v1/admin/tenants", headers=_admin_headers(fixture)
        ).json()["tenants"]
        self.assertTrue(tenant_ids)  # sanity: file tenants do exist
        file_tenant_id = tenant_ids[0]["tenant_id"]

        resp = _propose(client, fixture, tenant_id=file_tenant_id, headers=_admin_headers(fixture))

        self.assertEqual(resp.status_code, 404)


class ApproveRejectPolicyChangeTests(unittest.TestCase):
    def test_admin_approve_applies_change_and_invalidates_cache(self):
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture)
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["status"], "APPLIED")
        self.assertEqual(primary.get("acme").rpm_limit, 500)
        self.assertEqual(primary.get("acme").policy_epoch, 2)

    def test_manager_cannot_approve(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_manager_headers(fixture)
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(primary.get("acme").rpm_limit, 60)

    def test_double_approve_is_409(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]
        client.post(f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture))

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture)
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "INVALID_POLICY_CHANGE_TRANSITION")

    def test_approve_with_stale_base_epoch_is_epoch_conflict(self):
        """Another change landed (or set_state fired) between propose
        and approve -- the approval must not silently clobber it."""
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]
        primary.set_state("acme", TenantState.SUSPENDED)  # epoch 1 -> 2, intervening write

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture)
        )

        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error"]["code"], "POLICY_EPOCH_CONFLICT")

    def test_admin_reject_records_reason_and_does_not_apply(self):
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/reject",
            json={"reason": "budget not approved"},
            headers=_admin_headers(fixture),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "REJECTED")
        self.assertEqual(resp.json()["reason"], "budget not approved")
        self.assertEqual(primary.get("acme").rpm_limit, 60)

    def test_manager_cannot_reject(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]

        resp = client.post(
            f"/v1/admin/tenants/acme/policy-changes/{change_id}/reject",
            json={"reason": "x"},
            headers=_manager_headers(fixture),
        )

        self.assertEqual(resp.status_code, 403)

    def test_unknown_change_id_is_404(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = client.post(
            "/v1/admin/tenants/acme/policy-changes/pchg-doesnotexist/approve",
            headers=_admin_headers(fixture),
        )

        self.assertEqual(resp.status_code, 404)


class ListPolicyChangesTests(unittest.TestCase):
    def test_manager_lists_their_own_tenants_changes(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        _propose(client, fixture, headers=_manager_headers(fixture))
        _propose(client, fixture, headers=_manager_headers(fixture), changes={"rpm_limit": 999})

        resp = client.get("/v1/admin/tenants/acme/policy-changes", headers=_manager_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["changes"]), 2)

    def test_manager_cannot_list_another_tenants_changes(self):
        primary = InMemoryPolicyStore(
            {"acme": _policy(tenant_id="acme", policy_epoch=1), "other": _policy(tenant_id="other", policy_epoch=1)}
        )
        client, fixture = _app(policy_store_primary=primary)

        resp = client.get(
            "/v1/admin/tenants/other/policy-changes", headers=_manager_headers(fixture, "acme")
        )

        self.assertEqual(resp.status_code, 403)


class RollbackPolicyTests(unittest.TestCase):
    def test_admin_rollback_restores_prior_version_as_new_epoch(self):
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]
        client.post(f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture))
        self.assertEqual(primary.get("acme").rpm_limit, 500)  # sanity: applied

        resp = client.post(
            "/v1/admin/tenants/acme/rollback", json={"target_epoch": 1}, headers=_admin_headers(fixture)
        )

        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["policy_epoch"], 3)  # forward, not back to 1
        self.assertEqual(primary.get("acme").rpm_limit, 60)

    def test_manager_cannot_rollback(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = client.post(
            "/v1/admin/tenants/acme/rollback", json={"target_epoch": 1}, headers=_manager_headers(fixture)
        )

        self.assertEqual(resp.status_code, 403)

    def test_unknown_target_epoch_is_404(self):
        primary = InMemoryPolicyStore({"acme": _policy(policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)

        resp = client.post(
            "/v1/admin/tenants/acme/rollback", json={"target_epoch": 99}, headers=_admin_headers(fixture)
        )

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error"]["code"], "NO_PRIOR_POLICY_VERSION")


class PolicyHistoryTests(unittest.TestCase):
    def test_returns_prior_versions_newest_first(self):
        primary = InMemoryPolicyStore({"acme": _policy(rpm_limit=60, policy_epoch=1)})
        client, fixture = _app(policy_store_primary=primary)
        change_id = _propose(client, fixture, headers=_manager_headers(fixture)).json()["change_id"]
        client.post(f"/v1/admin/tenants/acme/policy-changes/{change_id}/approve", headers=_admin_headers(fixture))

        resp = client.get("/v1/admin/tenants/acme/policy-history", headers=_manager_headers(fixture))

        self.assertEqual(resp.status_code, 200)
        history = resp.json()["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["policy_epoch"], 1)
        self.assertEqual(history[0]["rpm_limit"], 60)

    def test_manager_cannot_see_another_tenants_history(self):
        primary = InMemoryPolicyStore(
            {"acme": _policy(tenant_id="acme", policy_epoch=1), "other": _policy(tenant_id="other", policy_epoch=1)}
        )
        client, fixture = _app(policy_store_primary=primary)

        resp = client.get(
            "/v1/admin/tenants/other/policy-history", headers=_manager_headers(fixture, "acme")
        )

        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
