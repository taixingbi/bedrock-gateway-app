"""JWT verification (M1).

Two implementations of the same `TokenVerifier` Protocol -- the seam
`ConverseClient` established in M0 (real class + fake/local class, same
interface, callers don't care which):

- `JwksVerifier`: real OIDC verification. Fetches the provider's JWKS,
  caches keys by `kid` with a TTL, verifies signature/issuer/audience/
  expiry via PyJWT. This is what a real deployment uses.
- `StaticKeyVerifier`: verifies against one fixed public key, no network
  call. Used for local dev (paired with `devkeys.py` +
  `scripts/generate_dev_token.py`) and for tests.

Both raise `AuthError` (never a bare exception) on any failure so the
route layer can turn it into a 401 without knowing anything about JWKS/
PyJWT internals.
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Callable, Dict, Optional, Protocol

from .identity import AuthError


class TokenVerifier(Protocol):
    def verify(self, token: str) -> dict:
        """Return verified claims, or raise AuthError."""
        ...


def _default_http_get(url: str, timeout_s: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:  # noqa: S310 (fixed https jwks_url from config, not user input)
        return resp.read()


class JwksVerifier:
    """Verifies RS256 tokens against a provider's published JWKS."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        cache_ttl_s: float = 300.0,
        http_get: Optional[Callable[[str], bytes]] = None,
    ):
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._cache_ttl_s = cache_ttl_s
        self._http_get = http_get or _default_http_get
        self._keys_by_kid: Dict[str, object] = {}
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        try:
            raw = self._http_get(self._jwks_url)
            jwks = json.loads(raw)
        except Exception as exc:
            raise AuthError(
                f"unable to fetch JWKS from {self._jwks_url}: {exc}",
                code="IDENTITY_PROVIDER_UNAVAILABLE",
            ) from exc

        import jwt

        self._keys_by_kid = {
            k["kid"]: jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(k)) for k in jwks.get("keys", [])
        }
        self._fetched_at = time.monotonic()

    def _refresh_if_stale(self) -> None:
        if not self._keys_by_kid or (time.monotonic() - self._fetched_at) >= self._cache_ttl_s:
            self._refresh()

    def verify(self, token: str) -> dict:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except Exception as exc:
            raise AuthError(f"malformed token: {exc}") from exc

        kid = header.get("kid")
        self._refresh_if_stale()
        key = self._keys_by_kid.get(kid)
        if key is None:
            # kid may have rotated since our last fetch -- refresh once more
            # before giving up, rather than caching a permanent miss.
            self._refresh()
            key = self._keys_by_kid.get(kid)
        if key is None:
            raise AuthError(f"unknown signing key id: {kid!r}")

        return _decode(jwt, token, key, issuer=self._issuer, audience=self._audience)


class StaticKeyVerifier:
    """Verifies RS256 tokens against one fixed public key. Dev/test only."""

    def __init__(self, *, public_key_pem: str, issuer: str, audience: str):
        self._public_key_pem = public_key_pem
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> dict:
        import jwt

        return _decode(jwt, token, self._public_key_pem, issuer=self._issuer, audience=self._audience)


def _decode(jwt_module, token: str, key, *, issuer: str, audience: str) -> dict:
    try:
        return jwt_module.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat"]},
        )
    except jwt_module.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt_module.InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc}") from exc
