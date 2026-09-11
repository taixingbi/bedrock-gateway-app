"""Shared JWT test fixtures.

Mints real, cryptographically valid RS256 tokens against an in-memory
keypair and a matching StaticKeyVerifier -- no filesystem, no network, no
real OIDC provider. RSA keygen is done once per test process (module-level
cache) since it's the slow part; minting individual tokens from an
existing key is cheap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..auth.devkeys import generate_dev_keypair, mint_dev_token
from ..auth.jwt_verifier import StaticKeyVerifier

ISSUER = "https://dev-issuer.local/"
AUDIENCE = "bedrock-gateway"


@dataclass
class AuthFixture:
    verifier: StaticKeyVerifier
    private_key_pem: str

    def token(
        self,
        *,
        sub: str = "user-1",
        tenant_id: str = "finance",
        application_id: str = "risk-chat",
        roles: Optional[List[str]] = None,
        ttl_s: float = 3600.0,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
    ) -> str:
        return mint_dev_token(
            private_key_pem=self.private_key_pem,
            issuer=issuer if issuer is not None else ISSUER,
            audience=audience if audience is not None else AUDIENCE,
            sub=sub,
            tenant_id=tenant_id,
            application_id=application_id,
            roles=roles if roles is not None else ["developer"],
            ttl_s=ttl_s,
        )


_FIXTURE: Optional[AuthFixture] = None


def get_auth_fixture() -> AuthFixture:
    global _FIXTURE
    if _FIXTURE is None:
        private_pem, public_pem = generate_dev_keypair()
        verifier = StaticKeyVerifier(public_key_pem=public_pem, issuer=ISSUER, audience=AUDIENCE)
        _FIXTURE = AuthFixture(verifier=verifier, private_key_pem=private_pem)
    return _FIXTURE


def auth_header(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}
