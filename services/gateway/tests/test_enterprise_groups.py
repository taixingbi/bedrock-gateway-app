"""Plan section 34.2: EnterpriseGroupResolver -- the OIDC-group
equivalent of auth/aws_iam.py's FileIamTenantResolver. Claim-shape
integration (identity_from_claims + enterprise_group_resolver) is
covered in test_identity.py; this file covers the resolver in
isolation (ambiguity rules, file loading).
"""
import tempfile
import unittest
from pathlib import Path

from ..auth.enterprise_groups import (
    EnterpriseGroupGrant,
    FileEnterpriseGroupResolver,
    InMemoryEnterpriseGroupResolver,
)
from ..auth.identity import AuthError


class InMemoryEnterpriseGroupResolverTests(unittest.TestCase):
    def test_single_matched_group_resolves(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {"G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=["developer"])}
        )

        resolved = resolver.resolve(["G1"])

        self.assertEqual(resolved.tenant_id, "acme")
        self.assertEqual(resolved.application_id, "app1")
        self.assertEqual(resolved.roles, ["developer"])

    def test_extra_unmapped_groups_in_token_are_ignored(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {"G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=["developer"])}
        )

        resolved = resolver.resolve(["Some-Unrelated-Corp-Group", "G1"])

        self.assertEqual(resolved.tenant_id, "acme")

    def test_no_matched_group_raises(self):
        resolver = InMemoryEnterpriseGroupResolver({})

        with self.assertRaises(AuthError) as ctx:
            resolver.resolve(["G1"])
        self.assertEqual(ctx.exception.code, "UNKNOWN_ENTERPRISE_GROUP")

    def test_conflicting_tenant_ids_raises(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {
                "G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=[]),
                "G2": EnterpriseGroupGrant(tenant_id="other", application_id="app1", roles=[]),
            }
        )

        with self.assertRaises(AuthError) as ctx:
            resolver.resolve(["G1", "G2"])
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_ENTERPRISE_GROUP_TENANT")

    def test_conflicting_application_ids_raises(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {
                "G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=[]),
                "G2": EnterpriseGroupGrant(tenant_id="acme", application_id="app2", roles=[]),
            }
        )

        with self.assertRaises(AuthError) as ctx:
            resolver.resolve(["G1", "G2"])
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_ENTERPRISE_GROUP_APPLICATION")

    def test_roles_are_unioned_without_duplicates(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {
                "G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=["developer"]),
                "G2": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=["developer", "manager"]),
            }
        )

        resolved = resolver.resolve(["G1", "G2"])

        self.assertEqual(resolved.roles, ["developer", "manager"])

    def test_list_mappings_returns_configured_grants(self):
        resolver = InMemoryEnterpriseGroupResolver(
            {"G1": EnterpriseGroupGrant(tenant_id="acme", application_id="app1", roles=["developer"])}
        )

        mappings = resolver.list_mappings()

        self.assertIn("G1", mappings)
        self.assertEqual(mappings["G1"].tenant_id, "acme")


class FileEnterpriseGroupResolverTests(unittest.TestCase):
    def test_loads_yaml_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "enterprise_groups.yaml"
            path.write_text(
                """
enterprise_groups:
  "AI-Gateway-Claims-Manager":
    tenant_id: claims
    application_id: claims-portal
    roles: [manager]
"""
            )
            resolver = FileEnterpriseGroupResolver(str(path))

            resolved = resolver.resolve(["AI-Gateway-Claims-Manager"])

            self.assertEqual(resolved.tenant_id, "claims")
            self.assertEqual(resolved.roles, ["manager"])

    def test_missing_file_is_empty_mapping_not_an_error(self):
        resolver = FileEnterpriseGroupResolver("/no/such/file.yaml")

        with self.assertRaises(AuthError):
            resolver.resolve(["anything"])

    def test_empty_path_is_empty_mapping(self):
        resolver = FileEnterpriseGroupResolver("")

        with self.assertRaises(AuthError):
            resolver.resolve(["anything"])


if __name__ == "__main__":
    unittest.main()
