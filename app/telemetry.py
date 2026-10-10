"""OpenTelemetry SDK setup: traces, metrics and logs, all pushed over OTLP/HTTP.

The Collector is the single owner of every pipeline, so the app exposes no
Prometheus scrape endpoint and ships no logs by any other route.
Endpoint and resource identity come from the standard OTEL_* environment
variables set in the Kubernetes manifests (Downward API).
"""
import json
import logging
import re
import sys

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

METRIC_EXPORT_INTERVAL_MS = 5000

_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)(\S+)"
)


def redact(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", text)


class RedactingFilter(logging.Filter):
    """Masks credential-looking values before a record reaches any handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


class JsonStdoutFormatter(logging.Formatter):
    """Structured stdout copy of each record, for `kubectl logs`."""

    _STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        ctx = trace.get_current_span().get_span_context()
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if ctx.is_valid:
            payload["trace_id"] = format(ctx.trace_id, "032x")
            payload["span_id"] = format(ctx.span_id, "016x")
        payload.update(
            {k: v for k, v in record.__dict__.items() if k not in self._STANDARD}
        )
        return json.dumps(payload, default=str)


def setup_telemetry() -> None:
    resource = Resource.create()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=METRIC_EXPORT_INTERVAL_MS
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(JsonStdoutFormatter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    otlp = LoggingHandler(logger_provider=logger_provider)
    # The SDK's own export-retry messages stay on stdout: shipping them through
    # the pipeline they complain about would show up as application errors.
    otlp.addFilter(lambda record: not record.name.startswith("opentelemetry"))
    root.handlers = [stdout, otlp]
    for handler in root.handlers:
        handler.addFilter(RedactingFilter())
    # Per-request access lines would duplicate the server spans.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
