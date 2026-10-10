"""End-to-end: each controlled incident, from healthy baseline to verified recovery.

Every step uses the real system: the fault is injected into the pod running in
Kubernetes, evidence is read back from Prometheus / Loki / Tempo / the
Kubernetes API, and the classification is compared with the scenario that was
triggered. The scenario label is ground truth for the test only; it is never
given to the analysis.

Takes about 8 minutes. Run with:  docker compose --profile test run --rm tests
"""
import time
from datetime import datetime, timedelta

import pytest

from intelligence.analysis_engine import AnalysisEngine, investigate_and_record
from intelligence.audit import AuditStore, ReviewError
from intelligence.scenarios import SCENARIOS, ScenarioRunner
from intelligence.schemas import utcnow
from tests.live import HTTP, SETTINGS, k8s, prom, wait_for

EXPECTED_SYMPTOM = {"latency": "elevated_latency", "errors": "elevated_error_rate", "memory": "container_restarted"}
# Evidence each scenario must be explained by, per telemetry source.
EXPECTED_SOURCES = {"latency": {"prometheus", "tempo", "loki"}, "errors": {"prometheus", "tempo", "loki"},
                    "memory": {"prometheus", "kubernetes"}}
TELEMETRY_SETTLE_S = 20  # SDK export interval + collector batch + backend ingest


@pytest.fixture(scope="module")
def system(tmp_path_factory):
    store = AuditStore(str(tmp_path_factory.mktemp("e2e") / "incidents.db"))
    return store, AnalysisEngine(SETTINGS), ScenarioRunner(SETTINGS, store)


def workload_state() -> dict:
    """What a remediation would have to change: the Deployment spec and the pod identity."""
    deployment = k8s("/apis/apps/v1/namespaces/aiobs-demo/deployments/demo-api").json()
    pod = k8s("/api/v1/namespaces/aiobs-demo/pods?labelSelector=app=demo-api").json()["items"][0]
    return {"generation": deployment["metadata"]["generation"], "replicas": deployment["spec"]["replicas"],
            "limits": deployment["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"],
            "pod_uid": pod["metadata"]["uid"],
            "restarts": pod["status"]["containerStatuses"][0]["restartCount"]}


class GarbageLLM:
    name = "garbage"

    def explain(self, payload):
        return '{"summary": "Certainly a network partition at 99.9%", "root_cause_category": "network", ' \
               '"explanation": "x", "supporting_evidence_ids": ["M-invented"], "recommendations": ["kubectl delete pod x"]}'


@pytest.mark.parametrize("key", ["latency", "errors", "memory"])
def test_incident_scenario_end_to_end(system, key):
    store, engine, runner = system
    scenario = SCENARIOS[key]

    # 1. healthy baseline, confirmed from telemetry
    baseline = wait_for(lambda: (s := runner.check_recovery())["healthy"] and s, 240, "a healthy baseline")
    assert all(baseline["checks"].values())
    before = workload_state()

    # 2. trigger (3. workload traffic comes from the traffic-generator Deployment, ~2 req/s)
    incident_id = runner.trigger(key)
    incident = store.get_incident(incident_id)
    started, ends = datetime.fromisoformat(incident["started_at"]), datetime.fromisoformat(incident["ended_at"])
    assert (ends - started).total_seconds() == scenario.duration_s

    # 4. the incident appears in telemetry while it is happening
    def symptom_visible():
        live = engine.collector.collect(incident_id, started, utcnow())
        return EXPECTED_SYMPTOM[key] in live.symptoms
    wait_for(symptom_visible, scenario.duration_s + 30, f"symptom {EXPECTED_SYMPTOM[key]} in live telemetry", interval_s=5)

    time.sleep(max(0.0, (ends - utcnow()).total_seconds()) + TELEMETRY_SETTLE_S)

    # 5 + 6. collect evidence and analyse (default mode: deterministic rules + offline mock narrative)
    evidence, result = investigate_and_record(engine, store, store.get_incident(incident_id))

    # 7. root-cause classification matches the injected fault
    assert result.root_cause_category == scenario.expected_category, result.confidence_explanation
    assert result.confidence >= 0.6 and result.human_review_required is True
    assert result.llm_status == "accepted", result.llm_rejection_reasons

    # 8. every cited evidence item exists, spans the expected sources, and is reproducible
    assert result.supporting_evidence_ids
    assert set(result.supporting_evidence_ids) <= set(evidence.evidence_references)
    cited = [evidence.get(i) for i in result.supporting_evidence_ids]
    assert EXPECTED_SOURCES[key] <= {item.source for item in cited}
    assert not any(evidence.source_unavailable(s) for s in ("prometheus", "loki", "tempo", "kubernetes"))
    for item in (i for i in cited if i.source == "prometheus" and i.id != "M-last_terminated_reason"):
        assert HTTP.get(f"{SETTINGS.prometheus_url}/api/v1/query_range", params={
            "query": item.query, "start": started.timestamp(), "end": ends.timestamp(), "step": 15,
        }).json()["data"]["result"], f"{item.id} is not reproducible"
    if key in ("latency", "errors"):
        # cross-signal correlation: metric + trace + a log joined to that trace by trace id
        trace = evidence.get("T-slow" if key == "latency" else "T-error")
        trace_id = trace.attributes["exemplar_trace_id"]
        assert HTTP.get(f"{SETTINGS.tempo_url}/api/traces/{trace_id}").status_code == 200
        linked = next(l for l in evidence.logs if l.attributes.get("linked_trace_evidence") == trace.id)
        assert linked.attributes["trace_id"] == trace_id and linked.id in result.supporting_evidence_ids
    if key == "latency":
        assert evidence.get("T-slow").attributes["dominant_span"] == "dependency.call"
        assert evidence.get("M-latency_p95").value >= 0.5 > evidence.get("M-latency_p95").baseline
    if key == "errors":
        assert 0.3 <= evidence.get("M-error_ratio").value <= 0.7  # injected rate is 50%
        # OpenTelemetry records the exception type module-qualified ("main.DependencyError")
        assert evidence.get("T-error").attributes["exception_type"].endswith("DependencyError")
    if key == "memory":
        reasons = [k.attributes.get("reason") for k in evidence.kubernetes_events if k.id.startswith("K-terminated-")]
        assert reasons == ["OOMKilled"]
        assert evidence.get("M-container_restarts").value == 1
        assert evidence.get("M-memory_limit_utilization").value >= 0.8
    # the harness's own "fault injection started" marker must not be in the evidence
    assert not any("fault_injection" in i.id for i in evidence.items())

    # same evidence, no LLM at all: identical finding
    rules_only = AnalysisEngine(SETTINGS, collector=engine.collector, llm=None).analyze(evidence)
    assert (rules_only.root_cause_category, rules_only.confidence) == (result.root_cause_category, result.confidence)
    # same evidence, an LLM that returns ungrounded output: rejected, finding unchanged
    rejected = AnalysisEngine(SETTINGS, collector=engine.collector, llm=GarbageLLM()).analyze(evidence)
    assert rejected.llm_status == "rejected" and rejected.root_cause_category == scenario.expected_category
    assert not any("kubectl delete" in r.text for r in rejected.recommendations)

    # 9. human review: approve one recommendation, reject another, with notes
    state_before_review = workload_state()
    recommendations = store.list_recommendations(incident_id)
    assert len(recommendations) >= 2 and store.get_incident(incident_id)["review_status"] == "awaiting_review"
    remediation = next(r for r in recommendations if r["kind"] == "remediate")
    investigation = next(r for r in recommendations if r["kind"] == "investigate")
    store.decide(investigation["id"], "approve", "e2e-reviewer", "evidence is convincing")
    store.decide(remediation["id"], "reject", "e2e-reviewer", "not doing this in a test")
    with pytest.raises(ReviewError):
        store.decide(remediation["id"], "approve", "e2e-reviewer")  # decisions are final
    for other in store.list_recommendations(incident_id):
        if other["status"] == "pending":
            store.decide(other["id"], "approve", "e2e-reviewer")

    # 10. audit trail
    trail = store.audit_trail(incident_id)
    assert [e["event"] for e in trail][:2] == ["incident_recorded", "analysis_completed"]
    analysis_entry = trail[1]["details"]
    assert analysis_entry["mode"] == "deterministic+llm:mock" and analysis_entry["root_cause_category"] == scenario.expected_category
    assert set(result.supporting_evidence_ids) <= set(analysis_entry["evidence_references"])
    rejected_entry = next(e for e in trail if e["event"] == "recommendation_rejected")
    assert rejected_entry["actor"] == "e2e-reviewer" and rejected_entry["details"]["note"] == "not doing this in a test"
    assert rejected_entry["details"]["executed"] is False and rejected_entry["ts"]
    assert next(r for r in store.list_recommendations(incident_id) if r["id"] == remediation["id"])["status"] == "rejected"
    assert store.get_incident(incident_id)["review_status"] == "reviewed"

    # no automatic remediation: approving and rejecting changed nothing in the cluster
    time.sleep(5)
    assert workload_state() == state_before_review

    # 11. restore (harness cleanup, separate from the review workflow) and 12. confirm recovery from telemetry
    if key != "memory":
        runner.reset()
    recovered = wait_for(lambda: (s := runner.check_recovery(incident_id))["healthy"] and s, 240, "recovery in telemetry")
    assert all(recovered["checks"].values())
    assert store.get_incident(incident_id)["recovered_at"]
    assert store.audit_trail(incident_id)[-1]["event"] == "recovery_verified"

    # the Deployment was never modified; only Kubernetes itself restarted the OOM-killed container
    after = workload_state()
    assert {k: after[k] for k in ("generation", "replicas", "limits", "pod_uid")} == \
           {k: before[k] for k in ("generation", "replicas", "limits", "pod_uid")}
    assert after["restarts"] - before["restarts"] == (1 if key == "memory" else 0)


def test_healthy_window_after_recovery_reports_no_anomaly(system):
    """Shutdown of the faults leaves a clean signal: a fresh window is classified as healthy."""
    _, engine, runner = system
    wait_for(lambda: runner.check_recovery()["healthy"], 240, "a healthy system")
    time.sleep(45)  # let a full, fault-free window accumulate
    end = utcnow()
    evidence, result = engine.investigate("E2E-HEALTHY", end - timedelta(seconds=40), end)
    assert result.root_cause_category == "no_anomaly_detected" and evidence.symptoms == []
    assert prom('sum(rate(http_server_request_duration_seconds_count{service_name="demo-api",http_route="/api/work"}[30s]))')
