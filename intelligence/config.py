"""Runtime settings, all from environment variables. Defaults target a host
shell next to the running stack; docker-compose.yml overrides them for the
dashboard container."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = "http://127.0.0.1:19090"
    loki_url: str = "http://127.0.0.1:13100"
    tempo_url: str = "http://127.0.0.1:13200"
    grafana_url: str = "http://127.0.0.1:13000"
    grafana_public_url: str = "http://localhost:13000"
    demo_api_url: str = "http://127.0.0.1:18080"
    k8s_api_url: str = "https://127.0.0.1:16443"
    k8s_credentials_dir: str = ".runtime/k8s"
    audit_db_path: str = "data/incidents.db"

    service: str = "demo-api"
    namespace: str = "aiobs-demo"

    llm_provider: str = "mock"  # mock | openai | none
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""

    # Bounds on every backend query.
    http_timeout_s: float = 8.0
    max_window_minutes: int = 60
    baseline_minutes: int = 5
    max_log_lines: int = 100
    max_traces: int = 20


def load_settings() -> Settings:
    defaults = Settings()
    overrides = {}
    for name in ("prometheus_url", "loki_url", "tempo_url", "grafana_url",
                 "grafana_public_url", "demo_api_url", "k8s_api_url",
                 "k8s_credentials_dir", "audit_db_path", "llm_provider",
                 "llm_base_url", "llm_api_key", "llm_model"):
        value = os.getenv(name.upper())
        if value:
            overrides[name] = value.rstrip("/") if name.endswith("_url") else value
    return Settings(**{**defaults.__dict__, **overrides})
