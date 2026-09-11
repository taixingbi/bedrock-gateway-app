"""OpenTelemetry tracing (M5, plan section 18).

Configures a TracerProvider with a console exporter by default -- zero
external dependencies, spans print as JSON to stdout, inspectable in dev
without standing up a collector -- and an OTLP HTTP exporter when
`OTEL_EXPORTER_OTLP_ENDPOINT` is set, for a real backend (Grafana Tempo,
Honeycomb, X-Ray via the ADOT collector, etc.). The OTLP exporter package
(`opentelemetry-exporter-otlp-proto-http`) is an optional extra, imported
lazily only when an endpoint is configured -- see requirements.txt.

`configure_tracing()` is idempotent (safe to call more than once), same
convention as `telemetry/logging.py`'s `configure_logging()`.

Every `/v1/chat` request is one span carrying the same attribute set as
the JSON telemetry record already logged (api/routes.py) -- tenant_id,
model, policy_epoch, guardrail_version, tokens, latency, retry_count,
fallback, cache_hit, estimated_cost, slo_breach. Traces and logs
deliberately carry the same shape so either can be cross-referenced by
request_id.
"""
from __future__ import annotations

from typing import Any, Optional

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_configured = False


def configure_tracing(service_name: str, *, otlp_endpoint: Optional[str] = None) -> trace.Tracer:
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter: Any = OTLPSpanExporter(endpoint=otlp_endpoint)
        else:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _configured = True
    return trace.get_tracer(service_name)


def set_span_attributes(span: trace.Span, **attributes: Any) -> None:
    """Sets only non-None attributes -- OTel attribute values can't be
    None, and most of the telemetry record's fields are legitimately
    absent on some code paths (e.g. guardrail_version before policy
    resolution, tokens on a fail-closed rejection)."""
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key, value)
