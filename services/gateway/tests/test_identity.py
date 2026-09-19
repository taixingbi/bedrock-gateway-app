import unittest

from ..auth.enterprise_groups import EnterpriseGroupGrant, InMemoryEnterpriseGroupResolver
from ..auth.identity import AuthError, identity_from_claims


class IdentityFromClaimsTests(unittest.TestCase):
    def test_dev_token_claim_shape(self):
        identity = identity_from_claims(
            {"sub": "u1", "tenant_id": "finance", "application_id": "risk-chat", "roles": ["developer"]}
        )

        self.assertEqual(identity.tenant_id, "finance")
        self.assertEqual(identity.application_id, "risk-chat")
        self.assertEqual(identity.roles, ["developer"])

    def test_cognito_claim_shape(self):
        """Cognito prefixes custom schema attributes with 'custom:' and
        represents group membership as a real array claim
        ('cognito:groups'), not our own invented 'roles' claim."""
        identity = identity_from_claims(
            {
                "sub": "cognito-sub-123",
                "custom:tenant_id": "platform",
                "custom:application_id": "portal",
                "cognito:groups": ["platform_admin"],
            }
        )

        self.assertEqual(identity.tenant_id, "platform")
        self.assertEqual(identity.application_id, "portal")
        self.assertEqual(identity.roles, ["platform_admin"])

    def test_missing_tenant_id_is_rejected_even_with_cognito_shape(self):
        with self.assertRaises(AuthError):
            identity_from_claims({"sub": "u1", "custom:application_id": "portal"})

    def test_no_roles_claim_at_all_defaults_to_empty(self):
        identity = identity_from_claims(
            {"sub": "u1", "tenant_id": "finance", "application_id": "risk-chat"}
        )

        self.assertEqual(identity.roles, [])


class EnterpriseGroupClaimShapeTests(unittest.TestCase):
    """Plan section 34.2: a real enterprise IdP's `groups` claim,
    resolved via an EnterpriseGroupResolver -- only tried when no
    direct tenant_id claim exists."""

    def _resolver(self):
        return InMemoryEnterpriseGroupResolver(
            {
                "AI-Gateway-Claims-Manager": EnterpriseGroupGrant(
                    tenant_id="claims", application_id="claims-portal", roles=["manager"]
                ),
                "AI-Gateway-Claims-Dev": EnterpriseGroupGrant(
                    tenant_id="claims", application_id="claims-portal", roles=["developer"]
                ),
            }
        )

    def test_groups_claim_resolves_when_no_direct_tenant_id(self):
        identity = identity_from_claims(
            {"sub": "okta-user-1", "groups": ["AI-Gateway-Claims-Manager"]},
            enterprise_group_resolver=self._resolver(),
        )

        self.assertEqual(identity.tenant_id, "claims")
        self.assertEqual(identity.application_id, "claims-portal")
        self.assertEqual(identity.roles, ["manager"])

    def test_multiple_matched_groups_union_roles(self):
        identity = identity_from_claims(
            {"sub": "okta-user-1", "groups": ["AI-Gateway-Claims-Manager", "AI-Gateway-Claims-Dev"]},
            enterprise_group_resolver=self._resolver(),
        )

        self.assertEqual(set(identity.roles), {"manager", "developer"})

    def test_direct_tenant_id_claim_takes_precedence_over_groups(self):
        """A token that already carries a direct tenant_id (the plain
        or Cognito shape) must never be overridden by a groups
        mapping -- groups resolution is only a fallback for when no
        direct claim exists."""
        identity = identity_from_claims(
            {
                "sub": "u1", "tenant_id": "finance", "application_id": "risk-chat",
                "groups": ["AI-Gateway-Claims-Manager"],
            },
            enterprise_group_resolver=self._resolver(),
        )

        self.assertEqual(identity.tenant_id, "finance")

    def test_unmapped_group_is_rejected(self):
        with self.assertRaises(AuthError):
            identity_from_claims(
                {"sub": "u1", "groups": ["Some-Other-Group"]},
                enterprise_group_resolver=self._resolver(),
            )

    def test_groups_claim_without_resolver_configured_is_missing_tenant_id(self):
        with self.assertRaises(AuthError):
            identity_from_claims({"sub": "u1", "groups": ["AI-Gateway-Claims-Manager"]})


if __name__ == "__main__":
    unittest.main()
