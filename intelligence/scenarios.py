"""Test harness for the three controlled incidents: trigger, reset, recovery check.

This is deliberately separate from the analysis and review workflow. It talks
only to the demo service's fault-injection endpoints; it is the single place
that changes workload behaviour, and it is driven by a human or a test, never
by an analysis result.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from .audit import AuditStore
from .config import Settings
from .correlation import ERROR_RATIO_MIN, LATENCY_P95_MIN_S
from .schemas import utcnow


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    path: str
    payload: dict | None
    duration_s: int        # how long the incident window is expected to last
    cooldown_s: int        # minimum gap before the same scenario may run again
    expected_category: str  # ground truth for tests; never passed to the analysis


SCENARIOS = {
    "latency": Scenario(
        "latency", "A — Latency degradation",
        "Adds 800 ms to the service's downstream dependency call for 90 seconds. Requests stay successful but slow.",
        "/incident/latency", {"delay_ms": 800, "duration_s": 90}, 90, 30, "latency_degradation"),
    "errors": Scenario(
        "errors", "B — Elevated error rate",
        "Makes the dependency call fail for every second request for 90 seconds, producing HTTP 500 responses.",
        "/incident/errors", {"error_rate": 0.5, "duration_s": 90}, 90, 30, "application_errors"),
    "memory": Scenario(
        "memory", "C — Memory exhaustion (OOM kill)",
        "Grows process memory by 3 MiB per second until the container hits its 192 MiB limit. "
        "Kubernetes OOM-kills and restarts it; the restart clears the fault.",
        "/incident/memory", None, 120, 180, "memory_exhaustion"),
}


class ScenarioError(Exception):
    pass


class ScenarioRunner:
    def __init__(self, settings: Settings, store: AuditStore, client: httpx.Client | None = None):
        self.s, self.store = settings, store
        self.http = client or httpx.Client(timeout=settings.http_timeout_s)

    def active_incident(self) -> dict | None:
        now = utcnow().isoformat()
        return next((i for i in self.store.list_incidents()
                     if i["origin"] == "scenario" and i["ended_at"] > now), None)

    def cooldown_remaining(self, key: str) -> int:
        scenario = SCENARIOS[key]
        last = next((i for i in self.store.list_incidents() if i["scenario"] == key), None)
        if not last:
            return 0
        ready_at = datetime.fromisoformat(last["ended_at"]) + timedelta(seconds=scenario.cooldown_s)
        return max(0, int((ready_at - utcnow()).total_seconds()))

    def trigger(self, key: str, actor: str = "operator") -> str:
        """Start a scenario and record the incident window. One at a time."""
        scenario = SCENARIOS[key]
        active = self.active_incident()
        if active:
            raise ScenarioError(f"{active['id']} is still running; wait for it to finish")
        remaining = self.cooldown_remaining(key)
        if remaining:
            raise ScenarioError(f"scenario '{key}' is cooling down for another {remaining}s")
        started = utcnow()
        response = self.http.post(f"{self.s.demo_api_url}{scenario.path}", json=scenario.payload)
        response.raise_for_status()
        return self.store.create_incident(
            self.s.service, started, started + timedelta(seconds=scenario.duration_s),
            origin="scenario", scenario=key, actor=actor)

    def reset(self, actor: str = "operator") -> None:
        """Harness cleanup: clear injected latency/error faults."""
        self.http.post(f"{self.s.demo_api_url}/incident/reset").raise_for_status()
        self.store.log_event(None, "fault_injection_reset", actor, {"via": "test harness"})

    def check_recovery(self, incident_id: str | None = None) -> dict:
        """Healthy = service answers, and current telemetry is back under thresholds."""
        checks: dict[str, bool | None] = {}
        try:
            checks["service_ready"] = self.http.get(f"{self.s.demo_api_url}/ready").status_code == 200
            checks["no_active_fault"] = not any(
                self.http.get(f"{self.s.demo_api_url}/api/status").json()["active_faults"].values())
        except (httpx.HTTPError, KeyError, ValueError):
            checks["service_ready"] = checks["no_active_fault"] = False
        svc = f'service_name="{self.s.service}",http_route="/api/work"'
        count = "http_server_request_duration_seconds_count"
        queries = {
            "error_ratio_normal": (f'(sum(rate({count}{{{svc},http_response_status_code=~"5.."}}[30s])) or vector(0))'
                                   f' / sum(rate({count}{{{svc}}}[30s]))', lambda v: v < ERROR_RATIO_MIN),
            "latency_normal": (f'histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{svc}}}[30s])))',
                               lambda v: v < LATENCY_P95_MIN_S),
            "container_ready": (f'min(k8s_container_ready{{k8s_namespace_name="{self.s.namespace}",k8s_container_name="{self.s.service}"}})',
                                lambda v: v >= 1),
        }
        values = {}
        for name, (expr, healthy) in queries.items():
            try:
                result = self.http.get(f"{self.s.prometheus_url}/api/v1/query", params={"query": expr}).json()["data"]["result"]
                values[name] = float(result[0]["value"][1]) if result else None
                checks[name] = healthy(values[name]) if values[name] is not None and values[name] == values[name] else None
            except (httpx.HTTPError, KeyError, ValueError):
                checks[name] = None  # telemetry unavailable: recovery cannot be confirmed
        status = {"healthy": all(v is True for v in checks.values()), "checks": checks, "values": values}
        if incident_id and status["healthy"]:
            self.store.mark_recovered(incident_id, status)
        return status
