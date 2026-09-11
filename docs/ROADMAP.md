# Build Roadmap

Full architecture rationale lives in the original plan (four invariants:
isolation, safety, policy, routing — see chat history / project notes).
This file just tracks milestone status against that plan's section 25.

| Milestone | Build | Exit criteria | Status |
|---|---|---|---|
| M0 — Walking Skeleton | ECS + `/chat` + Bedrock + minimal telemetry | End-to-end inference works | **Done** (this commit) |
| M1 — Identity | OIDC/JWT + RBAC/ABAC + tenant resolution | Tenant identity trustworthy | Not started |
| M2 — Policy Plane | Tenant config + policy epoch + kill switch + push invalidation + bounded TTL | Policy changes have bounded propagation | Not started |
| M3 — Safety | Input/output guardrails + fail-closed | Safety invariant holds | Not started |
| M4 — Gateway Reliability | Cache + retry + jitter + circuit breaker + certified fallback + stream cancellation | Failure paths behave correctly | Not started |
| M5 — Observability | OTel + latency/tokens/cost/SLO + PII-safe logging | Every request traceable | Not started |
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
