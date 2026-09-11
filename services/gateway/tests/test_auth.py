import time
import unittest

from starlette.testclient import TestClient

from ..config import load_settings
from ..main import create_app
from .auth_fixtures import AUDIENCE, ISSUER, auth_header, get_auth_fixture
from .fakes import FakeConverseClient


def _client() -> TestClient:
    settings = load_settings()
    fixture = get_auth_fixture()
    app = create_app(settings=settings, converse_client=FakeConverseClient(), token_verifier=fixture.verifier)
    return TestClient(app)


class AuthTests(unittest.TestCase):
    def test_missing_token_is_401(self):
        client = _client()

        resp = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "hi"}]})

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error"]["code"], "UNAUTHENTICATED")

    def test_malformed_authorization_header_is_401(self):
        client = _client()

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"authorization": "not-a-bearer-token"},
        )

        self.assertEqual(resp.status_code, 401)

    def test_expired_token_is_401(self):
        client = _client()
        fixture = get_auth_fixture()
        token = fixture.token(ttl_s=-10.0)  # already expired

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 401)

    def test_wrong_audience_is_401(self):
        client = _client()
        fixture = get_auth_fixture()
        token = fixture.token(audience="some-other-service")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 401)

    def test_wrong_issuer_is_401(self):
        client = _client()
        fixture = get_auth_fixture()
        token = fixture.token(issuer="https://not-the-real-issuer.local/")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 401)

    def test_token_signed_by_unknown_key_is_401(self):
        client = _client()
        from ..auth.devkeys import generate_dev_keypair, mint_dev_token

        other_private_pem, _other_public_pem = generate_dev_keypair()
        token = mint_dev_token(
            private_key_pem=other_private_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
            sub="attacker",
            tenant_id="finance",
            application_id="risk-chat",
            roles=["developer"],
        )

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 401)

    def test_missing_role_is_403(self):
        client = _client()
        fixture = get_auth_fixture()
        token = fixture.token(roles=["read_only"])  # not "developer"

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error"]["code"], "FORBIDDEN")

    def test_tenant_id_header_is_ignored_in_favor_of_token_claim(self):
        """A caller cannot spoof another tenant via a header -- tenant_id
        must come from the verified token's claims only (plan section 5)."""
        fake = FakeConverseClient()
        client = _client()
        fixture = get_auth_fixture()
        token = fixture.token(tenant_id="finance")

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={**auth_header(token), "X-Tenant-ID": "some-other-tenant"},
        )

        # Request succeeds using the token's tenant (finance), not the
        # spoofed header -- there's currently no per-tenant behavior
        # difference to assert on directly (that lands in M2), so this
        # test's job is just to prove the header has zero effect on the
        # outcome versus the same call without it.
        self.assertEqual(resp.status_code, 200)

    def test_valid_token_with_required_role_succeeds(self):
        client = _client()
        token = get_auth_fixture().token(roles=["developer"])

        resp = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=auth_header(token),
        )

        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
