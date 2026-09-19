"""Plan section 34.3: authz/decision.py's PDP-style decide(). Wraps
auth/rbac.py's existing, tested RBAC/ABAC functions -- these tests
check the wrapping (Decision shape, decision_id uniqueness, exact
message passthrough) not the underlying rules, which test_rbac-style
coverage already owns via pipeline.py's authorize*/test_policy.py's
AdminStateEndpointTests.
"""
import unittest

from ..auth.identity import Identity
from ..authz.decision import Decision, decide


def _identity(**overrides) -> Identity:
    defaults = dict(sub="u1", tenant_id="acme", application_id="app1", roles=["developer"])
    defaults.update(overrides)
    return Identity(**defaults)


class DecideRequiredRoleTests(unittest.TestCase):
    def test_allowed_when_role_present(self):
        decision = decide(_identity(roles=["platform_admin"]), action="x", required_role="platform_admin")

        self.assertTrue(decision.allow)
        self.assertEqual(decision.code, "ALLOWED")

    def test_denied_when_role_absent(self):
        decision = decide(_identity(roles=["developer"]), action="x", required_role="platform_admin")

        self.assertFalse(decision.allow)
        self.assertEqual(decision.code, "FORBIDDEN")
        self.assertIn("platform_admin", decision.reason)


class DecideRequiredRolesTests(unittest.TestCase):
    def test_allowed_when_any_role_matches(self):
        decision = decide(_identity(roles=["manager"]), action="x", required_roles=["platform_admin", "manager"])

        self.assertTrue(decision.allow)

    def test_denied_when_no_role_matches(self):
        decision = decide(_identity(roles=["developer"]), action="x", required_roles=["platform_admin", "manager"])

        self.assertFalse(decision.allow)


class DecideResourceTenantIdTests(unittest.TestCase):
    def test_allowed_for_own_tenant(self):
        decision = decide(
            _identity(tenant_id="acme"), action="x",
            resource_tenant_id="acme", override_role="platform_admin",
        )

        self.assertTrue(decision.allow)

    def test_allowed_for_other_tenant_with_override_role(self):
        decision = decide(
            _identity(tenant_id="acme", roles=["platform_admin"]), action="x",
            resource_tenant_id="other", override_role="platform_admin",
        )

        self.assertTrue(decision.allow)

    def test_denied_for_other_tenant_without_override_role(self):
        decision = decide(
            _identity(tenant_id="acme", roles=["manager"]), action="x",
            resource_tenant_id="other", override_role="platform_admin",
        )

        self.assertFalse(decision.allow)
        self.assertEqual(
            decision.reason,
            "tenant 'other' or role 'platform_admin' is required for this operation",
        )

    def test_missing_override_role_raises_value_error(self):
        with self.assertRaises(ValueError):
            decide(_identity(), action="x", resource_tenant_id="acme")


class DecideShapeTests(unittest.TestCase):
    def test_exactly_one_check_kind_required(self):
        with self.assertRaises(ValueError):
            decide(_identity(), action="x")

    def test_multiple_check_kinds_is_an_error(self):
        with self.assertRaises(ValueError):
            decide(
                _identity(), action="x", required_role="developer",
                resource_tenant_id="acme", override_role="platform_admin",
            )

    def test_decision_id_is_unique_per_call(self):
        d1 = decide(_identity(), action="x", required_role="developer")
        d2 = decide(_identity(), action="x", required_role="developer")

        self.assertNotEqual(d1.decision_id, d2.decision_id)

    def test_action_and_policy_version_carried_through(self):
        decision = decide(
            _identity(roles=["platform_admin"]), action="tenant.state.write",
            required_role="platform_admin", policy_version=7,
        )

        self.assertEqual(decision.action, "tenant.state.write")
        self.assertEqual(decision.policy_version, 7)

    def test_policy_version_defaults_to_none(self):
        decision = decide(_identity(roles=["developer"]), action="x", required_role="developer")

        self.assertIsNone(decision.policy_version)


if __name__ == "__main__":
    unittest.main()
