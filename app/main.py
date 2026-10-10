"""Instrumented demo workload: a small FastAPI service that does fake 'work'."""
import asyncio
import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from incident_simulator import MAX_DELAY_MS, MAX_DURATION_S, FaultState
from telemetry import setup_telemetry

setup_telemetry()

logger = logging.getLogger("demo.api")
tracer = trace.get_tracer("demo.api")
meter = metrics.get_meter("demo.api")

POD_NAME = os.getenv("POD_NAME", "local")
FAULT_INJECTION_ENABLED = os.getenv("FAULT_INJECTION_ENABLED", "false").lower() == "true"
STARTED_AT = time.time()
faults = FaultState()

work_units = meter.create_counter(
    "demo.work.units", unit="{unit}", description="Work units processed successfully"
)


def _observe_faults(_: CallbackOptions):
    # Feeds Grafana annotations only. The analysis engine never reads this
    # metric: it must infer the cause from symptoms, not from the trigger.
    return [Observation(int(on), {"scenario": name}) for name, on in faults.active().items()]


def _observe_status(_: CallbackOptions):
    return [Observation(0 if any(faults.active().values()) else 1)]


meter.create_observable_gauge(
    "demo.fault_injection.active", callbacks=[_observe_faults],
    description="1 while a controlled fault is injected (annotation source only)",
)
meter.create_observable_gauge(
    "demo.service.status", callbacks=[_observe_status],
    description="1 = operating normally, 0 = degraded",
)

app = FastAPI(title="demo-api", docs_url=None, redoc_url=None)


class DependencyError(RuntimeError):
    pass


@app.middleware("http")
async def correlation_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    span = trace.get_current_span()
    span.set_attribute("request.id", request_id)
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    ctx = span.get_span_context()
    if ctx.is_valid:
        response.headers["x-trace-id"] = format(ctx.trace_id, "032x")
    return response


@app.get("/health")
async def health():
    return {"status": "alive"}


@app.get("/ready")
async def ready():
    return {"status": "ready"}


@app.get("/api/status")
async def status():
    return {
        "service": "demo-api",
        "pod": POD_NAME,
        "uptime_s": round(time.time() - STARTED_AT, 1),
        "active_faults": faults.active(),
        "fault_injection_enabled": FAULT_INJECTION_ENABLED,
    }


async def _call_dependency(request_id: str) -> None:
    """Stands in for a downstream call; this is where latency and errors land."""
    with tracer.start_as_current_span("dependency.call") as span:
        span.set_attribute("peer.service", "simulated-inventory")
        delay = faults.current_delay_s()
        await asyncio.sleep(0.02 + delay)
        if delay:
            span.set_attribute("dependency.slow", True)
            logger.warning(
                "dependency call slow: took %d ms", int((0.02 + delay) * 1000),
                extra={"event": "dependency_slow", "request_id": request_id,
                       "duration_ms": int((0.02 + delay) * 1000)},
            )
        if faults.should_fail():
            raise DependencyError("simulated-inventory returned an unexpected failure")


@app.get("/api/work")
async def work(request: Request, units: int = 1):
    request_id = request.state.request_id
    units = max(1, min(units, 100))
    try:
        with tracer.start_as_current_span("work.validate"):
            await asyncio.sleep(0.002)
        await _call_dependency(request_id)
        with tracer.start_as_current_span("work.compute") as span:
            span.set_attribute("work.units", units)
            checksum = sum(i * i for i in range(units * 2000))
    except DependencyError as exc:
        span = trace.get_current_span()
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        logger.error(
            "work request failed: %s", exc,
            extra={"event": "work_failed", "request_id": request_id,
                   "error_type": type(exc).__name__},
        )
        return JSONResponse(
            status_code=500, content={"error": "dependency_failure", "request_id": request_id}
        )
    work_units.add(units)
    logger.info("work completed", extra={"event": "work_completed", "request_id": request_id, "units": units})
    return {"result": checksum % 9973, "units": units, "pod": POD_NAME, "request_id": request_id}


class LatencyFault(BaseModel):
    delay_ms: int = Field(800, ge=1, le=MAX_DELAY_MS)
    duration_s: int = Field(90, ge=1, le=MAX_DURATION_S)


class ErrorFault(BaseModel):
    error_rate: float = Field(0.5, gt=0, le=1)
    duration_s: int = Field(90, ge=1, le=MAX_DURATION_S)


if FAULT_INJECTION_ENABLED:

    @app.post("/incident/latency")
    async def incident_latency(fault: LatencyFault):
        return faults.inject_latency(fault.delay_ms, fault.duration_s)

    @app.post("/incident/errors")
    async def incident_errors(fault: ErrorFault):
        return faults.inject_errors(fault.error_rate, fault.duration_s)

    @app.post("/incident/memory")
    async def incident_memory():
        return faults.inject_memory_pressure()

    @app.post("/incident/reset")
    async def incident_reset():
        return faults.reset()


FastAPIInstrumentor.instrument_app(app)
logger.info("demo-api started", extra={"event": "service_started", "pod": POD_NAME})
