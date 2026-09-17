import io
import json
import unittest

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


class ConsoleSpanExporterFormattingTests(unittest.TestCase):
    """The default ConsoleSpanExporter pretty-prints each span across
    ~30 lines -- harmless in a terminal, but the awslogs driver ships
    stdout to CloudWatch one line at a time, splitting a single span
    into ~30 separate, individually-useless log events that drown out
    the real structured JSON logs between them (see telemetry/otel.py's
    configure_tracing(), which fixes this with a custom formatter)."""

    def test_span_prints_as_exactly_one_line(self):
        out = io.StringIO()
        exporter = ConsoleSpanExporter(out=out, formatter=lambda span: span.to_json(indent=None) + "\n")
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")

        with tracer.start_as_current_span("chat.request") as span:
            span.set_attribute("request_id", "abc-123")

        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["name"], "chat.request")
        self.assertEqual(parsed["attributes"]["request_id"], "abc-123")


if __name__ == "__main__":
    unittest.main()
