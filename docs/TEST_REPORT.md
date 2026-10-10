# Test report

Executed on 2026-10-10 against the system running on the development workstation (Windows 11, Docker Desktop 29.8.2, kind v0.33.0, Kubernetes v1.36.4). Every result below was observed in an actual run; anything not run is marked NOT VERIFIED.

## Summary

| Suite | Tests | Result | Where it runs | Duration |
|---|---|---|---|---|
| Unit | 101 | **PASS** (101 / 101) | Host virtualenv; no running system | ~2 s |
| Integration | 37 | **PASS** (37 / 37) | Container on the cluster's Docker network, against live backends | ~6 s |
| End-to-end | 4 | **PASS** (4 / 4) on the second run; first run 3 / 4 (see below) | Same container, live system | 7 min 31 s |
| `scripts/verify.ps1` | 23 checks | **PASS** (23 / 23) | Host | ~20 s |

Total automated tests: **142**.

```powershell
.\.venv\Scripts\python -m pytest tests/unit
docker compose --profile test run --rm tests            # integration + E2E
docker compose --profile test run --rm tests python -m pytest -p no:cacheprovider tests/integration
```

The integration and E2E suites run in a container, not on the host, because antivirus TLS inspection on this workstation makes host-side calls to the Kubernetes API unreliable. See [LIMITATIONS.md](LIMITATIONS.md).

## Failures encountered and what they were

| Run | Failure | Cause | Resolution |
|---|---|---|---|
| Unit, first run (94 / 101) | A window with no metrics at all was classified `no_anomaly_detected` | **Engine defect.** "Healthy" was claimed without checking that the core measurements existed. | Fixed in `analysis_engine.py`: a healthy verdict now requires the error-ratio, latency and restart measurements to be present; otherwise the result is `insufficient_evidence`. |
| Unit, first run | 6 other failures | Defects in the test fixtures (fake Kubernetes client without a base URL; fake returning a non-zero client failure ratio; an assertion that also matched the rejection reason text) | Fixtures corrected. No product change. |
| E2E, first run (3 / 4) | Scenario B stopped at an assertion on the exception type | **Wrong test expectation.** OpenTelemetry records the type module-qualified (`main.DependencyError`). Classification had already passed, but the review, audit and recovery steps for scenario B were not reached in that run. | Expectation corrected; full suite re-run, 4 / 4. |

| Integration, fourth run (36 / 37) | The trace test did not find the request's internal spans | **Test flaw.** It read only the first export batch of the trace; Tempo can return one trace in several batches. It had passed three times because the spans happened to share a batch. | Test now reads all batches and waits for the complete trace. Three consecutive runs afterwards: 37 / 37 each. |

Defects found by running the system rather than by a test:

- The OpenTelemetry SDK's own export-retry messages were being shipped as application ERROR logs. Fixed with a filter on the OTLP log handler; covered by `test_sdk_internal_logs_are_not_shipped_as_application_logs`.
- The harness's "fault injection started" log line was being collected as evidence, which would have disclosed the injected fault to an LLM. Now excluded at collection; asserted in the E2E test.
- An automated security review flagged the Collector's `nodes/proxy` permission as broader than needed. It was replaced with `nodes/pods`; the kubelet metrics kept flowing and `verify.ps1` now asserts the proxy API is denied.
- An invalid Collector setting (the OOM termination reason is a resource attribute, not a metric) made the new Collector pod crash-loop. The rolling update kept the old pod serving, so no telemetry was lost. Fixed, and `start.ps1` now stops with the pod's log if a rollout fails.

## Unit tests (101)

| File | Covers |
|---|---|
| `test_correlation_and_analysis.py` | The three incident classifications; threshold boundaries for latency and error ratio; score = satisfied weight / total weight; rule weights sum to 1; an old OOM label without a restart in the window is ignored; missing evidence lowers the score and is reported; no metrics → insufficient evidence; the primary rule is mandatory; every result requires human review; cited ids exist |
| `test_llm_validation.py` | No LLM required; default provider is the offline mock; a grounded answer is accepted but cannot change category, score or evidence; 11 kinds of invalid output are rejected (not JSON, extra fields, no citations, contradicting category, uncollected evidence id, invented millisecond and percent values, certainty language, `kubectl delete`, `terraform apply`); fallback to the rule-based result; transport failure is contained; no tools and no key are sent; **prompt injection in a log line**: detected, withheld from the model, does not change the finding, and a model that obeys it is rejected; secret redaction and length bounds |
| `test_audit_and_review.py` | Sequential incident ids; analysis creates pending items and an audit record with evidence references; approve / reject store reviewer, note, timestamp and `executed: false`; rejected items stay recorded; final status; decisions are final; invalid input refused; audit log is append-only at the database level; history survives re-analysis and reopening; **no-remediation boundary**: the `intelligence` package cannot run commands, Kubernetes access is GET-only, only the harness calls fault endpoints, and a review decision opens no network connection |
| `test_collector_and_harness.py` | Evidence normalisation from canned backend responses: baseline vs incident values, NaN handling, restart delta, trace timing breakdown, log grouping and the trace-id join, window-scoped Kubernetes state, sanitisation; each backend unreachable → reported missing, nothing fabricated; bounded windows; scenario guards (one at a time, cooldown, bounds); cost-model arithmetic |

## Integration tests (37) — live system

| Area | Verified |
|---|---|
| Metrics → Prometheus | Application request metrics present with `k8s_namespace_name`, `k8s_deployment_name`, `k8s_pod_name`, `k8s_cluster_name`; samples under 30 s old; request rate above 0.5 req/s; latency histogram and custom metrics; seven workload resource metrics; deployment availability; system namespaces filtered out; Collector throughput counters for all three signals |
| Logs → Loki | A request sent with a known `x-request-id` is found in Loki with the **same trace id** the response returned, the right pod name, severity and event; SDK-internal logs are not shipped |
| Traces → Tempo | That same request's trace is retrievable by id, contains `work.validate`, `dependency.call`, `work.compute`, carries Kubernetes resource attributes, and `dependency.call` lasts at least the 20 ms it sleeps; traces span both services; probe spans are filtered out |
| Grafana | Grafana itself can query each provisioned data source (Prometheus, Loki, Tempo) and both dashboards are provisioned |
| Evidence → analysis → audit | Evidence is aggregated from all four live sources; every metric item's recorded query returns data when re-run; analysis cites only collected evidence; analysis and evidence persist and reload from SQLite |
| Backend unavailability | With one backend address pointed at a closed port (Loki, Tempo, Prometheus in turn): the source is reported unavailable, no items from it exist, and without Prometheus the result is `insufficient_evidence` |
| Security | The read-only token can read the workload; the API server's own access review denies delete/create pods, patch/update deployments, exec, secrets and configmaps; other namespaces return 403 |

## End-to-end tests (4) — live system

`test_incident_scenario_end_to_end` runs once per scenario and performs, in order: healthy baseline from telemetry → trigger → symptom visible in live telemetry → wait for the window → collect and analyse → classification matches the injected fault → cited evidence exists, spans the expected sources and is reproducible → same finding with no LLM → ungrounded LLM output rejected → approve one recommendation, reject another, decisions final → audit trail → Deployment and pod unchanged by the review → harness reset → recovery confirmed from telemetry.

| Scenario | Classification | Score | Evidence sources cited | Recovery |
|---|---|---|---|---|
| A — latency | `latency_degradation` | ≥ 0.6 asserted; 1.00 observed | Prometheus, Tempo, Loki (log joined to the slow trace by trace id) | PASS |
| B — errors | `application_errors` | ≥ 0.6 asserted; 1.00 observed | Prometheus, Tempo, Loki (log joined to the error trace by trace id) | PASS |
| C — memory | `memory_exhaustion` | ≥ 0.6 asserted | Prometheus, Kubernetes API (`OOMKilled`) | PASS — restart count +1, same pod, Deployment generation unchanged |

`test_healthy_window_after_recovery_reports_no_anomaly` — a fresh fault-free window after the three scenarios is classified `no_anomaly_detected`.

**Re-running produces valid results.** The E2E suite was run twice in a row against the same cluster (7 min 15 s and 7 min 31 s). Scenarios A and C passed both times; B passed in full the second time.

## OpenTelemetry observability validation

| Requirement | Result | Evidence |
|---|---|---|
| Application instrumented with the OpenTelemetry SDK; real metrics, logs, traces | PASS | Integration: metrics, log and trace tests |
| Kubernetes context on telemetry | PASS | Namespace, pod, deployment, cluster labels asserted on metrics, logs and trace resources |
| Separate metrics, logs and traces pipelines in the Collector | PASS | `observability/otel-collector.yaml`; throughput counters non-zero per signal |
| Delivery to Prometheus, Loki, Tempo over OTLP | PASS | Integration suite; `verify.ps1` |
| Cross-signal correlation for one incident, with trace ids | PASS | E2E scenarios A and B: metric + trace + log record sharing that trace's id, all from the same window |
| Analysis consumes evidence from the backends | PASS | E2E: cited items re-queried from Prometheus and Tempo |
| Backend unavailability identified, not fabricated | PASS | Integration tests (closed port), plus the drill below |
| Collector unavailability | **NOT VERIFIED** as a separate drill | Only backend outages were exercised |

### Outage drill (manual, 2026-10-10 19:53 UTC)

The Loki container was stopped while the system ran. `python -m intelligence --minutes 3` then returned:

```
missing_evidence: [{'source': 'loki', 'item': None, 'reason': 'loki unavailable: ConnectError: ...'}]
loki items: []        other items: 9
```

After Loki was started again, the next analysis returned `missing: []`.

## Operational verification

| Check | Result |
|---|---|
| `start.ps1` deploys workload, Collector, backends and dashboard into the cluster | PASS (five complete runs during the build) |
| `verify.ps1` | PASS, 23 / 23 |
| `stop.ps1` then `start.ps1`: system returns healthy, incidents and audit trail still present | PASS (all 3 incidents and their audit trail present after the restart; all verify checks passed) |
| `cleanup.ps1` dry run lists what it would remove | PASS |
| `cleanup.ps1 -Force` | **NOT VERIFIED** — not executed, because it deletes the cluster and data |
| Cluster creation through `start.ps1` on a machine with no cluster | **NOT VERIFIED** — the cluster was created once with the same `kind create cluster` command the script uses, before the script existed. The script's create branch itself has not run. |
| Scenarios triggered, analysed and reviewed through the Streamlit UI (browser automation) | PASS — all three triggered; B and A analysed; approve and reject recorded for A; recovery check recorded |
| Screenshots captured from the running system | PASS — five files in `docs/images/` |
| Terraform | See [TERRAFORM_VALIDATION.md](TERRAFORM_VALIDATION.md) |

## Not covered by tests

- The OpenAI-compatible adapter against a real model (only a simulated endpoint).
- Concurrent or overlapping incidents.
- The Streamlit UI has no automated test suite; it was exercised by a scripted browser session.
- Behaviour after more than a few hours of uptime.
