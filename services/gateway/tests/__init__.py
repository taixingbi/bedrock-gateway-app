"""Test package init.

Configures a silent global tracer (no span processors -- spans are
created and immediately discarded) before any test module calls
create_app() without an explicit `tracer=` override. Without this, the
default console exporter (telemetry/otel.py's configure_tracing()) would
spam stdout with a JSON block per request across the whole suite.

Tests that need to assert on span attributes pass their own tracer via
otel_fixtures.make_test_tracer() (an in-memory exporter) instead of
relying on this global one.
"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from ..telemetry import otel as _otel

if not _otel._configured:
    trace.set_tracer_provider(TracerProvider(resource=Resource.create({"service.name": "gateway-api-test"})))
    _otel._configured = True
