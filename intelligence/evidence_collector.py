"""Collects incident evidence from the real backends, read-only.

Every item records the query that produced it. Nothing is synthesised: when a
backend is unreachable or returns no data, that is recorded in
`missing_evidence` instead.
"""
import json
import math
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings
from .correlation import detect_symptoms
from .sanitize import sanitize_text
from .schemas import EvidenceItem, IncidentEvidence, MissingEvidence, TimeWindow

RATE_WINDOW = "30s"
STEP_S = 5
SLOW_TRACE_THRESHOLD_MS = 300
LOG_SEVERITIES = "WARN|WARNING|ERROR|CRITICAL|FATAL"
# Kubernetes status can lag the incident window slightly.
K8S_WINDOW_SLACK = timedelta(seconds=90)


def _metric_definitions(service: str, namespace: str) -> list[dict]:
    http = f'service_name="{service}",http_route="/api/work"'
    container = f'k8s_namespace_name="{namespace}",k8s_container_name="{service}"'
    count = "http_server_request_duration_seconds_count"
    client = f'http_client_request_duration_seconds_count{{service_name="traffic-generator",server_address="{service}"'
    return [
        {"name": "error_ratio", "title": "HTTP 5xx ratio on /api/work", "unit": "ratio", "agg": "max",
         "expr": f'(sum(rate({count}{{{http},http_response_status_code=~"5.."}}[{RATE_WINDOW}])) or vector(0))'
                 f' / sum(rate({count}{{{http}}}[{RATE_WINDOW}]))'},
        {"name": "latency_p95", "title": "p95 latency of /api/work", "unit": "s", "agg": "max",
         "expr": f'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{http}}}[{RATE_WINDOW}])))'},
        {"name": "request_rate", "title": "Request rate on /api/work", "unit": "req/s", "agg": "mean",
         "expr": f'sum(rate({count}{{{http}}}[{RATE_WINDOW}]))'},
        {"name": "container_restarts", "title": "Container restarts", "unit": "restarts", "agg": "increase",
         "expr": f'max(k8s_container_restarts{{{container}}})'},
        {"name": "memory_limit_utilization", "title": "Container memory / limit", "unit": "ratio", "agg": "max",
         "expr": f'max(k8s_container_memory_limit_utilization_ratio{{{container}}})'},
        {"name": "container_ready", "title": "Container readiness (1 = ready)", "unit": "bool", "agg": "min",
         "expr": f'min(k8s_container_ready{{{container}}})'},
        {"name": "client_failure_ratio", "title": "Failed calls seen by traffic-generator", "unit": "ratio", "agg": "max",
         "expr": f'(sum(rate({client},error_type!=""}}[{RATE_WINDOW}])) or vector(0))'
                 f' / sum(rate({client}}}[{RATE_WINDOW}]))'},
    ]


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "s":
        return f"{value * 1000:.0f} ms"
    if unit == "bool":
        return "ready" if value >= 1 else "not ready"
    return f"{value:.2f} {unit}" if value % 1 else f"{value:.0f} {unit}"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class EvidenceCollector:
    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 k8s_client: httpx.Client | None = None):
        self.s = settings
        self.http = client or httpx.Client(timeout=settings.http_timeout_s)
        self._k8s = k8s_client

    # ------------------------------------------------------------------ API
    def collect(self, incident_id: str, start: datetime, end: datetime) -> IncidentEvidence:
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if end <= start:
            raise ValueError("time window end must be after start")
        max_len = timedelta(minutes=self.s.max_window_minutes)
        if end - start > max_len:  # bounded query window
            start = end - max_len
        baseline = TimeWindow(start=start - timedelta(minutes=self.s.baseline_minutes), end=start)
        evidence = IncidentEvidence(
            incident_id=incident_id, service=self.s.service,
            time_window=TimeWindow(start=start, end=end), baseline_window=baseline,
        )
        for source, collect in (("prometheus", self._collect_metrics), ("tempo", self._collect_traces),
                                ("loki", self._collect_logs), ("kubernetes", self._collect_kubernetes)):
            try:
                collect(evidence)
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
                evidence.missing_evidence.append(MissingEvidence(
                    source=source, reason=f"{source} unavailable: {type(exc).__name__}: {sanitize_text(str(exc), 160)}"))
        evidence.evidence_references = [item.id for item in evidence.items()]
        evidence.symptoms = detect_symptoms(evidence)
        return evidence

    # ----------------------------------------------------------- Prometheus
    def _collect_metrics(self, ev: IncidentEvidence) -> None:
        start, end = ev.time_window.start, ev.time_window.end
        for d in _metric_definitions(self.s.service, self.s.namespace):
            response = self.http.get(f"{self.s.prometheus_url}/api/v1/query_range", params={
                "query": d["expr"], "start": ev.baseline_window.start.timestamp(),
                "end": end.timestamp(), "step": STEP_S})
            response.raise_for_status()
            series = response.json()["data"]["result"]
            points = [(float(t), float(v)) for t, v in (series[0]["values"] if series else [])]
            points = [(t, v) for t, v in points if not math.isnan(v) and not math.isinf(v)]
            during = [v for t, v in points if t >= start.timestamp()]
            before = [v for t, v in points if t < start.timestamp()]
            if not during:
                ev.missing_evidence.append(MissingEvidence(
                    source="prometheus", item=d["name"], reason="query returned no samples in the incident window"))
                continue
            if d["agg"] == "increase":
                value = during[-1] - (before[-1] if before else during[0])
                baseline, observation = None, f"{d['title']}: increased by {value:.0f} during the window (now {during[-1]:.0f})"
            else:
                pick = {"max": max, "min": min, "mean": lambda xs: sum(xs) / len(xs)}[d["agg"]]
                value = pick(during)
                baseline = (sum(before) / len(before)) if before else None
                label = {"max": "peak", "min": "lowest", "mean": "average"}[d["agg"]]
                observation = (f"{d['title']}: {label} {_fmt(value, d['unit'])} during the window"
                               f" (pre-incident average {_fmt(baseline, d['unit'])})")
            ev.metrics.append(EvidenceItem(
                id=f"M-{d['name']}", source="prometheus", title=d["title"], observation=observation,
                value=value, baseline=baseline, unit=d["unit"], query=d["expr"],
                link=self._explore("prometheus", {"expr": d["expr"]}, ev),
                attributes={"samples": len(during), "aggregation": d["agg"]},
            ))
        self._collect_termination_reason(ev)

    def _collect_termination_reason(self, ev: IncidentEvidence) -> None:
        label = "k8s_container_status_last_terminated_reason"
        expr = (f'max by ({label}) (k8s_container_restarts{{'
                f'k8s_namespace_name="{self.s.namespace}",k8s_container_name="{self.s.service}",{label}!=""}})')
        response = self.http.get(f"{self.s.prometheus_url}/api/v1/query",
                                 params={"query": expr, "time": ev.time_window.end.timestamp()})
        response.raise_for_status()
        reasons = sorted({r["metric"].get(label, "") for r in response.json()["data"]["result"]} - {""})
        if reasons:
            ev.metrics.append(EvidenceItem(
                id="M-last_terminated_reason", source="prometheus", title="Last container termination reason",
                observation=f"Kubernetes reports the previous container instance terminated with reason: {', '.join(reasons)}",
                query=expr, link=self._explore("prometheus", {"expr": expr}, ev), attributes={"reasons": reasons},
            ))

    # ---------------------------------------------------------------- Tempo
    def _collect_traces(self, ev: IncidentEvidence) -> None:
        svc = f'resource.service.name="{self.s.service}"'
        searches = (
            ("T-slow", "Slow traces", f"{{{svc} && kind=server && duration>{SLOW_TRACE_THRESHOLD_MS}ms}}"),
            ("T-error", "Error traces", f"{{{svc} && status=error}}"),
        )
        for item_id, title, traceql in searches:
            response = self.http.get(f"{self.s.tempo_url}/api/search", params={
                "q": traceql, "limit": self.s.max_traces,
                "start": int(ev.time_window.start.timestamp()), "end": int(ev.time_window.end.timestamp()) + 1})
            response.raise_for_status()
            traces = sorted(response.json().get("traces") or [], key=lambda t: t.get("durationMs", 0), reverse=True)
            if not traces:
                continue  # absence of slow/error traces is a valid observation, not missing data
            exemplar_id = traces[0]["traceID"]
            detail = self._analyse_trace(exemplar_id)
            more = "at least " if len(traces) >= self.s.max_traces else ""
            if item_id == "T-slow":
                observation = (f"{more}{len(traces)} traces with a {self.s.service} server span slower than "
                               f"{SLOW_TRACE_THRESHOLD_MS} ms. Slowest sampled trace {exemplar_id}: server span "
                               f"{detail['server_ms']:.0f} ms, of which span '{detail['dominant_span']}' took "
                               f"{detail['dominant_ms']:.0f} ms ({detail['dominant_share_pct']:.0f}%)")
            else:
                observation = (f"{more}{len(traces)} traces with an error-status span in {self.s.service}. "
                               f"Sampled trace {exemplar_id}: span '{detail['error_span']}' failed"
                               + (f" with {detail['exception_type']}" if detail["exception_type"] else ""))
            ev.traces.append(EvidenceItem(
                id=item_id, source="tempo", title=title, observation=observation,
                value=detail["server_ms"] / 1000 if detail["server_ms"] else None, unit="s",
                query=traceql, link=self._explore("tempo", {"queryType": "traceql", "query": exemplar_id}, ev),
                attributes={"count": len(traces), "trace_ids": [t["traceID"] for t in traces[:10]],
                            "exemplar_trace_id": exemplar_id,
                            **{k: v for k, v in detail.items() if k != "exception_message"}},
                untrusted_text=[detail["exception_message"]] if detail["exception_message"] else [],
            ))

    def _analyse_trace(self, trace_id: str) -> dict:
        """Timing breakdown of one trace, restricted to the investigated service."""
        response = self.http.get(f"{self.s.tempo_url}/api/traces/{trace_id}")
        response.raise_for_status()
        body = response.json()
        spans, services = [], set()
        for batch in body.get("batches") or body.get("resourceSpans") or []:
            attrs = {a["key"]: next(iter(a["value"].values()), None) for a in batch.get("resource", {}).get("attributes", [])}
            service = attrs.get("service.name")
            services.add(service)
            for scope in batch.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    spans.append({**span, "_service": service,
                                  "_ms": (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6})
        own = [s for s in spans if s["_service"] == self.s.service]
        is_server = lambda s: s.get("kind") in ("SPAN_KIND_SERVER", 2)
        is_error = lambda s: s.get("status", {}).get("code") in ("STATUS_CODE_ERROR", 2)
        server = max((s for s in own if is_server(s)), key=lambda s: s["_ms"], default=None)
        inner = [s for s in own if not is_server(s) and not s["name"].endswith(("http send", "http receive"))]
        dominant = max(inner, key=lambda s: s["_ms"], default=None)
        failed = next((s for s in inner if is_error(s)), None) or next((s for s in own if is_error(s)), None)
        exc_type = exc_message = None
        for span in own:
            for event in span.get("events", []):
                if event.get("name") == "exception":
                    attrs = {a["key"]: next(iter(a["value"].values()), None) for a in event.get("attributes", [])}
                    exc_type = exc_type or attrs.get("exception.type")
                    exc_message = exc_message or attrs.get("exception.message")
        server_ms = server["_ms"] if server else 0.0
        return {
            "span_count": len(spans), "services": sorted(s for s in services if s),
            "server_ms": round(server_ms, 1),
            "dominant_span": dominant["name"] if dominant else None,
            "dominant_ms": round(dominant["_ms"], 1) if dominant else 0.0,
            "dominant_share_pct": round(100 * dominant["_ms"] / server_ms, 1) if dominant and server_ms else 0.0,
            "error_span": failed["name"] if failed else None,
            "exception_type": sanitize_text(exc_type, 80) if exc_type else None,
            "exception_message": sanitize_text(exc_message) if exc_message else None,
        }

    # ----------------------------------------------------------------- Loki
    def _collect_logs(self, ev: IncidentEvidence) -> None:
        start, end = ev.time_window.start, ev.time_window.end
        # Test-harness markers are excluded: they name the injected fault, and the
        # analysis must reach its conclusion from symptoms alone.
        selector = (f'{{service_name="{self.s.service}"}} | severity_text=~"{LOG_SEVERITIES}"'
                    f' | event!~"fault_injection_.*"')
        window_s = max(1, int((end - start).total_seconds()))
        count_query = f"sum by (severity_text, event) (count_over_time({selector} [{window_s}s]))"
        response = self.http.get(f"{self.s.loki_url}/loki/api/v1/query",
                                 params={"query": count_query, "time": int(end.timestamp() * 1e9)})
        response.raise_for_status()
        counts = {(r["metric"].get("severity_text", "?"), r["metric"].get("event", "other")): int(float(r["value"][1]))
                  for r in response.json()["data"]["result"]}

        groups: dict[tuple, dict] = {}
        for labels, ts, line in self._loki_lines(selector, start, end):
            key = (labels.get("severity_text", "?"), labels.get("event", "other"))
            group = groups.setdefault(key, {"lines": [], "trace_ids": [], "first_ns": ts})
            if len(group["lines"]) < 3:
                group["lines"].append(sanitize_text(line))
            trace_id = labels.get("trace_id")
            if trace_id and trace_id not in group["trace_ids"] and len(group["trace_ids"]) < 20:
                group["trace_ids"].append(trace_id)
        for key in sorted(set(counts) | set(groups)):
            severity, event = key
            group = groups.get(key, {"lines": [], "trace_ids": [], "first_ns": None})
            total = counts.get(key, len(group["lines"]))
            ev.logs.append(EvidenceItem(
                id=f"L-{severity.lower()}-{event}", source="loki", title=f"{severity} logs: {event}",
                observation=f"{total} {severity} log records with event '{event}' from {self.s.service} in the window",
                value=float(total), unit="records", query=selector,
                link=self._explore("loki", {"expr": selector}, ev),
                attributes={"count": total, "severity": severity, "event": event,
                            "trace_ids": group["trace_ids"], "first_timestamp_ns": group["first_ns"]},
                untrusted_text=group["lines"],
            ))
        self._collect_trace_linked_logs(ev)

    def _collect_trace_linked_logs(self, ev: IncidentEvidence) -> None:
        """Logs carrying the trace id of each sampled trace: the log<->trace join."""
        for trace_item in ev.traces:
            trace_id = trace_item.attributes["exemplar_trace_id"]
            query = f'{{service_name=~".+"}} | trace_id="{trace_id}"'
            lines = list(self._loki_lines(query, ev.time_window.start, ev.time_window.end, limit=20))
            if not lines:
                continue
            ev.logs.append(EvidenceItem(
                id=f"L-trace-{trace_id[:12]}", source="loki", title=f"Logs for trace {trace_id[:12]}…",
                observation=f"{len(lines)} log records carry trace id {trace_id}, linking them to {trace_item.id}",
                value=float(len(lines)), unit="records", query=query, link=self._explore("loki", {"expr": query}, ev),
                attributes={"count": len(lines), "trace_id": trace_id, "linked_trace_evidence": trace_item.id,
                            "severities": sorted({l[0].get("severity_text", "?") for l in lines})},
                untrusted_text=[sanitize_text(line) for _, _, line in lines[:3]],
            ))

    def _loki_lines(self, query: str, start: datetime, end: datetime, limit: int | None = None):
        response = self.http.get(f"{self.s.loki_url}/loki/api/v1/query_range", params={
            "query": query, "start": int(start.timestamp() * 1e9), "end": int(end.timestamp() * 1e9),
            "limit": limit or self.s.max_log_lines, "direction": "forward"})
        response.raise_for_status()
        for stream in response.json()["data"]["result"]:
            for ts, line in stream["values"]:
                yield stream["stream"], int(ts), line

    # ----------------------------------------------------------- Kubernetes
    def _k8s_client(self) -> httpx.Client:
        if self._k8s is None:
            creds = Path(self.s.k8s_credentials_dir)
            token = (creds / "token").read_text().strip()  # read-only service account
            self._k8s = httpx.Client(
                base_url=self.s.k8s_api_url, verify=ssl.create_default_context(cafile=str(creds / "ca.crt")),
                headers={"Authorization": f"Bearer {token}"}, timeout=self.s.http_timeout_s)
        return self._k8s

    def _k8s_get(self, path: str, **params) -> dict:
        client = self._k8s_client()
        try:
            response = client.get(path, params=params)
        except httpx.ConnectError:
            # One retry: host antivirus TLS inspection can break a single handshake.
            response = client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def workload_status(self) -> list[dict]:
        """Current deployment readiness in the demo namespace (read-only)."""
        deployments = self._k8s_get(f"/apis/apps/v1/namespaces/{self.s.namespace}/deployments")
        return [{"deployment": d["metadata"]["name"],
                 "desired": d["spec"].get("replicas", 0),
                 "ready": d.get("status", {}).get("readyReplicas", 0),
                 "available": d.get("status", {}).get("availableReplicas", 0)}
                for d in deployments.get("items", [])]

    def _collect_kubernetes(self, ev: IncidentEvidence) -> None:
        ns = self.s.namespace
        lo, hi = ev.time_window.start - K8S_WINDOW_SLACK, ev.time_window.end + K8S_WINDOW_SLACK
        pods_path = f"/api/v1/namespaces/{ns}/pods"
        pods = self._k8s_get(pods_path, labelSelector=f"app={self.s.service}")
        for pod in pods.get("items", []):
            name = pod["metadata"]["name"]
            for status in pod.get("status", {}).get("containerStatuses", []):
                ev.kubernetes_events.append(EvidenceItem(
                    id=f"K-status-{name}", source="kubernetes", title=f"Pod {name} container status",
                    observation=(f"Container '{status['name']}' in pod {name}: ready={status['ready']}, "
                                 f"restartCount={status['restartCount']}"),
                    value=float(status["restartCount"]), unit="restarts",
                    query=f"GET {pods_path}?labelSelector=app={self.s.service}",
                    attributes={"pod": name, "ready": status["ready"], "restart_count": status["restartCount"]},
                ))
                terminated = status.get("lastState", {}).get("terminated")
                finished = _parse_time(terminated.get("finishedAt")) if terminated else None
                if finished and lo <= finished <= hi:
                    ev.kubernetes_events.append(EvidenceItem(
                        id=f"K-terminated-{name}", source="kubernetes", title=f"Pod {name} container terminated",
                        observation=(f"Container '{status['name']}' in pod {name} was terminated at "
                                     f"{finished.isoformat()} with reason {terminated.get('reason')} "
                                     f"(exit code {terminated.get('exitCode')})"),
                        query=f"GET {pods_path}?labelSelector=app={self.s.service}",
                        attributes={"pod": name, "reason": terminated.get("reason"),
                                    "exit_code": terminated.get("exitCode"), "finished_at": finished.isoformat()},
                    ))
        events_path = f"/api/v1/namespaces/{ns}/events"
        grouped: dict[str, dict] = {}
        for event in self._k8s_get(events_path).get("items", []):
            when = _parse_time(event.get("lastTimestamp") or event.get("eventTime") or event.get("firstTimestamp"))
            involved = event.get("involvedObject", {}).get("name", "")
            if not when or not (lo <= when <= hi) or not involved.startswith(self.s.service):
                continue
            group = grouped.setdefault(event.get("reason", "Unknown"),
                                       {"count": 0, "type": event.get("type"), "messages": []})
            group["count"] += int(event.get("count") or 1)
            if len(group["messages"]) < 2:
                group["messages"].append(sanitize_text(event.get("message", "")))
        for reason, group in sorted(grouped.items()):
            ev.kubernetes_events.append(EvidenceItem(
                id=f"K-event-{reason}", source="kubernetes", title=f"Kubernetes event: {reason}",
                observation=f"{group['count']} '{reason}' ({group['type']}) events for {self.s.service} pods around the window",
                value=float(group["count"]), unit="events", query=f"GET {events_path}",
                attributes={"reason": reason, "type": group["type"], "count": group["count"]},
                untrusted_text=group["messages"],
            ))

    # ---------------------------------------------------------------- links
    def _explore(self, datasource: str, query: dict, ev: IncidentEvidence) -> str:
        pad = timedelta(minutes=2)
        panes = {"a": {"datasource": datasource,
                       "queries": [{"refId": "A", "datasource": {"uid": datasource}, **query}],
                       "range": {"from": str(int((ev.time_window.start - pad).timestamp() * 1000)),
                                 "to": str(int((ev.time_window.end + pad).timestamp() * 1000))}}}
        return f"{self.s.grafana_public_url}/explore?schemaVersion=1&orgId=1&panes={quote(json.dumps(panes))}"
