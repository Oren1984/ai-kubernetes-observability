"""Telemetry volume and cost awareness.

Three kinds of numbers, kept apart on purpose:
  measured     - read from Prometheus right now (local system)
  calculated   - arithmetic on measured values (e.g. bytes/s -> GB/month)
  hypothetical - unit prices below. They are illustrative placeholders, not
                 quotes and not AWS billing data; change them to model a vendor.
"""
from dataclasses import dataclass

import httpx

from .config import Settings

SECONDS_PER_MONTH = 30 * 24 * 3600
GB = 1e9


@dataclass(frozen=True)
class PriceAssumptions:
    log_ingest_usd_per_gb: float = 0.50
    trace_ingest_usd_per_gb: float = 0.30
    metric_usd_per_1k_active_series: float = 3.00  # per month


MEASUREMENTS = {
    # Throughput exported by the OpenTelemetry Collector (its own counters).
    "spans_per_s": "sum(rate(otelcol_exporter_sent_spans_total[5m]))",
    "log_records_per_s": "sum(rate(otelcol_exporter_sent_log_records_total[5m]))",
    "metric_points_per_s": "sum(rate(otelcol_exporter_sent_metric_points_total[5m]))",
    # Bytes actually received by the backends.
    "log_bytes_per_s": "sum(rate(loki_distributor_bytes_received_total[5m]))",
    "trace_bytes_per_s": "sum(rate(tempo_distributor_bytes_received_total[5m]))",
    # Series that belong to the workload and its Collector (what a vendor would bill for),
    # as opposed to everything in the local TSDB including the backends' own metrics.
    "active_series": 'count({k8s_namespace_name=~"aiobs-.*"} or {job=~"aiobs/.*"})',
    "prometheus_head_series": "prometheus_tsdb_head_series",
    "prometheus_storage_bytes": "prometheus_tsdb_storage_blocks_bytes + prometheus_tsdb_head_chunks_storage_size_bytes",
}

WORKLOAD_RESOURCES = {
    "cpu_request_cores": "k8s_container_cpu_request",
    "cpu_usage_cores": "container_cpu_usage",
    "memory_request_bytes": "k8s_container_memory_request_bytes",
    "memory_limit_bytes": "k8s_container_memory_limit_bytes",
    "memory_working_set_bytes": "container_memory_working_set_bytes",
}


def measure(settings: Settings, client: httpx.Client | None = None) -> dict[str, float | None]:
    """Current local telemetry throughput. None = not measurable right now."""
    http = client or httpx.Client(timeout=settings.http_timeout_s)
    out: dict[str, float | None] = {}
    for name, expr in MEASUREMENTS.items():
        try:
            result = http.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr}).json()["data"]["result"]
            out[name] = float(result[0]["value"][1]) if result else None
        except (httpx.HTTPError, KeyError, ValueError):
            out[name] = None
    return out


def workload_resources(settings: Settings, client: httpx.Client | None = None) -> list[dict]:
    """Requests/limits vs measured usage per container of the demo namespace."""
    http = client or httpx.Client(timeout=settings.http_timeout_s)
    rows: dict[str, dict] = {}
    for name, metric in WORKLOAD_RESOURCES.items():
        expr = f'max by (k8s_container_name) ({metric}{{k8s_namespace_name=~"aiobs-.*"}})'
        try:
            result = http.get(f"{settings.prometheus_url}/api/v1/query", params={"query": expr}).json()["data"]["result"]
        except (httpx.HTTPError, KeyError, ValueError):
            continue
        for series in result:
            container = series["metric"].get("k8s_container_name", "?")
            rows.setdefault(container, {"container": container})[name] = float(series["value"][1])
    return sorted(rows.values(), key=lambda r: r["container"])


def estimate_monthly(log_bytes_per_s: float, trace_bytes_per_s: float, active_series: float,
                     trace_sampling_ratio: float = 1.0, traffic_multiplier: float = 1.0,
                     prices: PriceAssumptions = PriceAssumptions()) -> dict:
    """Projected monthly ingest and cost under stated assumptions.

    Logs and traces scale with traffic; trace volume also scales with the
    head-sampling ratio. Active series are assumed not to grow with traffic
    (they grow with cardinality: pods, routes, status codes).
    """
    if not 0 < trace_sampling_ratio <= 1:
        raise ValueError("trace_sampling_ratio must be in (0, 1]")
    log_gb = log_bytes_per_s * traffic_multiplier * SECONDS_PER_MONTH / GB
    trace_gb = trace_bytes_per_s * traffic_multiplier * trace_sampling_ratio * SECONDS_PER_MONTH / GB
    costs = {
        "logs_usd": log_gb * prices.log_ingest_usd_per_gb,
        "traces_usd": trace_gb * prices.trace_ingest_usd_per_gb,
        "metrics_usd": active_series / 1000 * prices.metric_usd_per_1k_active_series,
    }
    return {"log_gb_per_month": log_gb, "trace_gb_per_month": trace_gb, "active_series": active_series,
            **costs, "total_usd": sum(costs.values())}
