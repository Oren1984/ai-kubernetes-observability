"""Investigation pipeline: collect -> correlate -> classify -> (optional LLM
narrative, validated) -> recommendations. The output always requires human
review, and nothing in this module can act on the cluster."""
from datetime import datetime

import httpx

from . import correlation
from .config import Settings
from .evidence_collector import EvidenceCollector
from .llm_adapter import LLMClient, LLMOutputRejected, build_payload, get_llm, validate_llm_output
from .audit import AuditStore
from .schemas import (INSUFFICIENT_EVIDENCE, AlternativeHypothesis, AnalysisResult, Hypothesis,
                      IncidentEvidence, Recommendation, utcnow)

# Advisory text only. A human decides; this project never executes any of it.
RECOMMENDATIONS = {
    "latency_degradation": [
        ("investigate", "Open the sampled slow trace in Tempo and confirm which span consumes the time, then check the health and latency of that downstream dependency."),
        ("investigate", "Compare the latency onset with recent deployments or configuration changes of the dependency."),
        ("remediate", "If the dependency is confirmed slow, consider a client-side timeout with fallback, or temporarily routing around it."),
    ],
    "application_errors": [
        ("investigate", "Read the ERROR log records linked to the sampled error trace and identify the failing call and exception type."),
        ("investigate", "Check whether the failing dependency changed recently (deployment, credentials, quota)."),
        ("remediate", "If a recent release introduced the failures, consider rolling it back; otherwise add retry with backoff around the failing call."),
    ],
    "memory_exhaustion": [
        ("investigate", "Review the container's memory curve before the kill and the workload's recent changes to find what retained memory."),
        ("investigate", "Check whether the memory limit is sized for normal peak usage or whether usage grew without bound (leak)."),
        ("remediate", "If usage is legitimate, consider raising the memory limit; if it is a leak, fix it in the application and redeploy."),
    ],
    "unexplained_restart": [
        ("investigate", "Inspect the previous container's logs and termination state (exit code, reason) and the pod's events."),
        ("investigate", "Check liveness-probe failures and node pressure around the restart time."),
    ],
}


class AnalysisEngine:
    def __init__(self, settings: Settings, collector: EvidenceCollector | None = None,
                 llm: LLMClient | None | str = "from-settings"):
        self.settings = settings
        self.collector = collector or EvidenceCollector(settings)
        self.llm = get_llm(settings) if llm == "from-settings" else llm

    def investigate(self, incident_id: str, start: datetime, end: datetime) -> tuple[IncidentEvidence, AnalysisResult]:
        evidence = self.collector.collect(incident_id, start, end)
        return evidence, self.analyze(evidence)

    def analyze(self, evidence: IncidentEvidence) -> AnalysisResult:
        hypotheses = correlation.evaluate(evidence)
        result = _deterministic_result(evidence, hypotheses)
        if self.llm is None or result.root_cause_category in ("insufficient_evidence", "no_anomaly_detected"):
            return result
        result.analysis_mode = f"deterministic+llm:{self.llm.name}"
        try:
            raw = self.llm.explain(build_payload(evidence, result, hypotheses))
            narrative = validate_llm_output(raw, evidence, result)
        except LLMOutputRejected as rejected:
            result.llm_status, result.llm_rejection_reasons = "rejected", rejected.reasons
            return result
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            result.llm_status = "rejected"
            result.llm_rejection_reasons = [f"LLM call failed: {type(exc).__name__}"]
            return result
        # The narrative adds wording. Category, score and evidence stay rule-derived.
        result.llm_status = "accepted"
        result.summary = narrative.summary
        result.inferred.append(f"Explanation ({self.llm.name}): {narrative.explanation}")
        result.recommendations += [Recommendation(kind="investigate", text=text, source="llm")
                                   for text in narrative.recommendations]
        return result


def investigate_and_record(engine: AnalysisEngine, store: AuditStore,
                           incident: dict) -> tuple[IncidentEvidence, AnalysisResult]:
    """Analyse a recorded incident's window and persist the result for review.
    Only the id and the time window are passed on: not the scenario label."""
    start = datetime.fromisoformat(incident["started_at"])
    end = min(utcnow(), datetime.fromisoformat(incident["ended_at"]))
    evidence, result = engine.investigate(incident["id"], start, end)
    store.save_analysis(evidence, result)
    return evidence, result


def _missing_information(evidence: IncidentEvidence, hypotheses: list[Hypothesis]) -> list[str]:
    missing = [f"{m.source}{f' / {m.item}' if m.item else ''}: {m.reason}" for m in evidence.missing_evidence]
    blocked = sorted({f"rule '{c.name}' could not be evaluated because {c.source} was unavailable"
                      for h in hypotheses for c in h.checks if not c.evaluable})
    return missing + blocked


def _explain_score(h: Hypothesis) -> str:
    satisfied = sum(c.weight for c in h.checks if c.satisfied)
    lines = [f"Evidence-support score {h.score:.2f} = satisfied rule weight {satisfied:.2f} / total "
             f"{sum(c.weight for c in h.checks):.2f} for hypothesis '{h.category}'. "
             "It measures how much of the expected evidence pattern was observed; it is not a calibrated probability."]
    for c in h.checks:
        mark = "✓" if c.satisfied else ("?" if not c.evaluable else "✗")
        lines.append(f"{mark} {c.name} (weight {c.weight:.2f}, {c.source}): {c.description}"
                     + (f" — evidence {', '.join(c.evidence_ids)}" if c.satisfied else "")
                     + ("" if c.evaluable else " — not evaluable, source unavailable"))
    return "\n".join(lines)


def _inferences(category: str, evidence: IncidentEvidence) -> list[str]:
    slow, error = evidence.get("T-slow"), evidence.get("T-error")
    if category == "latency_degradation":
        out = ["Requests are slow rather than failing: latency crossed its threshold while the error ratio and restart count did not."]
        if slow and slow.attributes.get("dominant_span"):
            out.append(f"The time is being spent in span '{slow.attributes['dominant_span']}' "
                       f"({slow.attributes['dominant_share_pct']:.0f}% of the sampled slow request), "
                       "which points at that downstream call rather than at the service's own computation.")
        return out
    if category == "application_errors":
        out = ["Requests are failing with HTTP 5xx while the container kept running, so the failures originate in request handling, not in a crash."]
        if error and error.attributes.get("error_span"):
            out.append(f"The sampled error trace fails in span '{error.attributes['error_span']}'"
                       + (f" with exception type {error.attributes['exception_type']}" if error.attributes.get("exception_type") else "")
                       + ", which locates the failure in that call path.")
        return out
    if category == "memory_exhaustion":
        return ["Kubernetes killed the container for exceeding its memory limit and restarted it; "
                "the restart and any readiness gap are consequences of that kill.",
                "Memory usage climbed towards the limit before the kill, which is consistent with unbounded "
                "memory growth in the process (for example a leak) rather than a one-off spike."]
    if category == "unexplained_restart":
        return ["The container restarted but no OOM kill was reported, so the cause is not established by the collected evidence."]
    return []


def _deterministic_result(evidence: IncidentEvidence, hypotheses: list[Hypothesis]) -> AnalysisResult:
    best = hypotheses[0]
    missing = _missing_information(evidence, hypotheses)
    alternatives = [AlternativeHypothesis(
        category=h.category, title=h.title, score=h.score,
        note="satisfied: " + (", ".join(c.name for c in h.checks if c.satisfied) or "none")
             + "; not satisfied: " + (", ".join(c.name for c in h.checks if not c.satisfied) or "none"))
        for h in hypotheses[1:] if h.score > 0]
    common = dict(incident_id=evidence.incident_id, analysis_mode="deterministic", missing_information=missing)

    if best.primary_satisfied and best.score >= correlation.ACCEPT_SCORE_MIN:
        ids = best.evidence_ids
        observed = [f"{evidence.get(i).observation} [{i}]" for i in ids]
        return AnalysisResult(
            **common, summary=f"{best.title}. Supported by {len(ids)} evidence items across "
                              f"{len({evidence.get(i).source for i in ids})} telemetry sources.",
            probable_root_cause=best.title, root_cause_category=best.category, confidence=best.score,
            confidence_explanation=_explain_score(best), observed=observed,
            inferred=_inferences(best.category, evidence), supporting_evidence_ids=ids,
            alternative_hypotheses=alternatives,
            recommendations=[Recommendation(kind=k, text=t) for k, t in RECOMMENDATIONS[best.category]],
        )

    # "Healthy" is only claimed when the core measurements actually exist.
    measured = all(evidence.get(i) for i in ("M-error_ratio", "M-latency_p95", "M-container_restarts"))
    healthy = measured and not evidence.symptoms
    if healthy:
        return AnalysisResult(
            **common, summary="No anomaly detected: no symptom threshold was crossed in the analysed window.",
            probable_root_cause="No anomaly detected in the analysed window",
            root_cause_category="no_anomaly_detected", confidence=0.0,
            confidence_explanation="No hypothesis applies: latency, error ratio, restarts and memory all stayed within thresholds.",
            observed=[f"{m.observation} [{m.id}]" for m in evidence.metrics], inferred=[],
            supporting_evidence_ids=[m.id for m in evidence.metrics], alternative_hypotheses=[],
            recommendations=[],
        )
    return AnalysisResult(
        **common, summary=INSUFFICIENT_EVIDENCE, probable_root_cause=INSUFFICIENT_EVIDENCE,
        root_cause_category="insufficient_evidence", confidence=best.score,
        confidence_explanation=("No hypothesis reached the acceptance bar "
                                f"(primary rule satisfied and score ≥ {correlation.ACCEPT_SCORE_MIN:.2f}).\n"
                                + _explain_score(best)),
        observed=[f"{i.observation} [{i.id}]" for i in evidence.items()
                  if i.id in {e for h in hypotheses for e in h.evidence_ids}],
        inferred=[], supporting_evidence_ids=[],
        alternative_hypotheses=[AlternativeHypothesis(category=h.category, title=h.title, score=h.score)
                                for h in hypotheses if h.score > 0],
        recommendations=[
            Recommendation(kind="investigate", text="Restore the unavailable telemetry sources listed under missing information, then re-run the analysis."),
            Recommendation(kind="investigate", text="Inspect the Grafana incident dashboard manually for the analysed time window."),
        ],
    )
