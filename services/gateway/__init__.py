"""Gateway service package (M0 walking skeleton).

This is the minimal end-to-end slice of the Enterprise GenAI Platform plan:
    Client -> Gateway (/v1/chat) -> Bedrock -> Response
                              |
                          Telemetry (structured JSON log line per request)

Everything else in the plan (auth, tenancy, policy plane, guardrails, cache,
certified routing, streaming, async jobs, FinOps, evaluation/lifecycle,
control plane/portal) is intentionally NOT here yet -- see docs/ROADMAP.md
for the milestone sequence. M0's only job is to prove the walking skeleton
works end to end with clean seams so M1+ can be layered in without a
rewrite.
"""
