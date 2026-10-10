# Observability

OpenTelemetry is the only telemetry path in this project. The application is instrumented with the OpenTelemetry SDK, and one OpenTelemetry Collector owns every pipeline from the cluster to the backends.

## Telemetry flow

```
demo-api / traffic-generator (OTel SDK)
        │  OTLP/HTTP :4318
        ▼
OpenTelemetry Collector (in cluster)  ◄── kubelet_stats · k8s_cluster · k8s_objects
        │
        ├─ metrics ─ OTLP/HTTP ─► Prometheus  /api/v1/otlp
        ├─ logs ──── OTLP/HTTP ─► Loki        /otlp
        └─ traces ── OTLP/gRPC ─► Tempo       :4317
```

All three backends ingest OTLP natively. There is no Prometheus scraping of the workload, no log file tailing, and no second agent.

## Application instrumentation

`app/telemetry.py` configures the SDK; `app/main.py` and `app/loadgen.py` use it.

| Signal | How | What |
|---|---|---|
| Traces | `FastAPIInstrumentor` + manual spans | Server span per request, with child spans `work.validate`, `dependency.call`, `work.compute`. Errors set span status and record the exception. `traffic-generator` adds the client span via `HTTPXClientInstrumentor`. |
| Metrics | FastAPI/httpx instrumentation (stable HTTP semantic conventions) + custom instruments | `http.server.request.duration` (histogram: count, latency, status code), `http.client.request.duration`, `demo.work.units`, `demo.service.status`, `demo.fault_injection.active`. Exported every 5 s. |
| Logs | Python `logging` → OTel `LoggingHandler` | Structured records with `event`, `request_id` and other fields as attributes. Trace and span ids are attached automatically. A JSON copy goes to stdout for `kubectl logs`. |

Correlation identifiers on every request:

- `x-request-id` — accepted from the caller or generated; set as span attribute `request.id`, logged as `request_id`, and returned in the response.
- `x-trace-id` — the trace id, returned in the response header.

Credential-looking values (`password=…`, `token: …`, `api_key=…`) are masked by a logging filter before a record leaves the process.

## Kubernetes context

Resource attributes come from two places, so they survive even if one fails:

- **Downward API → `OTEL_RESOURCE_ATTRIBUTES`** (`kubernetes/deployment.yaml`): `service.name`, `service.namespace`, `service.version`, `service.instance.id`, `k8s.namespace.name`, `k8s.pod.name`, `k8s.pod.uid`, `k8s.node.name`.
- **Collector `k8s_attributes` processor**: adds `k8s.deployment.name` and `k8s.pod.start_time`, matching telemetry to pods by pod UID, then by pod name and namespace, then by connection IP.
- **Collector `resource` processor**: `k8s.cluster.name=aiobs`, `deployment.environment.name=local`.

## Collector pipelines

Configuration: [`observability/otel-collector.yaml`](../observability/otel-collector.yaml). Deployment and RBAC: [`kubernetes/otel-collector.yaml`](../kubernetes/otel-collector.yaml).

| Pipeline | Receivers | Processors | Exporter |
|---|---|---|---|
| `traces` | `otlp` | `memory_limiter`, `filter/probes`, `k8s_attributes`, `resource`, `batch` | `otlp_grpc/tempo` |
| `metrics` | `otlp`, `kubelet_stats`, `k8s_cluster`, `prometheus/self` | `memory_limiter`, `k8s_attributes`, `filter/system-namespaces`, `resource`, `batch` | `otlp_http/prometheus` |
| `logs` | `otlp` | `memory_limiter`, `k8s_attributes`, `transform/redact`, `resource`, `batch` | `otlp_http/loki` |
| `logs/k8s-events` | `k8s_objects` | `memory_limiter`, `resource/k8s-events`, `resource`, `batch` | `otlp_http/loki` |

What the processors are for:

- `memory_limiter` (240 MiB, with `GOMEMLIMIT` and a 320 MiB container limit) — bounded memory.
- `filter/probes` — drops liveness/readiness probe spans.
- `filter/system-namespaces` — drops `kube-system` and other non-project namespaces from the cluster-wide receivers.
- `transform/redact` — second layer of credential masking on log bodies.
- `batch` — 2 s / 512 items.

Exporters use bounded sending queues (200 batches) and retry for at most 60 s, so an unavailable backend causes data loss for that signal rather than unbounded memory growth.

The Collector's service account has read-only cluster access (`get`, `list`, `watch`) and no write verbs.

## Backends

| Backend | Config | Notes |
|---|---|---|
| Prometheus | `observability/prometheus.yaml` | `--web.enable-otlp-receiver`. Promotes service and Kubernetes resource attributes to labels. 24 h retention. |
| Loki | `observability/loki.yaml` | Single binary, TSDB schema v13, structured metadata enabled (this is where `trace_id`, `severity_text`, `event` live). 24 h retention. |
| Tempo | `observability/tempo.yaml` | Single binary, local storage. 24 h retention. No metrics-generator. |

Metric names in Prometheus follow the OTLP translation with unit suffixes, for example `http.server.request.duration` → `http_server_request_duration_seconds_{bucket,count,sum}`, `k8s.container.restarts` → `k8s_container_restarts`.

## Cross-signal correlation

The three signals share:

- **Service identity** — `service.name` is `service_name` in Prometheus and Loki and `resource.service.name` in Tempo.
- **Kubernetes identity** — the same `k8s.pod.name`, `k8s.namespace.name`, `k8s.deployment.name` on all three.
- **Time** — all queries for an incident use the same window.
- **Trace id** — every log record emitted inside a request carries `trace_id` and `span_id`.

In Grafana, a Loki log line's `trace_id` links to the trace in Tempo, and a Tempo span links back to its logs (`observability/grafana/provisioning/datasources/datasources.yaml`).

The same join done by hand for one request:

```powershell
$r = Invoke-WebRequest http://localhost:18080/api/work -Headers @{ 'x-request-id' = 'demo-123' }
$trace = $r.Headers['x-trace-id']
# the trace
Invoke-RestMethod "http://localhost:13200/api/traces/$trace"
# its log record
Invoke-RestMethod http://localhost:13100/loki/api/v1/query_range -Body @{ query = "{service_name=`"demo-api`"} | trace_id=`"$trace`"" }
```

The evidence collector uses this join for incidents: it samples a slow or failed trace from Tempo, then fetches the Loki records that carry that trace id. See [AI_ANALYSIS.md](AI_ANALYSIS.md).

## Grafana dashboards

Provisioned from `observability/grafana/dashboards/`.

- **Kubernetes & Service Health** (`/d/aiobs-health`) — service status, deployment availability, container readiness and restarts, request rate by status code, error ratio, latency percentiles, memory and CPU against requests and limits, Collector throughput, client-side failure ratio.
- **Incident Investigation** (`/d/aiobs-incident`) — error ratio, latency and memory/restarts for the selected window; WARN-and-above application logs; Kubernetes events; a table of slow or failed traces; log volume by severity.

Both show annotations for injected faults (orange) and container restarts (red). The fault annotation reads `demo_fault_injection_active`, which exists for this purpose only and is not used by the analysis.

## Verification

Results of the verification runs are in [TEST_REPORT.md](TEST_REPORT.md). `scripts/verify.ps1` re-checks the pipelines at any time.
