"""Steady background traffic so the service always has a real baseline.

Runs as its own Deployment with service.name=traffic-generator. The httpx
instrumentation propagates W3C trace context, so every request yields one
distributed trace spanning both services, plus client-side metrics that keep
reporting even while demo-api itself is down.
"""
import logging
import os
import time

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

from telemetry import setup_telemetry

setup_telemetry()
HTTPXClientInstrumentor().instrument()

logger = logging.getLogger("demo.loadgen")
TARGET = os.getenv("TARGET_URL", "http://demo-api:8000/api/work")
RATE = min(float(os.getenv("REQUESTS_PER_SECOND", "2")), 20.0)


def main() -> None:
    interval = 1.0 / RATE
    logger.info("traffic generator started", extra={"target": TARGET, "rate_rps": RATE})
    with httpx.Client(timeout=5.0) as client:
        while True:
            started = time.monotonic()
            try:
                client.get(TARGET)
            except httpx.HTTPError as exc:
                logger.warning(
                    "request to demo-api failed: %s", type(exc).__name__,
                    extra={"event": "client_request_failed", "error_type": type(exc).__name__},
                )
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
