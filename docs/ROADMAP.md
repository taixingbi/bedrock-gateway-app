# Build Roadmap

Full architecture rationale lives in the original plan (four invariants:
isolation, safety, policy, routing — see chat history / project notes).
This file just tracks milestone status against that plan's section 25.

| Milestone | Build | Exit criteria | Status |
|---|---|---|---|
| M0 — Walking Skeleton | ECS + `/chat` + Bedrock + minimal telemetry | End-to-end inference works | **Done** |
| M1 — Identity | OIDC/JWT + RBAC/ABAC + tenant resolution | Tenant identity trustworthy | **Done** |
| M2 — Policy Plane | Tenant config + policy epoch + kill switch + push invalidation + bounded TTL | Policy changes have bounded propagation | **Done** |
| M3 — Safety | Input/output guardrails + fail-closed | Safety invariant holds | **Done** |
| M4 — Gateway Reliability | Cache + retry + jitter + circuit breaker + certified fallback + stream cancellation | Failure paths behave correctly | **Done** |
| M5 — Observability | OTel + latency/tokens/cost/SLO + PII-safe logging | Every request traceable | **Done** |
| M6 — Load / Chaos | Real Bedrock quota tests + 429 + noisy neighbor + policy/failure injection | SLO and invariants survive load | Not started |
| **V1 release** | M0–M6 | Enterprise MVP | — |
| M7 — Async | SQS + worker + Step Functions + Batch | Long/offline workloads | Not started |
| M8 — FinOps | budgets + chargeback/showback + anomaly alerts | Cost governance | Not started |
| M9 — Model Lifecycle | golden set + certification + canary + rollback | Controlled model deployment | Not started |
| M10 — Control Plane / Portal | self-service onboarding + approval workflows | Full enterprise platform | Not started |

## What M0 actually is

`services/gateway`: a Starlette app (not FastAPI yet — see
`docs/DESIGN-NOTES.md`) exposing:

- `POST /v1/chat` — validates the request, calls Bedrock's Converse API
  through `inference/bedrock_client.py` (bounded retry + exponential
  backoff + full jitter on throttling/5xx), returns the model output +
  token usage.
- `GET /healthz` — liveness probe for the ECS target group / ALB.

Every request gets a `request_id` (generated or reused from
`X-Request-Id`), a `duration_ms`-tagged access log line, and — on
`/v1/chat` — a structured `chat request completed`/`chat request failed`
log line whose field set already matches the full telemetry schema in
section 17 of the plan (tenant_id, policy_epoch, guardrail_version,
route_set, cache_hit, fallback are present as `null`/`false` placeholders
so M1–M4 fill them in without renaming anything downstream consumers
(Grafana panels, log-based alerts) depend on).

## What M0 deliberately does NOT do

Auth, tenancy, quota, guardrails, cache, certified routing/fallback,
streaming, async jobs, FinOps, evaluation/lifecycle, control plane/portal.
Every one of those is a real milestone above with its own invariant to
prove — bolting them on ad hoc here would just mean redoing them properly
later. The seams are intentional:

- `ConverseClient` (Protocol in `inference/bedrock_client.py`) is what M4's
  certified-router/fallback wraps.
- `build_router(converse_client=..., settings=...)` takes its dependencies
  as arguments — M1's auth/tenant middleware and M2's policy snapshot slot
  in as more constructor args, not a rewrite.
- The telemetry schema is already the full one, just mostly `null` — no
  future migration of log field names.

## What M1 actually is

`services/gateway/auth/`:

- `jwt_verifier.py` — `TokenVerifier` Protocol (the same seam pattern as
  `ConverseClient`) with two implementations: `JwksVerifier` (real OIDC —
  fetches and caches the provider's JWKS by `kid`, verifies RS256
  signature/issuer/audience/expiry) and `StaticKeyVerifier` (one fixed
  public key, no network — local dev and tests).
- `identity.py` — `Identity` (sub, tenant_id, application_id, roles)
  built **only** from verified claims. `tenant_id` never comes from a
  header; plan section 5 calls out `X-Tenant-ID` spoofing explicitly as
  the anti-pattern to avoid, and `test_auth.py` has a test proving a
  spoofed header has no effect.
- `rbac.py` — `require_role` / `require_any_role`.
- `devkeys.py` — generates/persists a local RSA keypair
  (`.dev/jwt_keypair.json`, gitignored) so the running server and
  `scripts/generate_dev_token.py` agree on a key without a real IdP.

`services/gateway/pipeline.py` is new: `authenticate()` and `authorize()`
are the first two stages of the plan section 2 request pipeline, called
from `api/routes.py`'s `chat()` handler before the request body is even
parsed. This module is where M2–M4's remaining stages (kill switch,
policy snapshot, rate limit, guardrails, cache, certified router) get
added, keeping `routes.py` a thin HTTP adapter.

`main.py`'s `create_app()` gained a `token_verifier` parameter (same
optional-injection pattern as `converse_client`); when omitted it builds
a `JwksVerifier` if `OIDC_JWKS_URL` is set, else a dev `StaticKeyVerifier`.

`/v1/chat` now requires `Authorization: Bearer <token>` with a valid,
unexpired, correctly-issued/audienced token carrying the
`CHAT_REQUIRED_ROLE` role (default `developer`) — 401 if missing/invalid,
403 if the role is missing. `/healthz` stays unauthenticated (ALB liveness
probe). `tenant_id` in the telemetry log is now the real value from the
token, no longer a placeholder.

## What M1 deliberately does NOT do

ABAC beyond a role check, tenant state/kill-switch, policy epoch, quota,
guardrails, cache, certified routing/fallback, streaming — all still
M2–M4. No token refresh/introspection endpoint (out of scope for a
gateway that only verifies tokens issued elsewhere). No admin API yet
(lands with the kill switch in M2, since `PUT .../state` is the first
admin action that needs one).

## What M2 actually is

`services/gateway/policy/`:

- `models.py` — `TenantPolicy` (state, models allowlist, rpm_limit,
  guardrail_policy, route_set, slo, policy_epoch) and `TenantState`
  (`ACTIVE`/`THROTTLED`/`READ_ONLY`/`SUSPENDED`/`EMERGENCY_BLOCK`).
  `BLOCKING_STATES = {SUSPENDED, EMERGENCY_BLOCK}` is the kill switch's
  BLOCK branch (plan section 7); `THROTTLED` instead reduces the
  effective rate limit (see `pipeline.enforce_rate_limit`); `READ_ONLY`
  is reserved -- there's no write-vs-read distinction on `/v1/chat` yet
  to apply it to.
- `store.py` — `PolicyStore`/`MutablePolicyStore` Protocols (same seam
  pattern as `ConverseClient`/`TokenVerifier`). `FilePolicyStore` reads
  `policies/tenants.yaml` once at startup into an `InMemoryPolicyStore`,
  standing in for the DynamoDB table in plan section 8.
- `cache.py` — `PolicySnapshotCache`: bounded TTL (`POLICY_CACHE_TTL_S`,
  default 30s) + `invalidate(tenant_id)` push invalidation, so "maximum
  stale-policy lifetime <= ttl_s" holds even if a push is dropped.
- `rate_limiter.py` — `TokenBucketRateLimiter`, strictly per-tenant
  (isolation invariant applied to capacity, not just data).

`pipeline.py` gained four stages, wired into `api/routes.py`'s `chat()`
in this order: `resolve_policy` -> `enforce_kill_switch` ->
`enforce_rate_limit` -> (body parsed/validated) -> `enforce_model_allowlist`.
A blocked or unprovisioned tenant, or one over its rate limit, never
reaches `converse_client.converse()` (tested directly on the fake).

`api/admin_routes.py` is new: `PUT /v1/admin/tenants/{id}/state`
(requires `ADMIN_REQUIRED_ROLE`, default `platform_admin`) flips a
tenant's state, bumps its `policy_epoch`, and calls
`policy_cache.invalidate()` -- the next `/v1/chat` for that tenant sees
the change immediately, not after the TTL. `main.py`'s `create_app()`
gained a `policy_store` parameter (same injectable-dependency pattern as
`converse_client`/`token_verifier`).

`tenant_id`/`policy_epoch`/`route_set` in the telemetry log are now real
values, no longer placeholders.

## What M2 deliberately does NOT do

TPM/dollar-budget tracking (that's FinOps, M8) -- the rate limiter is
requests-per-minute only. No DynamoDB/SNS/SQS (the file-backed store +
in-process push-invalidation callback are the swappable stand-ins plan
section 8 describes; `PolicyStore`/`MutablePolicyStore` are the seam a
real control-plane-backed implementation plugs into later). `READ_ONLY`
enforcement (no write-type endpoint exists yet to apply it to). Guardrails,
cache, certified routing/fallback, streaming — still M3/M4.

## What M3 actually is

`services/gateway/guardrails/`:

- `models.py` — `GuardrailAction` (ALLOW/BLOCK), `SafetyClass`
  (STRICT/STANDARD/LOW_RISK), `GuardrailDecision`.
- `client.py` — `GuardrailClient` Protocol (`check_input`/`check_output`,
  same seam pattern as `ConverseClient`/`TokenVerifier`/`PolicyStore`) and
  `GuardrailCheckError`, raised by an implementation when the check
  itself couldn't complete (timeout/unavailable) -- distinct from a
  completed BLOCK decision.
- `basic_guardrail.py` — `BasicGuardrailClient`: regex/keyword PII
  (SSN/credit-card/email) and a prompt-injection denylist. Deliberately
  not ML-grade, same "boring now, swap later" seam as `BedrockClient`;
  a real deployment drops in Bedrock Guardrails/Comprehend behind the
  same Protocol.
- `fail_closed.py` — `run_guardrail_check()` implements the core
  invariant from plan section 11: a `GuardrailCheckError` fails closed
  (`GuardrailUnavailableError`) for STRICT/STANDARD safety classes, and
  for LOW_RISK too *unless* the tenant policy explicitly set
  `allow_guardrail_bypass_on_error` -- no silent downgrade otherwise.
  `classify_safety()` maps a `guardrail_policy` id to a safety class via
  a naming convention (`-strict`/`-lowrisk`/default standard) until
  guardrail policies get their own registry.

`pipeline.py` gained `check_input_guardrail` (after rate limiting, before
the model is called; BLOCK -> 400 `INPUT_BLOCKED`, fail-closed -> 503
`AI_SAFETY_SERVICE_UNAVAILABLE`) and `check_output_guardrail` (after
Bedrock responds, before the response is returned; BLOCK -> 502
`OUTPUT_BLOCKED`). In both failure paths, `converse_client.converse()` is
never called or its result never reaches the client (tested directly on
the fake). Telemetry now fills `guardrail_version`, `guardrail_action`,
`guardrail_latency_ms`, `blocked_reason` — no longer placeholders.

`main.py`'s `create_app()` gained a `guardrail_client` parameter
(defaults to `BasicGuardrailClient()`), same injectable pattern as the
other four dependencies.

## What M3 deliberately does NOT do

ML-based moderation/PII detection (regex is a placeholder behind a real
Protocol, not a claim of production-grade accuracy). A real preemptive
per-check timeout (test fakes simulate "unavailable" by raising
`GuardrailCheckError` directly rather than actually blocking past a
deadline -- a real HTTP-backed `GuardrailClient` would enforce its own
request timeout and raise the same exception). Caching, certified
routing/fallback, streaming — still M4.

## What M4 actually is

`services/gateway/cache/`:

- `keys.py` — `build_cache_key()`: SHA256 over exactly the fields plan
  section 12 lists (tenant_id, application_id, model_route_id/version,
  inference_parameters, normalized_messages, guardrail_version,
  policy_epoch; prompt_template_version/tool_schema_version/
  retrieval_context_hash reserved as `None` until those features exist).
  A `policy_epoch` bump (any admin state change, per M2) makes every
  previously-cached response for that tenant an automatic miss.
- `store.py` — `ResponseCache` Protocol (same seam pattern) +
  `InMemoryResponseCache` (TTL + LRU eviction at `max_entries`). Only
  responses that already passed the output guardrail are ever written
  (see `api/routes.py`) -- never raw model output.

`services/gateway/routing/`:

- `circuit_breaker.py` — `CircuitBreaker`: per-model CLOSED/OPEN/
  HALF_OPEN, in-memory, same "real interface, no provisioned infra"
  pattern as the rate limiter.
- `router.py` — `CertifiedRouter`: wraps the retry-capable
  `BedrockClient` (M0's per-call bounded retry + backoff + jitter is
  unchanged) with the breaker and fallback strictly within a tenant's
  assigned `route_set` (`policies/route_sets.yaml`) -- a model not
  listed there is never tried, even when the primary fails (tested).
  No route_set assigned means exactly one candidate, i.e. identical to
  a direct M0-era call.

`services/gateway/streaming.py` — `stream_chat_response()`: SSE
generator, framework-independent (`is_disconnected` is just an async
predicate) so client-disconnect cancellation is unit-testable without a
real ASGI disconnect. On disconnect it calls `.close()` on the upstream
generator (raises `GeneratorExit` into it -- verified against both the
fake and, by construction, the real `BedrockClient.converse_stream`,
which iterates a boto3 EventStream the same way).

`api/routes.py`'s `/v1/chat` now branches on `stream`: non-streaming
requests go cache lookup -> `CertifiedRouter.converse()` -> output
guardrail -> cache write; streaming requests go straight to
`converse_stream()` on a single circuit-breaker-gated model (see
limitations below). `ChatResponse` gained `cache_hit`/`fallback` fields;
telemetry's `fallback`/`cache_hit` are real now, no longer placeholders.

`main.py`'s `create_app()` gained `response_cache`, `circuit_breaker`,
and `route_sets` parameters, same injectable pattern as everything else.

## What M4 deliberately does NOT do (streaming limitations)

Streaming responses skip the output guardrail (blocking after the fact
can't un-send tokens already sent to the client -- a real system needs
incremental/partial-buffer guardrail checks, a milestone-sized feature of
its own) and skip certified-router fallback (switching models mid-stream
after tokens have already reached the client isn't a clean retry). The
circuit breaker still gates and records the single streaming attempt.
Both are deliberate, documented scope cuts, not oversights -- revisit
if/when streaming becomes a primary interface rather than a secondary
one. Also out of scope: a real preemptive per-chunk timeout (same
rationale as M3's guardrail timeout), and true non-blocking I/O for the
underlying boto3 EventStream iteration (it blocks the event loop per
chunk, same simplification the non-streaming call already had since M0).

## What M5 actually is

`services/gateway/telemetry/`:

- `otel.py` — `configure_tracing()`: a `TracerProvider` with a console
  exporter by default (zero external deps, spans print as JSON to
  stdout) or an OTLP HTTP exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is
  set (the exporter package is an optional extra, imported lazily --
  see requirements.txt). Idempotent, same convention as
  `configure_logging()`. `set_span_attributes()` filters `None`s, since
  OTel attributes can't hold them and most telemetry fields are
  legitimately absent on some code paths.
- `cost.py` — `estimate_cost()`: a static $/1K-token table. Not live AWS
  billing (Bedrock pricing varies by model/region and changes) -- enough
  to populate `estimated_cost` and prove the FinOps seam exists; real
  pricing sync/aggregation is M8.
- `slo.py` — `slo_breached()`: per-request flag when latency exceeds the
  tenant's `slo.p95_latency_ms`. True p95 is a percentile over a window
  of requests, which belongs in the metrics backend (Prometheus/Grafana),
  not computed request-by-request here -- this is a leading indicator
  ("this one was slow"), not a replacement for real aggregation.
- `debug_capture.py` — `DebugCaptureStore` + `redact()`: opt-in only
  (`TenantPolicy.debug_capture_enabled`, default `False`). Deliberately a
  *different class* than operational telemetry (plan section 19's
  separation) so it could carry different IAM/encryption/retention rules
  in a real deployment -- there's no real AWS infra here to attach those
  to, so this demonstrates the separation, not production-grade storage.

`api/routes.py`'s `/v1/chat` now wraps its entire body in one
`chat.request` span per request, with the same attribute set as the JSON
telemetry record (tenant_id, model, policy_epoch, guardrail_version,
tokens, latency, retry_count, fallback, cache_hit, estimated_cost,
slo_breach) -- traces and logs deliberately share field names so either
can be cross-referenced by `request_id`. `main.py`'s `create_app()`
gained `tracer` and `debug_capture_store` parameters, same injectable
pattern as everything else -- tests use `tests/otel_fixtures.py`'s
in-memory-exporter tracer instead of the real console/OTLP one, both to
avoid spamming stdout during `python -m unittest` and to assert on span
attributes directly.

## What M5 deliberately does NOT do

Real AWS billing sync (M8/FinOps). True percentile SLO aggregation
across a request window (a metrics-backend job, not in-process). ML-based
PII detection in `debug_capture.redact()` (same regex patterns as M3's
guardrail, same caveat: a placeholder behind a real interface). Because
`debug_capture`'s redaction patterns are the same ones the input/output
guardrail already blocks on, content that reaches debug capture at all
has, by construction, no SSN/credit-card/email pattern left to redact in
practice -- the redaction logic itself is still real and unit-tested
directly (`test_observability.py`), it just can't be exercised through a
full HTTP round trip without the guardrail intercepting first. Real
per-tenant IAM/encryption/retention separation for the debug store (no
AWS infra to attach it to in this MVP -- the class-level separation is
the point, not a claim of matching production access controls).
