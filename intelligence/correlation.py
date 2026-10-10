"""Deterministic incident rules: explicit thresholds, no model involved.

Each hypothesis is a list of weighted checks over the collected evidence.
Its score is the weight of the satisfied checks divided by the total weight,
so the number is fully explainable: it says how much of the expected evidence
pattern was actually observed. It is an evidence-support score, not a
statistically calibrated probability.

The rules never read the fault-injection metric or the scenario label: the
cause is inferred from symptoms only.
"""
from .schemas import Hypothesis, IncidentEvidence, SignalCheck

# --- documented thresholds -------------------------------------------------
LATENCY_P95_MIN_S = 0.5        # p95 must reach at least this ...
LATENCY_BASELINE_FACTOR = 3.0  # ... and be this many times the pre-incident p95
ERROR_RATIO_MIN = 0.05         # 5% of requests answered with HTTP 5xx
ERROR_BASELINE_FACTOR = 2.0
MEMORY_UTILIZATION_MIN = 0.80  # container memory / limit
DOMINANT_SPAN_SHARE_MIN = 50.0  # % of the server span spent in one child span
CLIENT_FAILURE_MIN = 0.05
ACCEPT_SCORE_MIN = 0.60        # below this the engine answers "insufficient evidence"

CATEGORY_TITLES = {
    "latency_degradation": "Latency degradation caused by a slow downstream dependency call",
    "application_errors": "Elevated HTTP 5xx error rate from failures in the dependency call path",
    "memory_exhaustion": "Container exceeded its memory limit and was OOM-killed by Kubernetes",
    "unexplained_restart": "Container restarted for a reason other than memory exhaustion",
}


def _metric(ev: IncidentEvidence, name: str):
    return ev.get(f"M-{name}")


def _latency_elevated(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "latency_p95")
    if not m or m.value is None:
        return False, []
    above_baseline = m.baseline is None or m.value >= LATENCY_BASELINE_FACTOR * m.baseline
    return m.value >= LATENCY_P95_MIN_S and above_baseline, [m.id]


def _errors_elevated(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "error_ratio")
    if not m or m.value is None:
        return False, []
    above_baseline = not m.baseline or m.value >= ERROR_BASELINE_FACTOR * m.baseline
    return m.value >= ERROR_RATIO_MIN and above_baseline, [m.id]


def _errors_low(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "error_ratio")
    return (bool(m) and m.value is not None and m.value < ERROR_RATIO_MIN), ([m.id] if m else [])


def _restarted(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "container_restarts")
    return (bool(m) and (m.value or 0) >= 1), ([m.id] if m else [])


def _not_restarted(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "container_restarts")
    return (bool(m) and (m.value or 0) < 1), ([m.id] if m else [])


def _memory_near_limit(ev) -> tuple[bool, list[str]]:
    m = _metric(ev, "memory_limit_utilization")
    return (bool(m) and (m.value or 0) >= MEMORY_UTILIZATION_MIN), ([m.id] if m else [])


def _availability_impact(ev) -> tuple[bool, list[str]]:
    ids = []
    ready, client = _metric(ev, "container_ready"), _metric(ev, "client_failure_ratio")
    if ready and ready.value is not None and ready.value < 1:
        ids.append(ready.id)
    if client and (client.value or 0) >= CLIENT_FAILURE_MIN:
        ids.append(client.id)
    return bool(ids), ids


def _oom_killed(ev) -> tuple[bool, list[str]]:
    """OOMKilled reported by the Kubernetes API for a termination inside the
    window, or by the cluster-state metric together with a restart in the window
    (the metric alone would also match an older, unrelated OOM)."""
    ids = [k.id for k in ev.kubernetes_events
           if k.id.startswith("K-terminated-") and k.attributes.get("reason") == "OOMKilled"]
    reason = ev.get("M-last_terminated_reason")
    if reason and "OOMKilled" in reason.attributes.get("reasons", []) and _restarted(ev)[0]:
        ids.append(reason.id)
    return bool(ids), ids


def _terminated_not_oom(ev) -> tuple[bool, list[str]]:
    ids = [k.id for k in ev.kubernetes_events
           if k.id.startswith("K-terminated-") and k.attributes.get("reason") != "OOMKilled"]
    return bool(ids), ids


def _slow_trace_dominant_span(ev) -> tuple[bool, list[str]]:
    t = ev.get("T-slow")
    return (bool(t) and t.attributes.get("dominant_share_pct", 0) >= DOMINANT_SPAN_SHARE_MIN), ([t.id] if t else [])


def _error_traces(ev) -> tuple[bool, list[str]]:
    t = ev.get("T-error")
    return bool(t), ([t.id] if t else [])


def _logs_linked_to(trace_evidence_id: str):
    """Cross-signal join: log records that carry the sampled trace's trace id."""
    def check(ev) -> tuple[bool, list[str]]:
        ids = [l.id for l in ev.logs if l.attributes.get("linked_trace_evidence") == trace_evidence_id]
        return bool(ids), ids
    return check


def _error_logs(ev) -> tuple[bool, list[str]]:
    ids = [l.id for l in ev.logs
           if l.attributes.get("severity") in ("ERROR", "CRITICAL", "FATAL") and l.attributes.get("count", 0) > 0]
    return bool(ids), ids


# (category, [(name, description, source, weight, primary, evaluator)])
RULES = [
    ("latency_degradation", [
        ("p95_latency_elevated", f"p95 latency ≥ {LATENCY_P95_MIN_S:g}s and ≥ {LATENCY_BASELINE_FACTOR:g}× the pre-incident p95",
         "prometheus", 0.40, True, _latency_elevated),
        ("slow_trace_dominant_span", f"a sampled slow trace spends ≥ {DOMINANT_SPAN_SHARE_MIN:g}% of its server span in one child span",
         "tempo", 0.25, False, _slow_trace_dominant_span),
        ("logs_linked_to_slow_trace", "log records share the trace id of the sampled slow trace",
         "loki", 0.15, False, _logs_linked_to("T-slow")),
        ("errors_not_elevated", f"HTTP 5xx ratio stayed below {ERROR_RATIO_MIN:.0%}", "prometheus", 0.10, False, _errors_low),
        ("no_container_restart", "the container did not restart in the window", "prometheus", 0.10, False, _not_restarted),
    ]),
    ("application_errors", [
        ("error_ratio_elevated", f"HTTP 5xx ratio ≥ {ERROR_RATIO_MIN:.0%} and ≥ {ERROR_BASELINE_FACTOR:g}× the pre-incident ratio",
         "prometheus", 0.40, True, _errors_elevated),
        ("error_traces_present", "traces with an error-status span exist in the window", "tempo", 0.20, False, _error_traces),
        ("error_logs_present", "ERROR-level log records exist in the window", "loki", 0.15, False, _error_logs),
        ("logs_linked_to_error_trace", "log records share the trace id of the sampled error trace",
         "loki", 0.15, False, _logs_linked_to("T-error")),
        ("no_container_restart", "the container did not restart in the window", "prometheus", 0.10, False, _not_restarted),
    ]),
    ("memory_exhaustion", [
        ("oom_killed", "Kubernetes reports the container terminated with reason OOMKilled", "kubernetes", 0.45, True, _oom_killed),
        ("container_restarted", "the container restart count increased in the window", "prometheus", 0.20, False, _restarted),
        ("memory_near_limit", f"container memory reached ≥ {MEMORY_UTILIZATION_MIN:.0%} of its limit",
         "prometheus", 0.20, False, _memory_near_limit),
        ("availability_impact", "the container was not ready, or callers saw failed requests", "prometheus", 0.15, False, _availability_impact),
    ]),
    ("unexplained_restart", [
        ("container_restarted", "the container restart count increased in the window", "prometheus", 0.60, True, _restarted),
        ("terminated_other_reason", "Kubernetes reports a termination reason other than OOMKilled",
         "kubernetes", 0.40, False, _terminated_not_oom),
    ]),
]


def _evaluable(ev: IncidentEvidence, name: str, source: str) -> bool:
    if name == "oom_killed":  # answerable from either the Kubernetes API or the cluster-state metric
        return not (ev.source_unavailable("kubernetes") and ev.source_unavailable("prometheus"))
    return not ev.source_unavailable(source)


def evaluate(ev: IncidentEvidence) -> list[Hypothesis]:
    """All hypotheses, best supported first."""
    hypotheses = []
    for category, rules in RULES:
        checks = []
        for name, description, source, weight, primary, evaluator in rules:
            evaluable = _evaluable(ev, name, source)
            satisfied, ids = evaluator(ev) if evaluable else (False, [])
            checks.append(SignalCheck(name=name, description=description, source=source, weight=weight,
                                      primary=primary, evaluable=evaluable, satisfied=satisfied, evidence_ids=ids))
        score = sum(c.weight for c in checks if c.satisfied) / sum(c.weight for c in checks)
        hypotheses.append(Hypothesis(category=category, title=CATEGORY_TITLES[category],
                                     score=round(score, 2), checks=checks))
    return sorted(hypotheses, key=lambda h: (h.primary_satisfied, h.score), reverse=True)


def detect_symptoms(ev: IncidentEvidence) -> list[str]:
    """Threshold crossings confirmed directly by measurements (no inference)."""
    symptoms = []
    for name, check in (("elevated_latency", _latency_elevated), ("elevated_error_rate", _errors_elevated),
                        ("container_restarted", _restarted), ("memory_near_limit", _memory_near_limit),
                        ("availability_impact", _availability_impact)):
        if check(ev)[0]:
            symptoms.append(name)
    return symptoms
