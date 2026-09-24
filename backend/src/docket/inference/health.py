"""Startup health check for the local Ollama server. Never raises."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_HOST = "http://127.0.0.1:11434"


@dataclass
class HealthReport:
    reachable: bool
    missing_models: list[str] = field(default_factory=list)
    error: str | None = None
    host: str = DEFAULT_HOST

    @property
    def ok(self) -> bool:
        return self.reachable and not self.missing_models


def _normalize(name: str) -> str:
    name = name.strip()
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _installed_names(response: Any) -> set[str]:
    names: set[str] = set()
    for entry in _get(response, "models") or []:
        name = _get(entry, "model") or _get(entry, "name")
        if name:
            names.add(_normalize(str(name)))
    return names


def _resolve_host(host: str | None) -> str:
    if host:
        return host
    import os

    return os.environ.get("OLLAMA_HOST") or DEFAULT_HOST


def check_ollama(
    required_models: list[str],
    host: str | None = None,
    timeout: float = 1.5,
    client: Any | None = None,
) -> HealthReport:
    resolved = _resolve_host(host)
    try:
        if client is None:
            import ollama

            client = ollama.Client(host=resolved, timeout=timeout)
        installed = _installed_names(client.list())
        missing = [m for m in required_models if _normalize(m) not in installed]
        return HealthReport(reachable=True, missing_models=missing, host=resolved)
    except Exception as exc:  # noqa: BLE001 -- health checks must never raise
        msg = (str(exc) or type(exc).__name__).splitlines()[0][:120]
        return HealthReport(reachable=False, error=msg, host=resolved)


def format_health_warning(report: HealthReport | None, settings: Any = None) -> list[str]:
    if report is None:
        return []
    if not report.reachable:
        return [
            f"Can't reach Ollama at {report.host} — is it running? "
            "Start it with: ollama serve"
        ]
    return [
        f"Model not found: {m} — run: ollama pull {m}" for m in report.missing_models
    ]
