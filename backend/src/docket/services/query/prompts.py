"""Re-export shim.

`SYSTEM_PROMPT`/`AGENT_SYSTEM_PROMPT` (and friends) and `ABSTENTION_PHRASE`
moved to `docket.prompts.query`/`docket.prompts.shared`; `build_context_block`/
`validate_citations` moved to `docket.services.query.citations` (prompt-adjacent
logic, not prompt text -- see that module). This module stays as a thin
re-export so existing `from docket.services.query.prompts import ...` call sites
(`query/service.py`'s prompt-name imports, `eval/scoring.py`'s
`ABSTENTION_PHRASE` import) need zero changes. Mirrors the
`cli/interactive/__init__.py` re-export precedent.
"""

from __future__ import annotations

from docket.prompts.query import (
    AGENT_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT_WITH_HISTORY,
    REWRITE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_WITH_HISTORY,
)
from docket.prompts.shared import ABSTENTION_PHRASE

__all__ = [
    "ABSTENTION_PHRASE",
    "AGENT_SYSTEM_PROMPT",
    "AGENT_SYSTEM_PROMPT_WITH_HISTORY",
    "REWRITE_SYSTEM_PROMPT",
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_WITH_HISTORY",
]
