# Limitations

This is a local engineering POC. It is not a production incident-management platform, it does not remediate anything, and its confidence figure is not scientifically calibrated.

## The analysis

- **It is rule matching, not reasoning.** The engine recognises three incident patterns plus "restart for another reason". Anything else is reported as insufficient evidence or no anomaly. It will not discover a novel failure mode.
- **Rules are tuned to this service.** Thresholds, weights, span names such as `/api/work`, and the single-service scope are specific to `demo-api`. They are not fitted to historical data.
- **The score is not a probability.** It is the weighted share of expected evidence that was observed. A score of 1.0 means every check matched, not that the conclusion is certain.
- **Injected faults are clean.** Each scenario produces one unambiguous signal. Real incidents overlap and are noisy; concurrent faults were not tested.
- **The default narrative is a template.** With `LLM_PROVIDER=mock` no language model is involved. The OpenAI-compatible adapter is implemented and covered by unit tests with a simulated endpoint, but **it was not run against a real model** in this project.
- **LLM validation is heuristic.** The numeric-grounding check tolerates rounding and can miss a wrong number that happens to be close to a real one. Injection pattern matching on input is best-effort; the firm guarantees are the output gate and the absence of any execution path.
- **Detection is on demand.** Symptoms are evaluated when a window is analysed. There is no alerting loop that opens incidents automatically.

## The system

- **Single node, single replica.** One kind node and one `demo-api` pod. No high availability, and an OOM kill makes the service unavailable for a few seconds.
- **Short retention.** 24 hours in Prometheus, Loki and Tempo, on local Docker volumes.
- **Every trace is kept.** There is no sampling; the cost page models sampling arithmetically but the pipeline does not implement it.
- **No authentication on the dashboard.** The reviewer name is free text, so the audit trail records who the reviewer said they were. Grafana allows anonymous read-only access. Acceptable only because everything is bound to `127.0.0.1`.
- **Fault-injection endpoints are unauthenticated.** They exist because this is a demo workload; they are enabled by an environment variable and reachable only locally.
- **SQLite, one writer.** Fine for one reviewer; not for concurrent use.
- **Cost figures are illustrative.** Throughput is measured locally; unit prices are placeholders. Nothing here reads AWS billing.
- **Telemetry can be lost when a backend is down.** The Collector retries for 60 seconds with a bounded queue, then drops. This is intentional (bounded memory) and is reported as missing evidence, but the data is gone.

## Environment-specific findings on the development workstation

These were observed on the Windows machine the POC was built on, which runs Norton antivirus with HTTPS inspection.

- **TLS to local ports is intercepted intermittently.** Host-side `kubectl` against the kind API server failed with `x509: certificate signed by unknown authority` for minutes at a time, because the antivirus re-signed the connection. For that reason the operations scripts run `kubectl` inside the kind node (`docker exec`), and the integration and E2E suites run in a container on the cluster's Docker network rather than on the host.
- **`terraform validate` cannot run on the host** for the same reason (the provider plugin handshake is intercepted). It was run in the official Terraform container instead. See [TERRAFORM_VALIDATION.md](TERRAFORM_VALIDATION.md).
- **The downloaded `kind.exe` disappeared once** from `.tools/` during the session, with no action by the scripts; antivirus quarantine is the likely cause but was not confirmed. The scripts now need `kind` only to create the cluster, and re-download it with checksum verification if it is missing.

On a machine without TLS inspection the host-side commands should work directly; that was not tested.

## Not verified

- Behaviour on Linux or macOS hosts, or with a Kubernetes runtime other than kind on Docker Desktop.
- The Terraform example beyond `validate`: it has never been planned or applied, and as written the instance has no egress path.
- A real LLM provider, as noted above.
- Long-running stability beyond the few hours of the build session.
