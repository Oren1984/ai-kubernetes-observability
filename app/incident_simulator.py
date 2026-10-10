"""Controlled, bounded fault injection for the three demo incident scenarios.

Every fault has a hard upper bound and a recovery condition:
  latency / errors  -> expire automatically after `duration_s` (or on reset)
  memory            -> allocation is capped by MEMORY_MAX_MB and by the
                       container memory limit; the kubelet restarts the
                       container, which clears all in-process state.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("demo.incident")

MAX_DURATION_S = 300
MAX_DELAY_MS = 3000
MEMORY_MAX_MB = 512
MEMORY_CHUNK_MB = 3
MEMORY_INTERVAL_S = 1.0


@dataclass
class FaultState:
    latency_ms: int = 0
    latency_until: float = 0.0
    error_rate: float = 0.0
    error_until: float = 0.0
    memory_active: bool = False
    _error_acc: float = 0.0
    _hog: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inject_latency(self, delay_ms: int, duration_s: int) -> dict:
        delay_ms = max(1, min(delay_ms, MAX_DELAY_MS))
        duration_s = max(1, min(duration_s, MAX_DURATION_S))
        self.latency_ms, self.latency_until = delay_ms, time.time() + duration_s
        logger.warning(
            "fault injection started: latency",
            extra={"event": "fault_injection_started", "scenario": "latency",
                   "delay_ms": delay_ms, "duration_s": duration_s},
        )
        return {"scenario": "latency", "delay_ms": delay_ms, "duration_s": duration_s}

    def inject_errors(self, error_rate: float, duration_s: int) -> dict:
        error_rate = max(0.01, min(error_rate, 1.0))
        duration_s = max(1, min(duration_s, MAX_DURATION_S))
        self.error_rate, self.error_until = error_rate, time.time() + duration_s
        self._error_acc = 0.0
        logger.warning(
            "fault injection started: errors",
            extra={"event": "fault_injection_started", "scenario": "errors",
                   "error_rate": error_rate, "duration_s": duration_s},
        )
        return {"scenario": "errors", "error_rate": error_rate, "duration_s": duration_s}

    def inject_memory_pressure(self) -> dict:
        with self._lock:
            if self.memory_active:
                return {"scenario": "memory", "already_active": True}
            self.memory_active = True
        logger.warning(
            "fault injection started: memory",
            extra={"event": "fault_injection_started", "scenario": "memory",
                   "chunk_mb": MEMORY_CHUNK_MB, "max_mb": MEMORY_MAX_MB},
        )
        threading.Thread(target=self._grow_memory, daemon=True).start()
        return {"scenario": "memory", "chunk_mb": MEMORY_CHUNK_MB,
                "interval_s": MEMORY_INTERVAL_S, "max_mb": MEMORY_MAX_MB}

    def _grow_memory(self) -> None:
        held_mb = 0
        while self.memory_active and held_mb < MEMORY_MAX_MB:
            # Multiplying a non-zero byte forces the pages to be really resident.
            self._hog.append(bytearray(b"\x01") * (MEMORY_CHUNK_MB * 1024 * 1024))
            held_mb += MEMORY_CHUNK_MB
            if held_mb % 15 == 0:
                logger.warning(
                    "memory usage growing: retained buffers at %d MiB", held_mb,
                    extra={"event": "memory_growth", "retained_mb": held_mb},
                )
            time.sleep(MEMORY_INTERVAL_S)

    def reset(self) -> dict:
        self.latency_ms, self.latency_until = 0, 0.0
        self.error_rate, self.error_until = 0.0, 0.0
        self.memory_active = False
        self._hog.clear()
        logger.info("fault injection reset", extra={"event": "fault_injection_reset"})
        return {"reset": True}

    def current_delay_s(self) -> float:
        return self.latency_ms / 1000 if time.time() < self.latency_until else 0.0

    def should_fail(self) -> bool:
        """Deterministic: fails exactly `error_rate` of calls, evenly spaced."""
        if time.time() >= self.error_until:
            return False
        with self._lock:
            self._error_acc += self.error_rate
            if self._error_acc >= 1.0:
                self._error_acc -= 1.0
                return True
        return False

    def active(self) -> dict[str, bool]:
        now = time.time()
        return {
            "latency": now < self.latency_until,
            "errors": now < self.error_until,
            "memory": self.memory_active,
        }
