"""Exception -> user-facing message translation for the interactive session.

Kept as a stateless free function (rather than a `_Session` method) so it can
be called from both the REPL loop's last-resort handler and
`source_commands.first_run_offer`'s own error handling, with no dependency on
anything but the `session` object it's handed (for `.error`/`.say`/`.context`).
"""

from __future__ import annotations

import os
import re
from typing import Any

from docket.infra.inference.gateway import (
    InferenceError,
    InferenceUnavailableError,
    ModelNotFoundError,
)
from docket.services.ingestion.pipeline import SourceNotActiveError
from docket.services.sources.manager import SourceNotFoundError


def handle_error(session: Any, exc: BaseException) -> None:
    if os.environ.get("DOCKET_DEBUG"):
        import traceback

        session.error(traceback.format_exc())
    if isinstance(exc, InferenceUnavailableError):
        session.error("Can't reach Ollama — is it running? (ollama serve)")
        session.say(str(exc), style="dim")
    elif isinstance(exc, ModelNotFoundError):
        match = re.search(r"[Mm]odel '([^']+)'", str(exc))
        model = match.group(1) if match else getattr(
            getattr(session.context, "settings", None), "gen_model", "<model>"
        )
        session.error(f"Model not found: {model} — run: ollama pull {model}")
        session.say(str(exc), style="dim")
    elif isinstance(exc, InferenceError):
        session.error(f"Inference error: {exc}")
    elif isinstance(exc, (SourceNotFoundError, SourceNotActiveError)):
        session.error(f"Error: {exc}")
    elif isinstance(exc, ValueError):
        session.error(f"Error: {exc}")
    else:
        session.error(f"Unexpected error ({type(exc).__name__}): {exc}")
