# FinOps and resource awareness

Three kinds of numbers appear here and are kept apart:

- **Measured** — read from the running local system on 2026-10-10.
- **Calculated** — arithmetic on measured values.
- **Hypothetical** — unit prices. They are illustrative placeholders, not vendor quotes.

This project does not read AWS billing and does not calculate real cloud cost. The same view is live in the dashboard under *Telemetry cost* (`intelligence/finops.py`).

## Workload resources — measured

| Container | CPU request | CPU usage | Memory request | Memory limit | Memory working set |
|---|---|---|---|---|---|
| `demo-api` | 50 m | ~12 m | 96 MiB | 192 MiB | ~46 MiB |
| `traffic-generator` | 20 m | ~4 m | 64 MiB | 128 MiB | ~33 MiB |
| `otel-collector` | 100 m | ~6 m | 128 MiB | 320 MiB | ~74 MiB |

Requests and limits come from the `k8s_cluster` receiver, usage from `kubelet_stats`, at steady state (2 req/s, no fault). All three run well under their requests at this load; the requests are sized for headroom, not tuned.

Supporting containers (`docker stats`, steady state): Prometheus ~70 MiB, Loki ~79 MiB, Tempo ~83 MiB, Grafana ~218 MiB, dashboard ~110 MiB, kind node ~1.1 GiB. Each Compose service is capped at 512 MiB.

## Telemetry volume — measured

5-minute rates at steady state, from the Collector's exporter counters and the backends' ingest counters:

| Signal | Rate | Bytes received |
|---|---|---|
| Traces | 13.3 spans/s | ~2.6 kB/s |
| Logs | 1.7 records/s | ~0.67 kB/s |
| Metrics | 31.7 data points/s | — |

- Workload active series in Prometheus: **512**. The local TSDB holds about 6,300 series in total because Prometheus also scrapes Loki's, Tempo's and its own metrics; those are excluded from the cost model.
- About 6.6 spans per request: each request yields a client span, a server span, three internal spans and ASGI send/receive spans.

## Local storage — measured

After about two hours of running, including several incident runs:

| Volume | Size |
|---|---|
| `aiobs_prometheus-data` | 8.6 MB |
| `aiobs_tempo-data` | 12.3 MB |
| `aiobs_loki-data` | 0.8 MB |
| `aiobs_grafana-data` | 497 MB (Grafana's own database and bundled plugins, not telemetry) |

**Retention assumption:** all three telemetry backends keep 24 hours. Storage therefore plateaus after a day at roughly the daily ingest volume.

## Cost model — calculated, with hypothetical prices

```
log GB/month    = log bytes/s   × traffic multiplier × 2,592,000 s / 1e9
trace GB/month  = trace bytes/s × traffic multiplier × sampling ratio × 2,592,000 s / 1e9
cost            = log GB × P_log + trace GB × P_trace + (active series / 1000) × P_metric
```

Hypothetical unit prices (`PriceAssumptions` in `intelligence/finops.py`): logs $0.50 per GB ingested, traces $0.30 per GB ingested, metrics $3.00 per 1,000 active series per month.

| Scenario | Logs | Traces | Series | Estimated monthly cost |
|---|---|---|---|---|
| This demo as measured (2 req/s, every trace kept) | 1.7 GB | 6.7 GB | 512 | **$4.41** |
| 100× traffic, every trace kept | 174 GB | 667 GB | 512 | **$288.52** |
| 100× traffic, 10% of traces kept | 174 GB | 67 GB | 512 | **$108.55** |

Assumptions: a 30-day month; logs and traces scale linearly with traffic; active series do not grow with traffic; ingest bytes are as measured locally and would differ under a vendor's own accounting.

## What the numbers show

- **Sampling moves trace cost almost one-for-one.** Keeping 10% of traces cuts the trace line by 90% and, at 100× traffic, the total by more than 60%. This POC keeps every trace, which is right for a demo and wrong at scale. The cost is losing individual slow or failed traces unless sampling is tail-based (keep errors and slow requests, sample the rest).
- **Log volume is driven by per-request logging.** The service writes one INFO record per request. At 100× traffic that single line is most of the 174 GB. During the error scenario the service additionally wrote 90 ERROR records in 90 seconds, so incidents raise log cost exactly when volume is already high. Options are dropping routine INFO lines at the Collector, or keeping them for a shorter retention than WARN and above.
- **Metrics cost follows cardinality, not traffic.** 512 series stay 512 at any request rate. They would grow with more pods, routes and status codes, and sharply if a high-cardinality value such as a request id were ever used as a metric label.
- **Retention multiplies storage, not ingest.** Going from 24 hours to 14 days stores about 14× more for the same ingest bill.

## Limits of this model

It is a first-order estimate with made-up prices. It ignores query cost, storage and retention pricing tiers, data transfer, compression differences and volume discounts, and the compute needed to run the Collector and backends.
