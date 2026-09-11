# Load / chaos testing (M6)

Plan section 17 treats load testing as a release gate, not a folder of
scripts nobody runs. This repo has two layers of it, and they answer
different questions:

```text
loadtests/<area>/test_scenario.py   automated, CI-runnable, asserts a
                                     specific invariant, runs against
                                     fault-injecting fakes over ASGI
                                     (no real socket, no real Bedrock)

loadtests/<area>/locustfile.py      human-run, real HTTP, real
                                     throughput/latency numbers against
                                     a live gateway process -- fake-
                                     backed for a repeatable number, or
                                     (deliberately, by hand) real
                                     Bedrock for a real one
```

## Automated scenarios (run these in CI)

```bash
python -m unittest discover -s loadtests -t .
```

Five scenarios, each a concurrency test against the real Starlette app
via `httpx.ASGITransport` (no live server, no open port -- see
`loadtests/harness.py` for why this replaced a locust-subprocess-based
approach for the automated half):

| Scenario | File | What it proves |
|---|---|---|
| Bedrock throttling / circuit breaker | `bedrock/test_scenario.py` | Under a burst of concurrent requests to an always-throttling model, the circuit breaker opens and the gateway stops calling it -- no retry storm. |
| Tenant noisy neighbor | `tenant/test_scenario.py` | Tenant A's burst against its own tight `rpm_limit` never affects tenant B's concurrent requests. |
| Kill-switch under load | `failure/test_scenario.py` | An admin state flip to `EMERGENCY_BLOCK` blocks the very next requests -- no window where in-flight traffic still reaches the model. |
| Policy update under load | `failure/test_scenario.py` | A `policy_epoch` bump invalidates the response cache even with many requests racing to read/write it concurrently. |
| Guardrail unavailable under load | `guardrails/test_scenario.py` | A STRICT tenant's traffic fails closed (503) under every one of many concurrent requests when the guardrail backend is down -- never a partial leak through. |

These use `services/gateway/tests/otel_fixtures.py`'s quiet tracer (same
as the main suite) so they don't spam stdout, and `loadtests/fault_injection.py`'s
fakes (distinct from `services/gateway/tests/fakes.py`'s single-behavior
ones) since a load scenario needs to inject faults across *many*
concurrent calls with thread-safe counters, not just one canned response.

## Locust scenarios (run these by hand)

Each `loadtests/<area>/locustfile.py` targets a running gateway over real
HTTP. None of them are executed as part of this repo's automation --
point them at a local instance first:

```bash
python -m services.gateway.main &        # starts on :8080 by default

TOKEN=$(python scripts/generate_dev_token.py -q --tenant-id finance)
export GATEWAY_TOKEN=$TOKEN

poetry install                           # installs locust and test dependencies
locust -f loadtests/bedrock/locustfile.py --headless \
    --host http://localhost:8080 -u 20 -r 5 -t 60s --exit-code-on-error 1
```

Each locustfile's docstring has its own exact invocation (some need a
second token for a second tenant, or a second terminal to fire an admin
call partway through the run -- see `tenant/locustfile.py` and
`failure/locustfile.py`).

### Pointing at real Bedrock

The gateway itself doesn't know or care whether `converse_client` is a
fake or a real `BedrockClient` -- that's the whole point of the seam (see
`docs/ROADMAP.md`'s M0 notes). To load-test against real Bedrock instead
of the local dev keypair + fake-backed server:

1. Configure real AWS credentials in your environment (the gateway's
   `BedrockClient` uses the normal credential chain -- see the main
   README's quickstart).
2. Run `python -m services.gateway.main` with real credentials present
   and `BEDROCK_MODEL_ID` set to a model you have access to. It'll
   construct a real `BedrockClient` automatically (no fake injected)
   since that's `create_app()`'s default when `converse_client` is
   omitted.
3. Run the locustfiles above against it exactly as written.

**This will incur real AWS cost and is subject to your account's real
Bedrock quotas -- run it deliberately, on your own credentials, when
you're ready to. It is not run as part of this repo's tests or CI, and
nothing in this codebase runs it automatically.**

## What doesn't apply here (yet)

Plan section 17 also lists Redis unavailable, DynamoDB throttling, and
queue backlog scenarios. This MVP's policy store, response cache, rate
limiter, and circuit breaker are all in-memory (see each module's own
docstring for why -- the `PolicyStore`/`ResponseCache` Protocols are the
seam a real Redis/DynamoDB-backed implementation would plug into later,
per M2/M4's design). There's no queue (that's M7/Async). Those scenarios
become meaningful once real infra backs those seams -- there's nothing
to inject a fault into yet.
