"""Builders for hand-made evidence. These exist only to exercise the rules in
isolation; integration and E2E tests use evidence collected from live backends."""
from datetime import datetime, timedelta, timezone

import pytest

from intelligence.schemas import EvidenceItem, IncidentEvidence, MissingEvidence, TimeWindow

START = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
END = START + timedelta(seconds=90)


def metric(name, value, baseline=None, unit="ratio"):
    return EvidenceItem(id=f"M-{name}", source="prometheus", title=name, observation=f"{name} = {value}",
                        value=value, baseline=baseline, unit=unit, query=f"query({name})")


def healthy_metrics():
    return [metric("error_ratio", 0.0, 0.0), metric("latency_p95", 0.03, 0.03, "s"),
            metric("request_rate", 2.0, 2.0, "req/s"), metric("container_restarts", 0, unit="restarts"),
            metric("memory_limit_utilization", 0.25, 0.25), metric("container_ready", 1, 1, "bool"),
            metric("client_failure_ratio", 0.0, 0.0)]


def make_evidence(metrics=None, logs=(), traces=(), kubernetes=(), missing=()):
    ev = IncidentEvidence(
        incident_id="INC-TEST", service="demo-api",
        time_window=TimeWindow(start=START, end=END),
        baseline_window=TimeWindow(start=START - timedelta(minutes=5), end=START),
        metrics=list(healthy_metrics() if metrics is None else metrics), logs=list(logs),
        traces=list(traces), kubernetes_events=list(kubernetes), missing_evidence=list(missing))
    ev.evidence_references = [i.id for i in ev.items()]
    return ev


def replace_metric(metrics, name, value, baseline=None, unit="ratio"):
    return [metric(name, value, baseline, unit) if m.id == f"M-{name}" else m for m in metrics]


def slow_trace(share=97.0):
    return EvidenceItem(id="T-slow", source="tempo", title="Slow traces", query="{traceql}",
                        observation=f"12 slow traces; dependency.call took 820 ms ({share:.0f}%)", value=0.845, unit="s",
                        attributes={"count": 12, "exemplar_trace_id": "a" * 32, "dominant_span": "dependency.call",
                                    "dominant_ms": 820.0, "server_ms": 845.0, "dominant_share_pct": share})


def error_trace():
    return EvidenceItem(id="T-error", source="tempo", title="Error traces", query="{traceql}",
                        observation="20 error traces; span dependency.call failed with DependencyError",
                        attributes={"count": 20, "exemplar_trace_id": "b" * 32, "error_span": "dependency.call",
                                    "exception_type": "DependencyError"},
                        untrusted_text=["simulated-inventory returned an unexpected failure"])


def linked_logs(trace_evidence_id, text="dependency call slow: took 820 ms"):
    return EvidenceItem(id=f"L-trace-{trace_evidence_id}", source="loki", title="Logs for trace", query="{logql}",
                        observation=f"1 log records carry the trace id, linking them to {trace_evidence_id}",
                        value=1.0, unit="records",
                        attributes={"count": 1, "linked_trace_evidence": trace_evidence_id}, untrusted_text=[text])


def error_logs(count=40):
    return EvidenceItem(id="L-error-work_failed", source="loki", title="ERROR logs", query="{logql}",
                        observation=f"{count} ERROR log records with event 'work_failed'", value=float(count),
                        unit="records", attributes={"count": count, "severity": "ERROR", "event": "work_failed"},
                        untrusted_text=["work request failed: simulated-inventory returned an unexpected failure"])


def oom_terminated():
    return EvidenceItem(id="K-terminated-demo-api-x", source="kubernetes", title="terminated", query="GET /pods",
                        observation="Container terminated with reason OOMKilled (exit code 137)",
                        attributes={"reason": "OOMKilled", "exit_code": 137})


def source_down(source):
    return MissingEvidence(source=source, reason=f"{source} unavailable: ConnectError")


@pytest.fixture
def latency_evidence():
    metrics = replace_metric(healthy_metrics(), "latency_p95", 0.95, 0.03, "s")
    return make_evidence(metrics, logs=[linked_logs("T-slow")], traces=[slow_trace()])


@pytest.fixture
def error_evidence():
    metrics = replace_metric(healthy_metrics(), "error_ratio", 0.5, 0.0)
    return make_evidence(metrics, logs=[error_logs(), linked_logs("T-error", "work request failed")],
                         traces=[error_trace()])


@pytest.fixture
def memory_evidence():
    metrics = healthy_metrics()
    metrics = replace_metric(metrics, "container_restarts", 1, unit="restarts")
    metrics = replace_metric(metrics, "memory_limit_utilization", 0.97, 0.25)
    metrics = replace_metric(metrics, "container_ready", 0, 1, "bool")
    return make_evidence(metrics, kubernetes=[oom_terminated()])
