import unittest

from ..auth.identity import AuthorizationError, Identity
from ..auth.rbac import require_any_role, require_role, require_tenant_match_or_role


def _identity(**overrides) -> Identity:
    defaults = dict(sub="user-1", tenant_id="acme", application_id="app-1", roles=["developer"])
    defaults.update(overrides)
    return Identity(**defaults)


class RequireRoleTests(unittest.TestCase):
    def test_passes_when_role_present(self):
        require_role(_identity(roles=["developer"]), "developer")

    def test_raises_when_role_absent(self):
        with self.assertRaises(AuthorizationError):
            require_role(_identity(roles=["developer"]), "platform_admin")


class RequireAnyRoleTests(unittest.TestCase):
    def test_passes_when_any_role_present(self):
        require_any_role(_identity(roles=["manager"]), "platform_admin", "manager")

    def test_raises_when_none_present(self):
        with self.assertRaises(AuthorizationError):
            require_any_role(_identity(roles=["developer"]), "platform_admin", "manager")


class RequireTenantMatchOrRoleTests(unittest.TestCase):
    """plan section 30 -- the one ABAC primitive: does the identity's
    own tenant own the resource, or does it hold the override role."""

    def test_passes_when_tenant_matches_even_without_override_role(self):
        require_tenant_match_or_role(
            _identity(tenant_id="acme", roles=["manager"]), "acme", override_role="platform_admin"
        )

    def test_passes_when_tenant_differs_but_override_role_present(self):
        require_tenant_match_or_role(
            _identity(tenant_id="acme", roles=["platform_admin"]), "other-tenant", override_role="platform_admin"
        )

    def test_raises_when_tenant_differs_and_no_override_role(self):
        with self.assertRaises(AuthorizationError):
            require_tenant_match_or_role(
                _identity(tenant_id="acme", roles=["manager"]), "other-tenant", override_role="platform_admin"
            )


if __name__ == "__main__":
    unittest.main()
