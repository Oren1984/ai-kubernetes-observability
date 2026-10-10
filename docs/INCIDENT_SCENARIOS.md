# Incident scenarios

Three controlled, repeatable incidents. Each is injected into the real pod running in Kubernetes through `demo-api`'s fault-injection endpoints (`app/incident_simulator.py`), and each is bounded.

| | A — Latency degradation | B — Elevated error rate | C — Memory exhaustion |
|---|---|---|---|
| Trigger | `POST /incident/latency` `{"delay_ms": 800, "duration_s": 90}` | `POST /incident/errors` `{"error_rate": 0.5, "duration_s": 90}` | `POST /incident/memory` |
| What happens | The `dependency.call` step sleeps 800 ms longer. Requests succeed, slowly. | Every second request fails inside `dependency.call` and returns HTTP 500. | The process retains 3 MiB more per second until the container hits its 192 MiB limit and is OOM-killed. |
| Bound | ≤ 3000 ms delay, ≤ 300 s | ≤ 300 s | Container memory limit (192 MiB); hard cap of 512 MiB in code |
| Recovery | Expires by itself after 90 s, or `POST /incident/reset` | Expires by itself after 90 s, or `POST /incident/reset` | Kubernetes restarts the container; the restart clears the fault |
| Incident window | 90 s | 90 s | 120 s |
| Cooldown before re-run | 30 s | 30 s | 180 s |
| Expected classification | `latency_degradation` | `application_errors` | `memory_exhaustion` |

The error injection is deterministic: with `error_rate` 0.5 exactly every second call fails, not a random half.

## Expected evidence

**A — Latency**
- Metrics: p95 of `http.server.request.duration` on `/api/work` rises from about 45 ms to about 900 ms. Error ratio stays 0.
- Traces: server spans of ~830 ms in which the `dependency.call` child span takes ~99% of the time.
- Logs: `WARN dependency call slow: took 820 ms`, carrying the trace id of the slow request.

**B — Errors**
- Metrics: 5xx ratio on `/api/work` rises to about 50%.
- Traces: error-status spans with a recorded `DependencyError` exception on `dependency.call`.
- Logs: `ERROR work request failed: …`, carrying the trace id of the failed request.

**C — Memory**
- Metrics: `k8s.container.memory_limit_utilization` climbs to above 80%; `k8s.container.restarts` increases by 1; `k8s.container.ready` drops to 0 briefly; the traffic generator records failed calls.
- Kubernetes API: the pod's `lastState.terminated.reason` is `OOMKilled`, exit code 137.
- Logs: `WARN memory usage growing: retained buffers at N MiB` before the kill.
- No error traces are expected: the process dies instead of answering.

## Lifecycle of one scenario

1. **Trigger.** From the Streamlit page *Incident control*, or `ScenarioRunner.trigger()` in tests. The start and planned end time are recorded in SQLite and in the audit log.
2. **Observe.** Open *Incident Investigation* in Grafana. An orange annotation marks the injected fault; a red one marks a container restart.
3. **Collect evidence.** *AI investigation → Collect evidence & analyse* queries Prometheus, Tempo, Loki and the Kubernetes API for the recorded window.
4. **Analyse.** The rule engine classifies the window. See [AI_ANALYSIS.md](AI_ANALYSIS.md).
5. **Recover.** A and B expire on their own; *Reset injected faults* ends them early. C recovers when Kubernetes restarts the container.
6. **Verify healthy state.** *Verify healthy state from telemetry* checks that the service answers and that the error ratio, p95 latency and container readiness are back within thresholds, then records `recovery_verified`.

## Guards

- One scenario at a time: a trigger is refused while another incident window is open.
- Per-scenario cooldown, enforced from the incident history in SQLite.
- The fault endpoints exist only when `FAULT_INJECTION_ENABLED=true` (set in `kubernetes/deployment.yaml`).
- Scenario C cannot exhaust the node: the container's cgroup limit stops it at 192 MiB.

## Triggering by hand

```powershell
Invoke-RestMethod -Method Post http://localhost:18080/incident/latency -ContentType application/json -Body '{"delay_ms":800,"duration_s":90}'
Invoke-RestMethod http://localhost:18080/api/status
Invoke-RestMethod -Method Post http://localhost:18080/incident/reset
```

Incidents triggered this way are not recorded. To analyse one, use *AI investigation → Investigate an arbitrary time window*.

## Things to know

- The scenario label stored with an incident is ground truth for tests. It is never passed to the analysis, and the harness's own `fault_injection_started` log line is excluded from evidence collection.
- Repeated OOM kills make Kubernetes back off container restarts (10 s, 20 s, 40 s … up to 5 minutes, reset after 10 minutes of healthy running). The 180 s cooldown keeps normal use out of long back-offs, but several memory runs in a row will recover more slowly.
