# Design notes / deviations from the plan (M0)

**Starlette instead of FastAPI, for now — RESOLVED, ported to FastAPI
after M10.** M0 was built directly on Starlette with manual Pydantic
validation in the handlers, exactly as this note originally described;
by the time the port happened (after M10, once the admin API and jobs
API had grown well past one endpoint) every route module had a
`build_X_router()` returning a plain list of `Route` objects. The port
was close to mechanical as predicted: each returns a `fastapi.APIRouter`
now, handlers gained `@router.get/post/put(...)` decorators, and
request bodies that used to be manually parsed
(`await request.json()` + `Model.model_validate(...)`) are now
FastAPI-injected parameters instead. The one real design decision was
ordering: FastAPI resolves/validates injected body parameters *before*
the handler body runs, which is *after* where the manual auth check
used to sit -- judged safe since no pipeline invariant depends on
body-shape-vs-auth precedence, only on safety/policy checks running
before the model is ever called. `main.py`'s `invalid_request_body`
exception handler reformats FastAPI's default validation-error shape
back into the gateway's existing `ErrorResponse` contract so external
behavior (status codes, error codes) is unchanged. `/docs` and
`/openapi.json` now work for real.

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
