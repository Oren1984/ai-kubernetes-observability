"""Helpers shared by the integration and E2E suites (both need the running stack)."""
import ssl
import time

import httpx

from intelligence.config import load_settings

SETTINGS = load_settings()
HTTP = httpx.Client(timeout=10)


def wait_for(probe, timeout_s: float, what: str, interval_s: float = 3):
    """Poll `probe` until it returns something truthy; fail with a clear message."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last = exc
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}; last result: {last!r}")


def prom(query: str) -> list[dict]:
    response = HTTP.get(f"{SETTINGS.prometheus_url}/api/v1/query", params={"query": query})
    response.raise_for_status()
    return response.json()["data"]["result"]


def loki(query: str, since_s: int = 600, limit: int = 50) -> list[tuple[dict, str]]:
    now = time.time()
    response = HTTP.get(f"{SETTINGS.loki_url}/loki/api/v1/query_range", params={
        "query": query, "start": int((now - since_s) * 1e9), "end": int((now + 5) * 1e9), "limit": limit})
    response.raise_for_status()
    return [(s["stream"], line) for s in response.json()["data"]["result"] for _, line in s["values"]]


def k8s(path: str, method: str = "GET", json: dict | None = None) -> httpx.Response:
    """Kubernetes API call with the read-only evidence-reader token."""
    creds = SETTINGS.k8s_credentials_dir
    with open(f"{creds}/token") as handle:
        token = handle.read().strip()
    return httpx.request(method, f"{SETTINGS.k8s_api_url}{path}", json=json, verify=ssl.create_default_context(cafile=f"{creds}/ca.crt"),
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
