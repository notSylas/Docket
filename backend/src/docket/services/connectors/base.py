"""Connector protocol and shared helpers (provider independent)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator, Protocol, runtime_checkable

from docket.core.privacy import redact_text


class ConnectorError(RuntimeError):
    """A failure whose message is already scrubbed of secrets and mail content."""


class ConsentRequired(ConnectorError):
    pass


def scrub(text: object, secrets: tuple[str, ...] = ()) -> str:
    """Remove known secrets, then apply the shared log redaction patterns."""
    value = str(text)
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[secret]")
            value = value.replace("".join(secret.split()), "[secret]")
    return redact_text(value)[:300]


@dataclass(frozen=True)
class Change:
    """One remote change. `raw` is RFC 822 bytes for an add/update; `deleted`
    means the message left the synced scope (deleted, moved or out of folder)."""

    remote_id: str
    folder: str
    raw: bytes | None = None
    deleted: bool = False


@dataclass
class ChangeBatch:
    """Iterable of changes. `cursor` is final only after full iteration, so a
    caller persists it only after every change was applied."""

    changes: Iterator[Change]
    cursor: dict = field(default_factory=dict)
    skipped: int = 0
    truncated: bool = False

    def __iter__(self) -> Iterator[Change]:
        return self.changes


@runtime_checkable
class Connector(Protocol):
    provider: str
    allowed_hosts: tuple[str, ...]

    def validate_config(self) -> None: ...
    def authenticate(self) -> str:
        """Return an opaque credential reference (never the secret itself)."""
    def list_changes(self, since_cursor: dict | None) -> ChangeBatch: ...
    def disconnect(self) -> None: ...


def account_env_suffix(account: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", account.upper()).strip("_")


def secret_env_name(prefix: str, account: str) -> str:
    return f"{prefix}{account_env_suffix(account)}"


def resolve_env_secret(name: str, environ=None) -> str | None:
    value = (os.environ if environ is None else environ).get(name)
    return value.strip() if value and value.strip() else None
    # TODO: if `keyring` is ever added as a dependency, fall back to
    # keyring.get_password("docket", account) when the env var is unset.


SecretResolver = Callable[[], str | None]
