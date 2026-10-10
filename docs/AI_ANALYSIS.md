# AI-assisted incident analysis

**What this is.** A deterministic rule engine that classifies an incident window from collected telemetry, plus an optional LLM that may add wording on top. The rule engine is explicit threshold matching, not autonomous reasoning. The default "LLM" is an offline template (`mock`), not a language model.

## Pipeline

```
time window ─► collect evidence ─► detect symptoms ─► score hypotheses ─► classify
                                                                            │
              human review ◄─ recommendations ◄─ validate ◄─ optional LLM narrative
```

Code: `intelligence/evidence_collector.py`, `correlation.py`, `analysis_engine.py`, `llm_adapter.py`.

The analysis receives only an incident id and a time window. It does not receive the scenario that was triggered, does not read the `demo_fault_injection_active` metric, and excludes the harness's `fault_injection_*` log lines.

## Evidence

Collected read-only, for the incident window plus a 5-minute pre-incident baseline. Windows are capped at 60 minutes, log samples at 100 lines, traces at 20.

| Source | What is collected | Evidence ids |
|---|---|---|
| Prometheus | 5xx ratio, p95 latency, request rate, restart count increase, memory/limit, readiness, client-side failure ratio, last termination reason | `M-<name>` |
| Tempo | Slow traces (server span > 300 ms) and error traces; a timing breakdown of the slowest / first one | `T-slow`, `T-error` |
| Loki | WARN-and-above records grouped by severity and event, with counts; plus the records carrying each sampled trace's trace id | `L-<severity>-<event>`, `L-trace-<trace id prefix>` |
| Kubernetes API | Container status, last termination (reason, exit code) inside the window, events for the workload's pods | `K-status-…`, `K-terminated-…`, `K-event-…` |

Every item stores the exact query or API path that produced it, and a Grafana Explore link where one applies. If a backend cannot be reached, or a query returns no samples, that is recorded in `missing_evidence`. Nothing is substituted.

**Observed vs. inferred.** An evidence item's `observation` is a statement built from returned data ("p95 latency: peak 988 ms during the window, pre-incident average 45 ms"). Inferences ("the time is spent in the downstream call") are produced separately by the analysis and shown under a different label.

## Rules and thresholds

Defined in `intelligence/correlation.py`.

| Threshold | Value |
|---|---|
| p95 latency | ≥ 0.5 s **and** ≥ 3× the pre-incident p95 |
| HTTP 5xx ratio | ≥ 5% **and** ≥ 2× the pre-incident ratio |
| Container memory / limit | ≥ 80% |
| Share of a slow request spent in one child span | ≥ 50% |
| Client-observed failure ratio | ≥ 5% |
| Score needed to accept a hypothesis | ≥ 0.60, and its primary rule must hold |

Each hypothesis is a list of weighted checks. **Bold** marks the primary rule.

| Hypothesis | Checks (weight) |
|---|---|
| `latency_degradation` | **p95 elevated (0.40)** · sampled slow trace dominated by one child span (0.25) · logs share the slow trace's trace id (0.15) · 5xx ratio not elevated (0.10) · no container restart (0.10) |
| `application_errors` | **5xx ratio elevated (0.40)** · error traces present (0.20) · ERROR logs present (0.15) · logs share the error trace's trace id (0.15) · no container restart (0.10) |
| `memory_exhaustion` | **Kubernetes reports OOMKilled (0.45)** · restart count increased (0.20) · memory ≥ 80% of limit (0.20) · container not ready or callers saw failures (0.15) |
| `unexplained_restart` | **restart count increased (0.60)** · termination reason other than OOMKilled (0.40) |

The OOM rule accepts either the Kubernetes API (a termination inside the window) or the cluster-state metric label together with a restart inside the window. The metric label alone is not enough, because it keeps saying `OOMKilled` long after the event.

## The score

```
evidence-support score = sum of weights of satisfied checks / sum of all weights
```

It answers "how much of the expected evidence pattern was actually observed". It is **not** a calibrated probability, and the UI and API label it `evidence_support_score`. The weights are engineering judgment for this demo service, not fitted to data.

A check whose source was unavailable counts as not satisfied and is listed under missing information, so a missing backend lowers the score instead of being ignored.

Outcomes:

- **A hypothesis is accepted** when its primary rule holds and its score is ≥ 0.60. Other hypotheses with a non-zero score are reported as alternatives, with the checks they did and did not satisfy.
- **`no_anomaly_detected`** when the core measurements exist and no symptom threshold was crossed.
- **`insufficient_evidence`** otherwise. The result text is "Insufficient evidence — further investigation required." and no root cause is named.

## Optional LLM narrative

Set by `LLM_PROVIDER`:

| Value | Behaviour |
|---|---|
| `mock` (default) | Offline template over the evidence. No network, no key. It exists so the validation path always runs. |
| `openai` | Any OpenAI-compatible `/chat/completions` endpoint (`LLM_BASE_URL`, `LLM_MODEL`, optional `LLM_API_KEY`), including local model servers. |
| `none` | Rules only. |

The model is called only after a hypothesis has been accepted. It receives the collected evidence and the rule engine's finding, and may contribute a summary, an explanation and extra suggestions. It cannot change the root-cause category, the score or the cited evidence, and it is given no tools.

Its answer is discarded, and the rule-based result is shown with the rejection reasons, if any of these holds:

| Check | Rejects |
|---|---|
| Strict JSON schema, no extra fields | malformed output, added fields such as an "execute" key |
| Root-cause category equals the rule engine's | contradictory or unsupported root causes |
| Every cited evidence id was collected; at least one is cited | fabricated or missing references |
| Every number with a unit (%, ms, s, MiB) matches a value in the evidence | invented metric values |
| No certainty language ("definitely", "100% sure", …) | unverifiable certainty |
| No state-changing commands (`kubectl delete`, `terraform apply`, …) | executable remediation |
| No instruction-like text | injection carried over from telemetry |

The number check is a heuristic with a rounding tolerance; it catches invented values, not every possible misstatement.

## Prompt injection through telemetry

Log lines, exception messages and event messages are written by the monitored workload and are treated as untrusted data:

1. At collection they are stripped of control characters, credential-looking values are masked, and length is capped at 300 characters.
2. Before being sent to an LLM, any text matching injection patterns is replaced with a placeholder. The original stays visible to the human reviewer.
3. The system prompt marks the field as data.
4. The output gate above applies regardless of what the model was told.
5. The rule engine never interprets log text: it uses counts, severities and trace ids.

Pattern matching on input (step 2) is not a complete defence against a determined attacker. The guarantee comes from steps 4 and 5 and from the fact that nothing downstream can execute.

## Result shape

```json
{
  "incident_id": "INC-0001",
  "analysis_mode": "deterministic+llm:mock",
  "summary": "...",
  "probable_root_cause": "Latency degradation caused by a slow downstream dependency call",
  "root_cause_category": "latency_degradation",
  "confidence": 1.0,
  "confidence_kind": "evidence_support_score",
  "confidence_explanation": "...",
  "observed": ["... [M-latency_p95]"],
  "inferred": ["..."],
  "supporting_evidence_ids": ["M-latency_p95", "T-slow", "L-trace-…"],
  "alternative_hypotheses": [],
  "missing_information": [],
  "recommendations": [{"kind": "investigate", "text": "...", "source": "rules"}],
  "human_review_required": true,
  "llm_status": "accepted"
}
```

`human_review_required` is always `true`; the type does not allow another value.

## Command line

```powershell
docker exec aiobs-dashboard python -m intelligence --minutes 5
```

Analyses the last five minutes and prints the result as JSON. Read-only; nothing is stored.
