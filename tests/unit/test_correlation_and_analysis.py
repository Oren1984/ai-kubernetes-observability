"""Incident rules, score calculation, missing evidence and the insufficient-evidence path."""
import pytest

from intelligence import correlation
from intelligence.analysis_engine import AnalysisEngine
from intelligence.config import Settings
from intelligence.schemas import INSUFFICIENT_EVIDENCE

from .conftest import (healthy_metrics, linked_logs, make_evidence, oom_terminated, replace_metric,
                       slow_trace, source_down)


def analyze(evidence):
    return AnalysisEngine(Settings(), collector=object(), llm=None).analyze(evidence)


def scores(evidence):
    return {h.category: h for h in correlation.evaluate(evidence)}


# ---------------------------------------------------------------- incident rules
def test_latency_scenario_is_classified(latency_evidence):
    result = analyze(latency_evidence)
    assert result.root_cause_category == "latency_degradation"
    assert result.confidence == 1.0
    assert {"M-latency_p95", "T-slow", "L-trace-T-slow"} <= set(result.supporting_evidence_ids)


def test_error_scenario_is_classified(error_evidence):
    result = analyze(error_evidence)
    assert result.root_cause_category == "application_errors"
    assert {"M-error_ratio", "T-error", "L-error-work_failed"} <= set(result.supporting_evidence_ids)


def test_memory_scenario_is_classified(memory_evidence):
    result = analyze(memory_evidence)
    assert result.root_cause_category == "memory_exhaustion"
    assert "K-terminated-demo-api-x" in result.supporting_evidence_ids


def test_healthy_window_reports_no_anomaly():
    result = analyze(make_evidence())
    assert result.root_cause_category == "no_anomaly_detected"
    assert result.recommendations == []


@pytest.mark.parametrize("p95,baseline,expected", [
    (0.49, 0.03, False),  # below the absolute threshold
    (0.50, 0.03, True),   # at the threshold, far above baseline
    (0.60, 0.40, False),  # slow, but the service was already that slow before the window
    (0.60, None, True),   # no baseline available: the absolute threshold alone decides
])
def test_latency_threshold_boundaries(p95, baseline, expected):
    ev = make_evidence(replace_metric(healthy_metrics(), "latency_p95", p95, baseline, "s"))
    assert ("elevated_latency" in correlation.detect_symptoms(ev)) is expected


@pytest.mark.parametrize("ratio,expected", [(0.049, False), (0.05, True), (0.5, True)])
def test_error_ratio_threshold_boundaries(ratio, expected):
    ev = make_evidence(replace_metric(healthy_metrics(), "error_ratio", ratio, 0.0))
    assert ("elevated_error_rate" in correlation.detect_symptoms(ev)) is expected


def test_old_oom_reason_without_a_restart_in_the_window_is_ignored():
    """The cluster-state label keeps saying OOMKilled long after the event."""
    from intelligence.schemas import EvidenceItem
    stale = EvidenceItem(id="M-last_terminated_reason", source="prometheus", title="reason", query="q",
                         observation="terminated with reason: OOMKilled", attributes={"reasons": ["OOMKilled"]})
    ev = make_evidence(healthy_metrics() + [stale])
    assert not scores(ev)["memory_exhaustion"].primary_satisfied
    assert analyze(ev).root_cause_category == "no_anomaly_detected"


def test_restart_without_oom_is_not_called_memory_exhaustion():
    ev = make_evidence(replace_metric(healthy_metrics(), "container_restarts", 1, unit="restarts"))
    result = analyze(ev)
    assert result.root_cause_category != "memory_exhaustion"


def test_rules_never_read_the_fault_injection_metric():
    import inspect
    source = inspect.getsource(correlation)
    assert "fault_injection" not in source.replace("fault-injection metric", "")


# ------------------------------------------------------------ score calculation
def test_score_is_satisfied_weight_over_total_weight(latency_evidence):
    latency_evidence.logs.clear()  # drop the 0.15 log-link check
    h = scores(latency_evidence)["latency_degradation"]
    satisfied = sum(c.weight for c in h.checks if c.satisfied)
    assert h.score == pytest.approx(satisfied / sum(c.weight for c in h.checks), abs=0.005)
    assert h.score == pytest.approx(0.85)


def test_rule_weights_sum_to_one():
    for category, rules in correlation.RULES:
        assert sum(weight for _, _, _, weight, _, _ in rules) == pytest.approx(1.0), category


def test_score_explanation_lists_every_rule(latency_evidence):
    result = analyze(latency_evidence)
    for check in scores(latency_evidence)["latency_degradation"].checks:
        assert check.name in result.confidence_explanation
    assert "not a calibrated probability" in result.confidence_explanation
    assert result.confidence_kind == "evidence_support_score"


# -------------------------------------------------------------- missing evidence
def test_unavailable_trace_backend_lowers_score_and_is_reported(latency_evidence):
    latency_evidence.traces.clear()
    latency_evidence.logs.clear()
    latency_evidence.missing_evidence.append(source_down("tempo"))
    result = analyze(latency_evidence)
    assert result.root_cause_category == "latency_degradation"  # metrics still carry it
    assert result.confidence == pytest.approx(0.60)
    assert any("tempo unavailable" in m for m in result.missing_information)
    assert any("slow_trace_dominant_span" in m and "could not be evaluated" in m for m in result.missing_information)


def test_no_metrics_means_insufficient_evidence():
    ev = make_evidence(metrics=[], traces=[slow_trace()], logs=[linked_logs("T-slow")],
                       missing=[source_down("prometheus")])
    result = analyze(ev)
    assert result.root_cause_category == "insufficient_evidence"
    assert result.probable_root_cause == INSUFFICIENT_EVIDENCE
    assert result.supporting_evidence_ids == []
    assert any("prometheus unavailable" in m for m in result.missing_information)


def test_primary_rule_is_required_even_with_supporting_signals():
    """Slow traces alone, without the latency metric crossing its threshold, are not enough."""
    ev = make_evidence(traces=[slow_trace()], logs=[linked_logs("T-slow")])
    assert analyze(ev).root_cause_category in ("no_anomaly_detected", "insufficient_evidence")


def test_oom_is_still_found_from_metrics_when_kubernetes_api_is_down(memory_evidence):
    from intelligence.schemas import EvidenceItem
    memory_evidence.kubernetes_events.clear()
    memory_evidence.missing_evidence.append(source_down("kubernetes"))
    memory_evidence.metrics.append(EvidenceItem(
        id="M-last_terminated_reason", source="prometheus", title="reason", query="q",
        observation="terminated with reason: OOMKilled", attributes={"reasons": ["OOMKilled"]}))
    result = analyze(memory_evidence)
    assert result.root_cause_category == "memory_exhaustion"
    assert "M-last_terminated_reason" in result.supporting_evidence_ids


# ------------------------------------------------------ recommendations / output
def test_every_result_requires_human_review(latency_evidence, error_evidence, memory_evidence):
    for evidence in (latency_evidence, error_evidence, memory_evidence, make_evidence()):
        assert analyze(evidence).human_review_required is True


def test_supporting_ids_all_exist_in_evidence(latency_evidence, error_evidence, memory_evidence):
    for evidence in (latency_evidence, error_evidence, memory_evidence):
        result = analyze(evidence)
        assert result.supporting_evidence_ids
        assert set(result.supporting_evidence_ids) <= set(evidence.evidence_references)
        assert all(any(f"[{i}]" in line for line in result.observed) for i in result.supporting_evidence_ids)


def test_recommendations_are_advisory_text_from_the_catalogue(memory_evidence):
    from intelligence.llm_adapter import _MUTATING_COMMAND
    result = analyze(memory_evidence)
    assert result.recommendations and all(r.source == "rules" for r in result.recommendations)
    assert not any(_MUTATING_COMMAND.search(r.text) for r in result.recommendations)


def test_alternative_hypotheses_are_reported(memory_evidence):
    alternatives = {a.category for a in analyze(memory_evidence).alternative_hypotheses}
    assert "unexplained_restart" in alternatives
