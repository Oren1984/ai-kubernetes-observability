"""Integration: real telemetry from the Kubernetes workload reaches every backend,
is correlatable, and is what the analysis engine consumes.

Requires the running stack. Run with:  docker compose --profile test run --rm tests
"""
import time
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest

from intelligence.analysis_engine import AnalysisEngine, investigate_and_record
from intelligence.audit import AuditStore
from intelligence.schemas import utcnow
from tests.live import HTTP, SETTINGS, k8s, loki, prom, wait_for


@pytest.fixture(scope="module")
def traced_request():
    """One real request through the Kubernetes Service, with a known correlation id."""
    request_id = f"it-{uuid.uuid4().hex}"
    response = HTTP.get(f"{SETTINGS.demo_api_url}/api/work", headers={"x-request-id": request_id})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id
    return {"request_id": request_id, "trace_id": response.headers["x-trace-id"], "pod": response.json()["pod"]}


# ------------------------------------------------------------------- metrics
def test_prometheus_receives_application_metrics_with_kubernetes_context():
    series = wait_for(lambda: prom('http_server_request_duration_seconds_count{service_name="demo-api",http_route="/api/work"}'),
                      60, "demo-api request metrics in Prometheus")
    labels = series[0]["metric"]
    assert labels["k8s_namespace_name"] == "aiobs-demo" and labels["k8s_deployment_name"] == "demo-api"
    assert labels["k8s_pod_name"].startswith("demo-api-") and labels["k8s_cluster_name"] == "aiobs"
    assert labels["service_namespace"] == "aiobs" and labels["http_response_status_code"]
    assert time.time() - float(series[0]["value"][0]) < 30


def test_metrics_are_fresh_and_request_rate_is_real():
    rate = wait_for(lambda: prom('sum(rate(http_server_request_duration_seconds_count{service_name="demo-api",http_route="/api/work"}[1m]))'),
                    90, "a non-zero request rate")
    assert float(rate[0]["value"][1]) > 0.5  # the traffic generator sends ~2 req/s


def test_latency_histogram_and_custom_metrics_exist():
    assert prom('histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="demo-api"}[1m])))')
    assert prom('demo_work_units_total{service_name="demo-api"}') and prom('demo_service_status{service_name="demo-api"}')
    assert prom('http_client_request_duration_seconds_count{service_name="traffic-generator"}')


@pytest.mark.parametrize("metric", [
    "container_memory_working_set_bytes", "container_cpu_usage", "k8s_container_memory_limit_bytes",
    "k8s_container_cpu_request", "k8s_container_restarts", "k8s_container_ready",
    "k8s_container_memory_limit_utilization_ratio"])
def test_prometheus_receives_workload_resource_metrics(metric):
    assert prom(f'{metric}{{k8s_namespace_name="aiobs-demo",k8s_container_name="demo-api"}}'), metric


def test_deployment_availability_is_reported():
    available = prom('k8s_deployment_available{k8s_namespace_name="aiobs-demo",k8s_deployment_name="demo-api"}')
    assert float(available[0]["value"][1]) == 1


def test_system_namespaces_are_filtered_out_by_the_collector():
    assert prom('count(k8s_container_ready{k8s_namespace_name="kube-system"})') == []


def test_collector_reports_throughput_for_all_three_pipelines():
    for signal in ("spans", "log_records", "metric_points"):
        sent = prom(f"sum(otelcol_exporter_sent_{signal}_total)")
        assert sent and float(sent[0]["value"][1]) > 0, signal


# ---------------------------------------------------------------------- logs
def test_loki_receives_the_log_of_a_specific_request(traced_request):
    query = f'{{service_name="demo-api"}} | request_id="{traced_request["request_id"]}"'
    labels, line = wait_for(lambda: loki(query), 60, "the request's log record in Loki")[0]
    assert line == "work completed"
    assert labels["trace_id"] == traced_request["trace_id"]  # log <-> trace join key
    assert labels["k8s_pod_name"] == traced_request["pod"] and labels["k8s_namespace_name"] == "aiobs-demo"
    assert labels["severity_text"] == "INFO" and labels["event"] == "work_completed"


def test_sdk_internal_logs_are_not_shipped_as_application_logs():
    assert loki('{service_name="demo-api"} | scope_name=~"opentelemetry.*"', since_s=300) == []


# -------------------------------------------------------------------- traces
def test_tempo_receives_the_trace_with_meaningful_spans(traced_request):
    def fetch():
        # A trace can arrive in several export batches; wait until the server span is there too.
        response = HTTP.get(f"{SETTINGS.tempo_url}/api/traces/{traced_request['trace_id']}")
        if response.status_code != 200:
            return None
        batches = response.json()["batches"]
        names = {s["name"] for b in batches for scope in b["scopeSpans"] for s in scope["spans"]}
        return batches if {"GET /api/work", "dependency.call"} <= names else None
    batches = wait_for(fetch, 60, "the request's complete trace in Tempo")
    resource = {a["key"]: next(iter(a["value"].values())) for a in batches[0]["resource"]["attributes"]}
    spans = [s for b in batches for scope in b["scopeSpans"] for s in scope["spans"]]
    assert {"work.validate", "dependency.call", "work.compute"} <= {s["name"] for s in spans}
    assert resource["service.name"] == "demo-api" and resource["k8s.pod.name"] == traced_request["pod"]
    assert resource["k8s.deployment.name"] == "demo-api" and resource["k8s.namespace.name"] == "aiobs-demo"
    dependency = next(s for s in spans if s["name"] == "dependency.call")
    assert int(dependency["endTimeUnixNano"]) - int(dependency["startTimeUnixNano"]) >= 20_000_000  # the 20 ms sleep


def test_traces_are_distributed_across_both_services():
    def search():
        response = HTTP.get(f"{SETTINGS.tempo_url}/api/search", params={
            "q": '{resource.service.name="traffic-generator"} && {resource.service.name="demo-api"}', "limit": 3})
        return response.json().get("traces")
    assert wait_for(search, 60, "a trace spanning traffic-generator and demo-api")


def test_probe_spans_are_filtered_out_by_the_collector():
    response = HTTP.get(f"{SETTINGS.tempo_url}/api/search",
                        params={"q": '{span.http.route="/health" || span.http.route="/ready"}', "limit": 5})
    assert not response.json().get("traces")


# ------------------------------------------------------------------- grafana
@pytest.mark.parametrize("uid,query", [
    ("prometheus", {"expr": "up", "instant": True}),
    ("loki", {"expr": '{service_name="demo-api"}', "queryType": "range", "maxLines": 5}),
    ("tempo", {"queryType": "traceql", "query": '{resource.service.name="demo-api"}', "limit": 3}),
])
def test_grafana_can_query_each_provisioned_datasource(uid, query):
    now = int(time.time() * 1000)
    response = HTTP.post(f"{SETTINGS.grafana_url}/api/ds/query", json={
        "from": str(now - 300_000), "to": str(now),
        "queries": [{"refId": "A", "datasource": {"uid": uid}, "intervalMs": 15000, "maxDataPoints": 100, **query}]})
    assert response.status_code == 200, response.text[:300]
    result = response.json()["results"]["A"]
    assert "error" not in result and result["frames"], uid


@pytest.mark.parametrize("uid", ["aiobs-health", "aiobs-incident"])
def test_grafana_dashboards_are_provisioned(uid):
    response = HTTP.get(f"{SETTINGS.grafana_url}/api/dashboards/uid/{uid}")
    assert response.status_code == 200 and response.json()["dashboard"]["panels"]


# ------------------------------------------------- evidence -> analysis -> audit
def test_evidence_is_aggregated_from_all_four_live_sources():
    end = utcnow()
    engine = AnalysisEngine(SETTINGS)
    evidence, result = engine.investigate("IT-LIVE", end - timedelta(minutes=2), end)
    assert evidence.missing_evidence == [] or not any(m.item is None for m in evidence.missing_evidence)
    assert {"M-error_ratio", "M-latency_p95", "M-request_rate", "M-container_restarts"} <= set(evidence.evidence_references)
    assert any(i.source == "kubernetes" for i in evidence.items())
    assert evidence.get("M-request_rate").value > 0.5
    assert result.root_cause_category in ("no_anomaly_detected", "latency_degradation", "application_errors",
                                          "memory_exhaustion", "unexplained_restart", "insufficient_evidence")
    # Every metric item is reproducible: its recorded query returns data from Prometheus.
    for item in evidence.metrics:
        assert prom(item.query), item.id


def test_analysis_uses_only_collected_evidence_and_persists(tmp_path):
    store = AuditStore(str(tmp_path / "it.db"))
    end = utcnow()
    incident_id = store.create_incident(SETTINGS.service, end - timedelta(minutes=2), end)
    evidence, result = investigate_and_record(AnalysisEngine(SETTINGS), store, store.get_incident(incident_id))
    assert set(result.supporting_evidence_ids) <= set(evidence.evidence_references)
    _, stored_evidence, stored_result = AuditStore(store.path).latest_analysis(incident_id)
    assert stored_evidence.evidence_references == evidence.evidence_references
    assert stored_result.root_cause_category == result.root_cause_category
    assert [e["event"] for e in store.audit_trail(incident_id)] == ["incident_recorded", "analysis_completed"]


@pytest.mark.parametrize("field,source", [("loki_url", "loki"), ("tempo_url", "tempo"), ("prometheus_url", "prometheus")])
def test_unreachable_backend_is_reported_as_missing_not_fabricated(field, source):
    """Same live stack, but one backend address points at a closed port."""
    broken = replace(SETTINGS, **{field: "http://127.0.0.1:9", "http_timeout_s": 3.0})
    end = utcnow()
    evidence, result = AnalysisEngine(broken).investigate("IT-DOWN", end - timedelta(minutes=2), end)
    assert evidence.source_unavailable(source)
    assert [i for i in evidence.items() if i.source == source] == []
    assert any(source in line for line in result.missing_information)
    if source == "prometheus":
        assert result.root_cause_category == "insufficient_evidence"


# ------------------------------------------------------------------ security
def test_evidence_reader_can_read_the_workload():
    pods = k8s("/api/v1/namespaces/aiobs-demo/pods?labelSelector=app=demo-api")
    assert pods.status_code == 200 and pods.json()["items"]


@pytest.mark.parametrize("verb,group,resource", [
    ("delete", "", "pods"), ("create", "", "pods"), ("patch", "apps", "deployments"), ("update", "apps", "deployments"),
    ("create", "", "pods/exec"), ("get", "", "secrets"), ("list", "", "configmaps")])
def test_evidence_reader_has_no_write_or_secret_access(verb, group, resource):
    """Asks the API server what the token may do (an access review changes nothing)."""
    resource, _, subresource = resource.partition("/")
    review = k8s("/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "POST", {
        "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": {"namespace": "aiobs-demo", "verb": verb, "group": group,
                                        "resource": resource, "subresource": subresource}}})
    assert review.status_code == 201 and review.json()["status"]["allowed"] is False


def test_evidence_reader_cannot_see_other_namespaces():
    assert k8s("/api/v1/namespaces/kube-system/pods").status_code == 403
