"""App factory + entrypoint for the gateway-api service (M0).

Run locally:
    python -m services.gateway.main

Run under uvicorn directly (what the Dockerfile does):
    uvicorn services.gateway.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import uuid
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware import Middleware

from .api.admin_routes import build_admin_router
from .api.routes import build_router
from .auth.devkeys import load_or_create_dev_keypair
from .auth.jwt_verifier import JwksVerifier, StaticKeyVerifier, TokenVerifier
from .config import Settings, load_settings
from .guardrails.basic_guardrail import BasicGuardrailClient
from .guardrails.client import GuardrailClient
from .inference.bedrock_client import BedrockClient, ConverseClient
from .policy.cache import PolicySnapshotCache
from .policy.rate_limiter import TokenBucketRateLimiter
from .policy.store import FilePolicyStore, PolicyStore
from .telemetry.logging import configure_logging, get_logger, log_event
from .telemetry.middleware import RequestContextMiddleware

_logger = get_logger("gateway.main")


def _build_default_token_verifier(settings: Settings) -> TokenVerifier:
    if settings.oidc_jwks_url:
        return JwksVerifier(
            jwks_url=settings.oidc_jwks_url,
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            cache_ttl_s=settings.oidc_jwks_cache_ttl_s,
        )
    # No real OIDC provider configured -- local dev keypair (see
    # auth/devkeys.py and scripts/generate_dev_token.py).
    _private_pem, public_pem = load_or_create_dev_keypair(settings.dev_jwt_keypair_path)
    return StaticKeyVerifier(
        public_key_pem=public_pem, issuer=settings.oidc_issuer, audience=settings.oidc_audience
    )


def create_app(
    settings: Optional[Settings] = None,
    converse_client: Optional[ConverseClient] = None,
    token_verifier: Optional[TokenVerifier] = None,
    policy_store: Optional[PolicyStore] = None,
    guardrail_client: Optional[GuardrailClient] = None,
) -> Starlette:
    settings = settings or load_settings()
    configure_logging(settings.service_name, settings.log_level)

    if converse_client is None:
        converse_client = BedrockClient(
            region=settings.aws_region,
            timeout_s=settings.bedrock_timeout_s,
            max_retries=settings.bedrock_max_retries,
        )
    if token_verifier is None:
        token_verifier = _build_default_token_verifier(settings)
    if policy_store is None:
        policy_store = FilePolicyStore(settings.tenant_policy_path)
    if guardrail_client is None:
        guardrail_client = BasicGuardrailClient()

    policy_cache = PolicySnapshotCache(store=policy_store, ttl_s=settings.policy_cache_ttl_s)
    rate_limiter = TokenBucketRateLimiter()

    routes = build_router(
        converse_client=converse_client,
        settings=settings,
        token_verifier=token_verifier,
        policy_cache=policy_cache,
        rate_limiter=rate_limiter,
        guardrail_client=guardrail_client,
    )
    admin_routes = build_admin_router(
        policy_store=policy_store,
        policy_cache=policy_cache,
        settings=settings,
        token_verifier=token_verifier,
    )
    routes = routes + admin_routes

    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        log_event(
            _logger, "ERROR", "unhandled exception",
            request_id=request_id, path=request.url.path, error=str(exc),
        )
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": "internal error", "request_id": request_id}},
            status_code=500,
        )

    app = Starlette(
        routes=routes,
        middleware=[Middleware(RequestContextMiddleware)],
        exception_handlers={Exception: unhandled_error},
    )
    app.state.settings = settings
    return app


# Module-level `app` for `uvicorn services.gateway.main:app`. Constructing a
# real BedrockClient requires boto3 + AWS credentials, so this only works
# where both are present (the container / a properly configured dev box) --
# tests build their own app via create_app(converse_client=<fake>) instead.
try:
    app = create_app()
except Exception:  # pragma: no cover - boto3/creds not available at import time
    app = None


if __name__ == "__main__":
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
