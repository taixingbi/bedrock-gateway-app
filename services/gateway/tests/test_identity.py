import unittest

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


if __name__ == "__main__":
    unittest.main()
