# Security and governance

Controls sized for a local POC. Each one below is implemented; where a control is checked by a test, the test is named.

## The human approval boundary

**AI investigates and recommends. Humans decide and execute.**

- An analysis result always has `human_review_required: true`.
- Each recommendation becomes a review item with status `pending`. A reviewer approves or rejects it once, with a name and an optional note. Decisions are final.
- **Approval records a decision. It does not execute anything.** There is no code path from a recommendation to an action: recommendations are text stored in SQLite.
- The only component that changes workload behaviour is the scenario test harness (`intelligence/scenarios.py`), which calls the demo service's fault-injection endpoints when a human clicks a button or a test runs. It is not connected to analysis output.

Checked by: `test_deciding_a_recommendation_makes_no_network_or_process_call`, `test_only_the_scenario_harness_can_change_the_workload`, and the E2E assertion that the Deployment spec and pod identity are unchanged after approving and rejecting.

## Why remediation cannot be automated here

| Layer | Restriction |
|---|---|
| Code | The `intelligence` package imports no `subprocess`, `os` (except `config.py` for environment variables), `shutil`, or Kubernetes/Docker client, and calls no `eval`/`exec`. Checked by `test_intelligence_package_cannot_run_commands`. |
| Kubernetes credentials | The engine's token belongs to `evidence-reader`: `get`/`list` on pods, events and deployments in `aiobs-demo` only. Checked against the API server by `test_evidence_reader_has_no_write_or_secret_access` (delete, create, patch, update, exec, secrets, configmaps are all denied) and by `scripts/verify.ps1`. |
| Kubernetes client | The evidence collector only issues `GET`. Checked by `test_kubernetes_access_is_get_only`. |
| LLM | Called without tools. Its output is text that is validated and may be rejected; state-changing commands in it are a rejection reason. |

## Kubernetes permissions

| Identity | Scope | Permissions |
|---|---|---|
| `demo-workload` (app pods) | — | None. `automountServiceAccountToken: false`. |
| `evidence-reader` (analysis engine) | namespace `aiobs-demo` | `get`, `list` on pods, events, deployments |
| `otel-collector` | cluster | `get`, `list`, `watch` on the objects its receivers need; `get` on `nodes/stats` and `nodes/pods`. Not `nodes/proxy`, which would open the whole kubelet API. |

No identity in the project has `cluster-admin` or any write verb. The evidence-reader token is issued for 24 hours by `scripts/start.ps1` and stored in `.runtime/k8s/` (git-ignored), mounted read-only into the dashboard container.

## Container hardening

All three in-cluster workloads run as non-root (UID 10001), with a read-only root filesystem, no privilege escalation, all capabilities dropped and the `RuntimeDefault` seccomp profile. No privileged containers. CPU and memory requests and limits are set on each.

## Secrets and configuration

- No API keys or passwords in the repository. Configuration is by environment variable; `.env.example` holds names and placeholders only.
- `scripts/start.ps1` creates `.env` with a randomly generated Grafana admin password. `.env`, `.runtime/` and `data/` are git-ignored.
- No LLM key is required. The default provider is the offline mock.
- No AWS credentials are used anywhere. See [TERRAFORM_VALIDATION.md](TERRAFORM_VALIDATION.md).

## Network exposure

Every published port is bound to `127.0.0.1` (Compose services, the Kubernetes API, the workload NodePort). Checked by `scripts/verify.ps1`.

## Untrusted telemetry

Log lines, exception messages and Kubernetes event messages are written by the monitored workload and are treated as untrusted input:

- **Sensitive data filtering** in three places: the application's logging filter, the Collector's `transform/redact` processor, and `sanitize_text` at evidence collection.
- **Bounded** to 300 characters, control characters removed.
- **Instruction injection**: suspected injection text is withheld from the LLM, the LLM output is validated independently of its input, and the rule engine never interprets log text. Details in [AI_ANALYSIS.md](AI_ANALYSIS.md).

Checked by the prompt-injection tests in `tests/unit/test_llm_validation.py`, including one where the model obeys the injected text and the output gate rejects it.

## Bounded queries

- Incident windows are capped at 60 minutes; the baseline is 5 minutes.
- HTTP timeouts of 8 s per backend call; log samples capped at 100 lines and traces at 20.
- Loki enforces `max_query_length: 12h` and `max_entries_limit_per_query: 1000`.
- Fault injection is bounded in size and duration. See [INCIDENT_SCENARIOS.md](INCIDENT_SCENARIOS.md).

## Audit trail

SQLite table `audit_log`, append-only: database triggers reject `UPDATE` and `DELETE` (`test_audit_log_is_append_only`).

| Event | Recorded |
|---|---|
| `incident_recorded` | incident id, origin, time window, actor |
| `analysis_completed` | analysis timestamp and mode, LLM status and any rejection reasons, root-cause category, score, all evidence references, cited evidence, missing evidence, proposed recommendations |
| `recommendation_approved` / `recommendation_rejected` | reviewer, decision timestamp, note, the recommendation text, resulting final status, `executed: false` |
| `recovery_verified` | the health checks and their values |
| `fault_injection_reset` | actor |

Each stored analysis keeps the full evidence record it was based on, so a decision can be traced back to the exact data the reviewer saw.

## What is deliberately not here

Authentication on the Streamlit dashboard (the reviewer name is free text), TLS between local components, network policies, image signing, secret management, multi-user roles. These are listed in [LIMITATIONS.md](LIMITATIONS.md).
