"""Test tracer backed by an in-memory exporter -- no console spam, and
spans are inspectable directly (exporter.get_finished_spans()) instead of
parsed out of stdout JSON."""
from __future__ import annotations

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def make_test_tracer():
    """Returns (tracer, exporter). Each call gets its own isolated
    TracerProvider, so tests don't see each other's spans."""
    provider = TracerProvider(resource=Resource.create({"service.name": "gateway-api-test"}))
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("gateway-api-test")
    return tracer, exporter
