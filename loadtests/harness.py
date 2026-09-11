"""Shared harness for M6 load/chaos scenarios.

Builds a gateway app wired with fault-injecting fakes and fires many
concurrent requests at it over ASGI directly (`httpx.ASGITransport`) --
no real socket/port needed, but a genuine request/response cycle through
the full Starlette app, so concurrency-sensitive state (rate limiter,
circuit breaker, response cache, policy cache -- all real locks around
real shared state, see M2/M4) gets exercised for real, not mocked out.

This is a deliberate, documented deviation from the originally-planned
"run everything through locust" approach -- see docs/LOAD_TESTING.md and
docs/ROADMAP.md's M6 section for why: locust needs a live HTTP server and
a subprocess/CSV-parsing pipeline to get machine-checkable pass/fail out
of a run, which is a lot of moving parts for scenarios that are really
about *concurrency correctness inside this process*. The locustfiles
alongside this harness in each subdirectory are still real and still the
right tool for an actual throughput/latency load test against a running
instance (fake-backed or, deliberately by hand, real Bedrock) -- they're
just not what the automated, CI-runnable scenario tests use.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

import httpx

from services.gateway.auth.devkeys import generate_dev_keypair, mint_dev_token
from services.gateway.auth.jwt_verifier import StaticKeyVerifier
from services.gateway.config import load_settings
from services.gateway.main import create_app
from services.gateway.policy.models import TenantPolicy, TenantState
from services.gateway.policy.store import InMemoryPolicyStore
from services.gateway.tests.otel_fixtures import make_test_tracer

ISSUER = "https://loadtest-issuer.local/"
AUDIENCE = "bedrock-gateway"


@dataclass
class ScenarioApp:
    client: httpx.AsyncClient
    policy_store: InMemoryPolicyStore
    private_key_pem: str

    def token(self, *, tenant_id: str, roles: Optional[List[str]] = None, sub: str = "load-test-user") -> str:
        return mint_dev_token(
            private_key_pem=self.private_key_pem, issuer=ISSUER, audience=AUDIENCE,
            sub=sub, tenant_id=tenant_id, application_id="loadtest", roles=roles or ["developer"],
        )

    def auth_header(self, *, tenant_id: str, roles: Optional[List[str]] = None) -> Dict[str, str]:
        return {"authorization": f"Bearer {self.token(tenant_id=tenant_id, roles=roles)}"}

    def admin_header(self) -> Dict[str, str]:
        return self.auth_header(tenant_id="platform", roles=["platform_admin"])

    async def aclose(self) -> None:
        await self.client.aclose()


def build_scenario_app(
    *,
    tenants: Dict[str, TenantPolicy],
    converse_client,
    guardrail_client=None,
    **create_app_kwargs,
) -> ScenarioApp:
    private_pem, public_pem = generate_dev_keypair()
    verifier = StaticKeyVerifier(public_key_pem=public_pem, issuer=ISSUER, audience=AUDIENCE)
    policy_store = InMemoryPolicyStore(tenants)
    tracer, _exporter = make_test_tracer()  # quiet -- no console span spam during a load run

    settings = load_settings()
    app = create_app(
        settings=settings,
        converse_client=converse_client,
        token_verifier=verifier,
        policy_store=policy_store,
        guardrail_client=guardrail_client,
        tracer=tracer,
        **create_app_kwargs,
    )
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://loadtest")
    return ScenarioApp(client=client, policy_store=policy_store, private_key_pem=private_pem)


async def fire_concurrent(coro_factory: Callable[[int], Awaitable], n: int) -> List:
    """Runs n concurrent invocations of coro_factory(i) and gathers all
    results, exceptions included (return_exceptions=True) -- a load
    scenario expects some requests to fail by design; one failing
    shouldn't cancel the rest."""
    return await asyncio.gather(*(coro_factory(i) for i in range(n)), return_exceptions=True)


def default_tenant_policy(tenant_id: str, **overrides) -> TenantPolicy:
    defaults = dict(
        tenant_id=tenant_id, state=TenantState.ACTIVE, rpm_limit=10_000, guardrail_policy="standard-v1"
    )
    defaults.update(overrides)
    return TenantPolicy(**defaults)
