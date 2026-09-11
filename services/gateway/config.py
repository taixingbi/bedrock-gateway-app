"""Environment-driven configuration for the gateway service (M0).

Kept deliberately tiny for the walking skeleton. Later milestones add a
proper settings layer (per-tenant policy, guardrail config, etc.) -- this
module should stay the single place that reads os.environ so the rest of
the codebase never calls os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # AWS / Bedrock
    aws_region: str
    bedrock_model_id: str
    bedrock_timeout_s: float
    bedrock_max_retries: int

    # HTTP server
    host: str
    port: int

    # Telemetry
    service_name: str
    log_level: str

    # Request handling
    max_input_chars: int

    # M1 identity
    oidc_issuer: str
    oidc_audience: str
    oidc_jwks_url: str  # empty -> fall back to the local dev keypair (see auth/devkeys.py)
    oidc_jwks_cache_ttl_s: float
    dev_jwt_keypair_path: str
    chat_required_role: str
    admin_required_role: str

    # M2 policy plane
    tenant_policy_path: str
    policy_cache_ttl_s: float

    # M4 gateway reliability
    route_set_config_path: str
    response_cache_ttl_s: float
    response_cache_max_entries: int
    circuit_breaker_failure_threshold: int
    circuit_breaker_reset_timeout_s: float

    # M5 observability
    otel_exporter_otlp_endpoint: str
    debug_capture_ttl_s: float


def load_settings() -> Settings:
    return Settings(
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        bedrock_model_id=os.environ.get(
            "BEDROCK_MODEL_ID",
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
        ),
        bedrock_timeout_s=_env_float("BEDROCK_TIMEOUT_S", 30.0),
        bedrock_max_retries=_env_int("BEDROCK_MAX_RETRIES", 2),
        host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
        port=_env_int("GATEWAY_PORT", 8080),
        service_name=os.environ.get("SERVICE_NAME", "gateway-api"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        max_input_chars=_env_int("MAX_INPUT_CHARS", 32_000),
        oidc_issuer=os.environ.get("OIDC_ISSUER", "https://dev-issuer.local/"),
        oidc_audience=os.environ.get("OIDC_AUDIENCE", "bedrock-gateway"),
        oidc_jwks_url=os.environ.get("OIDC_JWKS_URL", ""),
        oidc_jwks_cache_ttl_s=_env_float("OIDC_JWKS_CACHE_TTL_S", 300.0),
        dev_jwt_keypair_path=os.environ.get("DEV_JWT_KEYPAIR_PATH", ".dev/jwt_keypair.json"),
        chat_required_role=os.environ.get("CHAT_REQUIRED_ROLE", "developer"),
        admin_required_role=os.environ.get("ADMIN_REQUIRED_ROLE", "platform_admin"),
        tenant_policy_path=os.environ.get("TENANT_POLICY_PATH", "policies/tenants.yaml"),
        policy_cache_ttl_s=_env_float("POLICY_CACHE_TTL_S", 30.0),
        route_set_config_path=os.environ.get("ROUTE_SET_CONFIG_PATH", "policies/route_sets.yaml"),
        response_cache_ttl_s=_env_float("RESPONSE_CACHE_TTL_S", 60.0),
        response_cache_max_entries=_env_int("RESPONSE_CACHE_MAX_ENTRIES", 1000),
        circuit_breaker_failure_threshold=_env_int("CIRCUIT_BREAKER_FAILURE_THRESHOLD", 5),
        circuit_breaker_reset_timeout_s=_env_float("CIRCUIT_BREAKER_RESET_TIMEOUT_S", 30.0),
        otel_exporter_otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        debug_capture_ttl_s=_env_float("DEBUG_CAPTURE_TTL_S", 900.0),
    )
