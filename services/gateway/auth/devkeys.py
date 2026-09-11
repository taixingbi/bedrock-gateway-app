"""Local RSA keypair + token minting for dev/tests.

Not a substitute for a real OIDC provider -- `JwksVerifier` (fetching a
real JWKS over HTTPS) is what runs against one. This module exists purely
so the gateway is end-to-end runnable and curl-able without one: the
running server and `scripts/generate_dev_token.py` both read/create the
same keypair file, so a token minted by the script verifies against the
server's `StaticKeyVerifier`.

The keypair file is dev material, not a production secret, but it's still
gitignored (`.dev/`) so nobody accidentally treats it as one.
"""
from __future__ import annotations

import json
import os
import time
from typing import List


def generate_dev_keypair() -> tuple[str, str]:
    """Returns (private_key_pem, public_key_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def load_or_create_dev_keypair(path: str) -> tuple[str, str]:
    """Load a persisted (private_pem, public_pem) pair, generating and
    saving one on first use so subsequent server/script runs agree."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data["private_key"], data["public_key"]

    private_pem, public_pem = generate_dev_keypair()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"private_key": private_pem, "public_key": public_pem}, f)
    return private_pem, public_pem


def mint_dev_token(
    *,
    private_key_pem: str,
    issuer: str,
    audience: str,
    sub: str,
    tenant_id: str,
    application_id: str,
    roles: List[str],
    ttl_s: float = 3600.0,
) -> str:
    import jwt

    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + int(ttl_s),
        "tenant_id": tenant_id,
        "application_id": application_id,
        "roles": roles,
    }
    return jwt.encode(claims, private_key_pem, algorithm="RS256")
