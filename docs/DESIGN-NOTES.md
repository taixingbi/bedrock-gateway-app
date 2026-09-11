# Design notes / deviations from the plan (M0)

**Starlette instead of FastAPI, for now.** The plan's section 3 implies
FastAPI (OpenAPI docs, familiar DI). M0 is built directly on Starlette with
manual Pydantic validation in the handlers instead. Functionally
equivalent for M0's scope (one POST endpoint); the only thing given up is
auto-generated OpenAPI docs, which don't matter until the API surface in
section 3 grows (`/v1/embeddings`, `/v1/batch`, `/v1/jobs/{id}`,
`/v1/models`, the admin API). Porting to FastAPI later is mechanical: the
route handlers already take a validated Pydantic model and return a dict;
wrap them with `@router.post(...)` and drop the manual
`ChatRequest.model_validate(...)` call. Do this port at the same time the
admin API is added (M2/M10) rather than before, since FastAPI's DI system
is worth adopting properly once there's more than one dependency
(converse_client) to inject.

**No circuit breaker / fallback yet.** `inference/bedrock_client.py`
retries a single Bedrock call with bounded attempts + jitter, but there is
only one model in play — there is nothing to fall back *to* until M4/M12
add the certified route set. Retry-storm protection (a shared retry
budget across requests, not just per-request bounded attempts) is also
M4/M6 scope, not M0.

**No streaming.** `/v1/chat` is request/response, not SSE. Section 13
(streaming, client-disconnect handling, backpressure) is a distinct
milestone-worthy chunk of work and is out of scope here.

**No tenant/auth/guardrail/cache fields — but their telemetry slots
exist.** See `docs/ROADMAP.md` for why the telemetry schema is emitted at
full width already.
