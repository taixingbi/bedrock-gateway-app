#!/usr/bin/env python3
"""Mint a local dev JWT for curl-testing the gateway without a real OIDC
provider (M1). Reads/creates the same keypair file the running server
uses (see services/gateway/auth/devkeys.py) so a token minted here
verifies against the server's StaticKeyVerifier.

Usage:
    python scripts/generate_dev_token.py \
        --tenant-id finance --application-id risk-chat --roles developer

    curl -s http://localhost:8080/v1/chat \
      -H "authorization: Bearer $(python scripts/generate_dev_token.py -q)" \
      -H 'content-type: application/json' \
      -d '{"messages":[{"role":"user","content":"hi"}]}'
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.gateway.auth.devkeys import load_or_create_dev_keypair, mint_dev_token  # noqa: E402
from services.gateway.config import load_settings  # noqa: E402


def main() -> None:
    settings = load_settings()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sub", default="dev-user")
    parser.add_argument("--tenant-id", default="finance")
    parser.add_argument("--application-id", default="risk-chat")
    parser.add_argument("--roles", nargs="+", default=[settings.chat_required_role])
    parser.add_argument("--ttl-s", type=float, default=3600.0)
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print only the raw token (for use in $(...))"
    )
    args = parser.parse_args()

    if settings.oidc_jwks_url:
        print(
            "warning: OIDC_JWKS_URL is set -- the running server verifies against a "
            "real provider, so this locally-signed dev token will be rejected.",
            file=sys.stderr,
        )

    private_pem, _public_pem = load_or_create_dev_keypair(settings.dev_jwt_keypair_path)
    token = mint_dev_token(
        private_key_pem=private_pem,
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        sub=args.sub,
        tenant_id=args.tenant_id,
        application_id=args.application_id,
        roles=args.roles,
        ttl_s=args.ttl_s,
    )

    if args.quiet:
        print(token)
    else:
        print(f"tenant_id={args.tenant_id} application_id={args.application_id} roles={args.roles}")
        print(token)


if __name__ == "__main__":
    main()
