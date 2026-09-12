import tempfile
import textwrap
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from .. import pipeline
from ..auth.aws_iam import HEADER_ACCOUNT_ID, HEADER_PRINCIPAL_ARN, FileIamTenantResolver, IamPrincipalGrant
from ..auth.identity import AuthError
from ..config import load_settings
from ..main import create_app
from .auth_fixtures import get_auth_fixture
from .fakes import FakeConverseClient

_KNOWN_ARN = "arn:aws:iam::646821141010:role/team-a-ai-client"
_KNOWN_ASSUMED_ROLE_ARN = "arn:aws:sts::646821141010:assumed-role/team-a-ai-client/some-session"
_UNKNOWN_ARN = "arn:aws:iam::646821141010:role/nobody-maps-this"


class _FakeIamTenantResolver:
    """In-memory stand-in for FileIamTenantResolver -- no filesystem I/O,
    same Protocol (`resolve(principal_arn) -> IamPrincipalGrant`)."""

    def __init__(self, grants: dict):
        self._grants = grants

    def resolve(self, principal_arn: str) -> IamPrincipalGrant:
        grant = self._grants.get(principal_arn)
        if grant is None:
            raise AuthError(
                f"no tenant mapping for IAM principal '{principal_arn}'", code="UNKNOWN_IAM_PRINCIPAL"
            )
        return grant


def _resolver() -> _FakeIamTenantResolver:
    grant = IamPrincipalGrant(tenant_id="team-a", application_id="team-a-ai-client", roles=["developer"])
    return _FakeIamTenantResolver({_KNOWN_ARN: grant})


class AuthenticateIamUnitTests(unittest.TestCase):
    """Direct pipeline.authenticate_iam()/authenticate() tests -- no HTTP,
    no AWS, mirrors the resolve_policy()-style unit tests in test_policy.py."""

    def test_known_principal_resolves_to_identity(self):
        identity = pipeline.authenticate_iam(
            _KNOWN_ARN, "646821141010", iam_tenant_resolver=_resolver()
        )

        self.assertEqual(identity.sub, _KNOWN_ARN)
        self.assertEqual(identity.tenant_id, "team-a")
        self.assertEqual(identity.application_id, "team-a-ai-client")
        self.assertEqual(identity.roles, ["developer"])
        self.assertEqual(identity.auth_type, "aws_iam")
        self.assertEqual(identity.account_id, "646821141010")

    def test_unknown_principal_is_403(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.authenticate_iam(_UNKNOWN_ARN, "646821141010", iam_tenant_resolver=_resolver())

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.code, "UNKNOWN_IAM_PRINCIPAL")

    def test_authenticate_dispatches_to_iam_path_when_principal_arn_present(self):
        # No bearer token at all -- iam_principal_arn alone must be enough,
        # proving the IAM branch is checked before the JWT branch.
        identity = pipeline.authenticate(
            None,
            token_verifier=get_auth_fixture().verifier,
            iam_principal_arn=_KNOWN_ARN,
            iam_account_id="646821141010",
            iam_tenant_resolver=_resolver(),
        )

        self.assertEqual(identity.auth_type, "aws_iam")
        self.assertEqual(identity.tenant_id, "team-a")

    def test_authenticate_falls_back_to_jwt_when_no_principal_arn(self):
        fixture = get_auth_fixture()
        token = fixture.token(tenant_id="finance")

        identity = pipeline.authenticate(
            f"Bearer {token}", token_verifier=fixture.verifier, iam_tenant_resolver=_resolver()
        )

        self.assertEqual(identity.auth_type, "jwt")
        self.assertEqual(identity.tenant_id, "finance")

    def test_iam_principal_arn_without_resolver_configured_is_500(self):
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.authenticate(
                None, token_verifier=get_auth_fixture().verifier, iam_principal_arn=_KNOWN_ARN
            )

        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.code, "IAM_AUTH_NOT_CONFIGURED")


class FileIamTenantResolverTests(unittest.TestCase):
    """Tests the real YAML-file-backed resolver (policies/iam_tenants.yaml's
    actual implementation), separate from the in-memory fake used above."""

    def _write(self, contents: str) -> str:
        tmpdir = tempfile.mkdtemp()
        path = Path(tmpdir) / "iam_tenants.yaml"
        path.write_text(textwrap.dedent(contents))
        return str(path)

    def test_exact_match(self):
        path = self._write(
            """
            iam_principals:
              "arn:aws:iam::646821141010:role/team-a-ai-client":
                tenant_id: team-a
                application_id: team-a-ai-client
                roles: [developer]
            """
        )
        resolver = FileIamTenantResolver(path)

        grant = resolver.resolve(_KNOWN_ARN)

        self.assertEqual(grant.tenant_id, "team-a")
        self.assertEqual(grant.application_id, "team-a-ai-client")
        self.assertEqual(grant.roles, ["developer"])

    def test_wildcard_suffix_match_for_assumed_role_session_names(self):
        path = self._write(
            """
            iam_principals:
              "arn:aws:sts::646821141010:assumed-role/team-a-ai-client/*":
                tenant_id: team-a
                application_id: team-a-ai-client
                roles: [developer]
            """
        )
        resolver = FileIamTenantResolver(path)

        # Two different session names both match the same wildcard entry.
        grant_a = resolver.resolve("arn:aws:sts::646821141010:assumed-role/team-a-ai-client/session-a")
        grant_b = resolver.resolve("arn:aws:sts::646821141010:assumed-role/team-a-ai-client/session-b")

        self.assertEqual(grant_a.tenant_id, "team-a")
        self.assertEqual(grant_b.tenant_id, "team-a")

    def test_exact_match_takes_precedence_over_wildcard(self):
        path = self._write(
            """
            iam_principals:
              "arn:aws:sts::646821141010:assumed-role/team-a-ai-client/*":
                tenant_id: team-a
                application_id: team-a-ai-client
                roles: [developer]
              "arn:aws:sts::646821141010:assumed-role/team-a-ai-client/pinned-session":
                tenant_id: team-a-pinned
                application_id: team-a-ai-client
                roles: [developer]
            """
        )
        resolver = FileIamTenantResolver(path)

        grant = resolver.resolve("arn:aws:sts::646821141010:assumed-role/team-a-ai-client/pinned-session")

        self.assertEqual(grant.tenant_id, "team-a-pinned")

    def test_unknown_principal_raises_auth_error(self):
        path = self._write(
            """
            iam_principals:
              "arn:aws:iam::646821141010:role/team-a-ai-client":
                tenant_id: team-a
                application_id: team-a-ai-client
                roles: [developer]
            """
        )
        resolver = FileIamTenantResolver(path)

        with self.assertRaises(AuthError) as ctx:
            resolver.resolve(_UNKNOWN_ARN)

        self.assertEqual(ctx.exception.code, "UNKNOWN_IAM_PRINCIPAL")

    def test_missing_file_is_tolerated_as_empty_mapping(self):
        resolver = FileIamTenantResolver("/nonexistent/path/iam_tenants.yaml")

        with self.assertRaises(AuthError):
            resolver.resolve(_KNOWN_ARN)


def _client() -> TestClient:
    settings = load_settings()
    fixture = get_auth_fixture()
    app = create_app(
        settings=settings,
        converse_client=FakeConverseClient(),
        token_verifier=fixture.verifier,
        iam_tenant_resolver=_resolver(),
    )
    return TestClient(app)


class ChatEndpointIamAuthTests(unittest.TestCase):
    """Full-stack (TestClient) equivalent of test_auth.py's JWT tests, for
    the AWS_IAM path -- these headers are only trustworthy in production
    because API Gateway's VPC Link is the only way to reach this app (see
    auth/aws_iam.py); here we're just proving the app-side dispatch and
    tenant mapping behave correctly given those headers."""

    def test_known_iam_principal_succeeds_with_no_bearer_token(self):
        client = _client()

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={HEADER_PRINCIPAL_ARN: _KNOWN_ARN, HEADER_ACCOUNT_ID: "646821141010"},
        )

        self.assertEqual(resp.status_code, 200)

    def test_assumed_role_session_suffix_is_not_matched_without_wildcard_entry(self):
        # _resolver() only maps the exact role ARN, not the
        # assumed-role/*/<session> wildcard form -- confirms the fake
        # behaves like an exact-match-only mapping unless told otherwise,
        # matching FileIamTenantResolverWildcardTests below for the real
        # file-backed resolver.
        client = _client()

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={HEADER_PRINCIPAL_ARN: _KNOWN_ASSUMED_ROLE_ARN, HEADER_ACCOUNT_ID: "646821141010"},
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "UNKNOWN_IAM_PRINCIPAL")

    def test_unknown_iam_principal_is_403(self):
        client = _client()

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={HEADER_PRINCIPAL_ARN: _UNKNOWN_ARN, HEADER_ACCOUNT_ID: "646821141010"},
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "UNKNOWN_IAM_PRINCIPAL")


if __name__ == "__main__":
    unittest.main()
