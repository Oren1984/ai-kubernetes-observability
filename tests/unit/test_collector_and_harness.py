"""Evidence normalisation against canned backend responses, plus the scenario
harness guards and the cost model. No live services are used here."""
from datetime import timedelta

import httpx
import pytest

from intelligence import finops
from intelligence.audit import AuditStore
from intelligence.config import Settings
from intelligence.evidence_collector import EvidenceCollector
from intelligence.scenarios import SCENARIOS, ScenarioError, ScenarioRunner
from intelligence.schemas import utcnow

from .conftest import END, START

TRACE_ID = "ab" * 16
SETTINGS = Settings(prometheus_url="http://prom", loki_url="http://loki", tempo_url="http://tempo",
                    k8s_api_url="http://k8s", demo_api_url="http://demo")


def prom_matrix(before, during):
    points = [[START.timestamp() - 60 + i, str(v)] for i, v in enumerate(before)]
    points += [[START.timestamp() + i * 5, str(v)] for i, v in enumerate(during)]
    return {"data": {"result": [{"metric": {}, "values": points}] if points else []}}


def backend(down=(), error_ratio=(0.0, 0.5), log_line="work request failed: boom"):
    """A fake of all four backends. `down` lists hosts that refuse connections."""
    def handler(request: httpx.Request) -> httpx.Response:
        host, path, query = request.url.host, request.url.path, request.url.params.get("query", "")
        if host in down:
            raise httpx.ConnectError("connection refused", request=request)
        if host == "prom" and path.endswith("query_range"):
            if "5.." in query and "http_server" in query:
                return httpx.Response(200, json=prom_matrix([error_ratio[0]] * 3, [error_ratio[1]] * 3))
            if "histogram_quantile" in query:
                return httpx.Response(200, json=prom_matrix([0.03] * 3, [0.04, float("nan"), 0.05]))
            if "k8s_container_restarts" in query:
                return httpx.Response(200, json=prom_matrix([2] * 3, [2, 2, 2]))
            if "http_client" in query:
                return httpx.Response(200, json=prom_matrix([0.0] * 3, [0.0] * 3))
            if "memory_limit" in query:
                return httpx.Response(200, json=prom_matrix([], []))  # no samples
            return httpx.Response(200, json=prom_matrix([1] * 3, [1] * 3))
        if host == "prom":
            return httpx.Response(200, json={"data": {"result": []}})
        if host == "tempo" and path == "/api/search":
            if "status=error" in request.url.params["q"]:
                return httpx.Response(200, json={"traces": [{"traceID": TRACE_ID, "durationMs": 31}]})
            return httpx.Response(200, json={"traces": []})
        if host == "tempo":
            span = lambda name, kind, start, end, **extra: {"name": name, "kind": kind, "startTimeUnixNano": str(start),
                                                           "endTimeUnixNano": str(end), **extra}
            return httpx.Response(200, json={"batches": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "demo-api"}}]},
                "scopeSpans": [{"spans": [
                    span("GET /api/work", "SPAN_KIND_SERVER", 0, 30_000_000),
                    span("dependency.call", "SPAN_KIND_INTERNAL", 2_000_000, 24_000_000,
                         status={"code": "STATUS_CODE_ERROR"},
                         events=[{"name": "exception", "attributes": [
                             {"key": "exception.type", "value": {"stringValue": "DependencyError"}},
                             {"key": "exception.message", "value": {"stringValue": "failed password=hunter2"}}]}]),
                ]}]}]})
        if host == "loki" and path.endswith("/query"):
            return httpx.Response(200, json={"data": {"result": [
                {"metric": {"severity_text": "ERROR", "event": "work_failed"}, "value": [0, "41"]}]}})
        if host == "loki":
            labels = {"severity_text": "ERROR", "event": "work_failed", "trace_id": TRACE_ID}
            return httpx.Response(200, json={"data": {"result": [
                {"stream": labels, "values": [[str(int(START.timestamp() * 1e9)), log_line]]}]}})
        if host == "k8s" and path.endswith("/pods"):
            return httpx.Response(200, json={"items": [{"metadata": {"name": "demo-api-abc"}, "status": {
                "containerStatuses": [{"name": "demo-api", "ready": True, "restartCount": 2, "lastState": {
                    "terminated": {"reason": "OOMKilled", "exitCode": 137,
                                   "finishedAt": (START - timedelta(hours=3)).isoformat()}}}]}}]})
        if host == "k8s":
            return httpx.Response(200, json={"items": [
                {"reason": "Unhealthy", "type": "Warning", "count": 3, "message": "Readiness probe failed",
                 "lastTimestamp": (START + timedelta(seconds=30)).isoformat(), "involvedObject": {"name": "demo-api-abc"}},
                {"reason": "Pulled", "type": "Normal", "message": "old", "involvedObject": {"name": "demo-api-abc"},
                 "lastTimestamp": (START - timedelta(hours=5)).isoformat()},
                {"reason": "Killing", "type": "Normal", "message": "other workload",
                 "lastTimestamp": START.isoformat(), "involvedObject": {"name": "traffic-generator-x"}}]})
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport), httpx.Client(transport=transport, base_url=SETTINGS.k8s_api_url)


def collect(**kwargs):
    client, k8s = backend(**kwargs)
    return EvidenceCollector(SETTINGS, client=client, k8s_client=k8s).collect("INC-0001", START, END)


# ------------------------------------------------------------- normalisation
def test_metrics_are_split_into_baseline_and_incident_values():
    item = collect().get("M-error_ratio")
    assert (item.value, item.baseline, item.unit, item.source) == (0.5, 0.0, "ratio", "prometheus")
    assert "http_response_status_code" in item.query and "50.0%" in item.observation


def test_nan_samples_are_dropped_and_restart_delta_is_computed():
    ev = collect()
    assert ev.get("M-latency_p95").value == 0.05
    assert ev.get("M-container_restarts").value == 0  # 2 before, 2 after: no restart in the window


def test_metric_without_samples_is_reported_missing_not_invented():
    ev = collect()
    assert ev.get("M-memory_limit_utilization") is None
    assert any(m.source == "prometheus" and m.item == "memory_limit_utilization" for m in ev.missing_evidence)
    assert not ev.source_unavailable("prometheus")


def test_trace_breakdown_and_secret_redaction():
    trace = collect().get("T-error")
    assert trace.attributes["exemplar_trace_id"] == TRACE_ID and trace.attributes["error_span"] == "dependency.call"
    assert trace.attributes["dominant_share_pct"] == pytest.approx(73.3)
    assert trace.untrusted_text == ["failed password=[REDACTED]"]


def test_logs_are_grouped_counted_and_joined_to_the_trace_by_id():
    ev = collect()
    group = ev.get("L-error-work_failed")
    assert group.attributes["count"] == 41 and TRACE_ID in group.attributes["trace_ids"]
    linked = ev.get(f"L-trace-{TRACE_ID[:12]}")
    assert linked.attributes["linked_trace_evidence"] == "T-error" and TRACE_ID in linked.query


def test_kubernetes_state_is_window_scoped():
    ev = collect()
    ids = [i.id for i in ev.kubernetes_events]
    assert "K-status-demo-api-abc" in ids and "K-event-Unhealthy" in ids
    assert "K-terminated-demo-api-abc" not in ids  # that OOM kill happened 3 hours earlier
    assert "K-event-Pulled" not in ids and "K-event-Killing" not in ids


def test_references_list_every_item_and_symptoms_come_from_measurements():
    ev = collect()
    assert ev.evidence_references == [i.id for i in ev.items()] and len(set(ev.evidence_references)) == len(ev.evidence_references)
    assert ev.symptoms == ["elevated_error_rate"]
    assert all(i.query for i in ev.items())


def test_untrusted_log_text_is_sanitised_at_collection():
    ev = collect(log_line="boom token=abc123 \x1b[31m" + "x" * 900)
    text = ev.get("L-error-work_failed").untrusted_text[0]
    assert "abc123" not in text and "\x1b" not in text and len(text) < 330


# ------------------------------------------------------- backend unavailability
@pytest.mark.parametrize("host,source", [("prom", "prometheus"), ("loki", "loki"), ("tempo", "tempo"), ("k8s", "kubernetes")])
def test_unreachable_backend_is_reported_and_nothing_is_fabricated(host, source):
    ev = collect(down=(host,))
    assert ev.source_unavailable(source)
    assert not [i for i in ev.items() if i.source == source]
    others = {"prometheus", "loki", "tempo", "kubernetes"} - {source}
    assert not any(ev.source_unavailable(o) for o in others)


def test_all_backends_down_yields_empty_evidence_and_four_gaps():
    ev = collect(down=("prom", "loki", "tempo", "k8s"))
    assert ev.items() == [] and ev.symptoms == []
    assert sorted(m.source for m in ev.missing_evidence) == ["kubernetes", "loki", "prometheus", "tempo"]


def test_window_is_bounded_and_validated():
    client, k8s = backend()
    collector = EvidenceCollector(SETTINGS, client=client, k8s_client=k8s)
    ev = collector.collect("INC-0001", END - timedelta(hours=10), END)
    assert ev.time_window.end - ev.time_window.start == timedelta(minutes=SETTINGS.max_window_minutes)
    with pytest.raises(ValueError):
        collector.collect("INC-0001", END, START)


# ------------------------------------------------------------ scenario harness
@pytest.fixture
def runner(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={"active_faults": {}})

    runner = ScenarioRunner(SETTINGS, AuditStore(str(tmp_path / "a.db")), httpx.Client(transport=httpx.MockTransport(handler)))
    runner.calls = calls
    return runner


def test_trigger_records_the_incident_window(runner):
    incident = runner.store.get_incident(runner.trigger("latency"))
    assert (incident["origin"], incident["scenario"]) == ("scenario", "latency")
    assert runner.calls == [("POST", "/incident/latency")]
    assert runner.store.audit_trail(incident["id"])[0]["event"] == "incident_recorded"


def test_only_one_scenario_runs_at_a_time(runner):
    runner.trigger("latency")
    with pytest.raises(ScenarioError, match="still running"):
        runner.trigger("memory")
    assert len(runner.calls) == 1


def test_cooldown_blocks_repeated_resource_intensive_runs(runner):
    ended = utcnow() - timedelta(seconds=10)
    runner.store.create_incident("demo-api", ended - timedelta(seconds=120), ended, origin="scenario", scenario="memory")
    assert 0 < runner.cooldown_remaining("memory") <= SCENARIOS["memory"].cooldown_s
    with pytest.raises(ScenarioError, match="cooling down"):
        runner.trigger("memory")
    assert runner.calls == []


def test_scenarios_are_bounded():
    from incident_simulator import MAX_DELAY_MS, MAX_DURATION_S
    assert SCENARIOS["latency"].payload["delay_ms"] <= MAX_DELAY_MS
    assert all((s.payload or {}).get("duration_s", 0) <= MAX_DURATION_S for s in SCENARIOS.values())
    assert set(s.expected_category for s in SCENARIOS.values()) == {
        "latency_degradation", "application_errors", "memory_exhaustion"}


# ------------------------------------------------------------------ cost model
def test_cost_estimate_arithmetic():
    est = finops.estimate_monthly(log_bytes_per_s=1000, trace_bytes_per_s=10_000, active_series=2000)
    assert est["log_gb_per_month"] == pytest.approx(2.592) and est["trace_gb_per_month"] == pytest.approx(25.92)
    assert est["total_usd"] == pytest.approx(2.592 * 0.50 + 25.92 * 0.30 + 2 * 3.00)


def test_sampling_reduces_trace_volume_only():
    full = finops.estimate_monthly(1000, 10_000, 2000)
    sampled = finops.estimate_monthly(1000, 10_000, 2000, trace_sampling_ratio=0.1)
    assert sampled["trace_gb_per_month"] == pytest.approx(full["trace_gb_per_month"] / 10)
    assert sampled["log_gb_per_month"] == full["log_gb_per_month"] and sampled["metrics_usd"] == full["metrics_usd"]
    with pytest.raises(ValueError):
        finops.estimate_monthly(1, 1, 1, trace_sampling_ratio=0)
