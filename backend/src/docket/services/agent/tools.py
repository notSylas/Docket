"""Factory functions building the real `search_knowledge`/`read_evidence`
LangChain tools that the Agent Policy Gateway's allow-list is built from.

The validation spike (`spike/test_agent_policy_gateway.py`) used two
module-level `@tool`-decorated functions that returned hardcoded, canned
results -- fine for isolating the gateway's *enforcement* logic (the thing
that spike was actually validating), but useless for a real investigation:
nothing in them touched the real retrieval/resolution stack.

Here, `search_knowledge`/`read_evidence` need to call real code
(`docket.infra.retrieval.hybrid.hybrid_search`, `docket.infra.retrieval.resolver.
EvidenceResolver`), which in turn depend on an `Engine`, a LanceDB `table`,
an `InferenceGateway`, and a `session_factory` -- none of which exist at
import time. They're only known once an agent is actually being built for a
real query session (typically per-request, since `table`/`resolver` are
tied to a specific workspace's data). So these can't be bare module-level
`@tool` functions like the spike's stubs; they have to be *factories* that
close over those dependencies and return a freshly-built tool bound to them.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from langchain_core.tools import BaseTool, tool

from docket.core.config import settings
from docket.infra.inference.gateway import InferenceGateway
from pydantic import BaseModel, Field

from docket.infra.evidence.workbook_reader import WorkbookReader, WorkbookReadError
from docket.prompts.agent import (
    CALCULATE_DESCRIPTION,
    READ_EVIDENCE_DESCRIPTION,
    READ_RANGE_DESCRIPTION,
    SEARCH_KNOWLEDGE_DESCRIPTION,
)
from docket.services.agent.calculator import safe_calculate
from docket.infra.retrieval.hybrid import hybrid_search
from docket.infra.retrieval.resolver import ChunkNotFoundError, EvidenceResolver


def make_search_knowledge_tool(
    *,
    engine: Any,
    table: Any,
    gateway: InferenceGateway,
    top_k: int = settings.default_top_k,
    manifest_guard: Any | None = None,
    page_table: Any | None = None,
    extra_queries_provider: Callable[[], list[str] | None] | None = None,
) -> BaseTool:
    """Build a `search_knowledge` tool bound to a specific engine/table/gateway.

    Replaces the spike's hardcoded fake result with a real
    `hybrid_search` call. Returns JSON: ``{"results": [{"chunk_id", "score"}, ...]}``
    -- deliberately just chunk_id + score, not the chunk text itself, so the
    model has to make a separate, deliberate `read_evidence` call (and
    therefore a separate, gateway-checked, budget-counted tool call) to see
    real content, mirroring the spike's two-tool split.

    Parity with the fast path (doc 05 section 8): `page_table` (the visual
    leg) and `manifest_guard` are passed to `hybrid_search` exactly as
    `QueryService._ask_fast` does, and `extra_queries_provider` (called at
    each search; returns the follow-up standalone rewrite QueryService computed
    once for this run, or nothing) supplies `extra_queries` for EVERY search
    call. It is a provider, not a list, because the agent graph is built once
    and reused across questions.
    """

    @tool(description=SEARCH_KNOWLEDGE_DESCRIPTION)
    def search_knowledge(query: str) -> str:
        """Model-facing description lives in `docket.prompts.agent.SEARCH_KNOWLEDGE_DESCRIPTION`."""
        extra = (extra_queries_provider() if extra_queries_provider else None) or []
        ranked = hybrid_search(
            engine=engine,
            table=table,
            gateway=gateway,
            query=query,
            top_k=top_k,
            page_table=page_table,
            manifest_guard=manifest_guard,
            **({"extra_queries": list(extra)} if extra else {}),
        )
        return json.dumps({"results": [{"chunk_id": rc.chunk_id, "score": rc.score} for rc in ranked]})

    return search_knowledge


def make_read_evidence_tool(*, resolver: EvidenceResolver) -> BaseTool:
    """Build a `read_evidence` tool bound to a specific `EvidenceResolver`.

    Replaces the spike's hardcoded fake string with a real
    `resolver.resolve()` call. Returns JSON: ``{"chunk_id", "text",
    "citation_label", "source_display_name", "heading"}``. `chunk_id` is
    included (in addition to the fields the model needs to see) so that a
    caller reconstructing citations from the agent's tool-call trace after
    the fact (see `QueryService`'s agent branch) has a real chunk_id to
    resolve per successful call, without having to thread it back out via
    some side channel.

    If `chunk_id` doesn't exist (the model invented or mistyped one --
    plausible, since chunk_ids are opaque hashes it only ever saw as
    search_knowledge output), `ChunkNotFoundError` is caught here and turned
    into a JSON error object rather than allowed to propagate. A tool's job
    is to hand the model clean feedback it can react to (e.g. try a
    different chunk_id, or re-search); an uncaught exception would instead
    crash the whole agent loop over a single bad tool argument.
    """

    @tool(description=READ_EVIDENCE_DESCRIPTION)
    def read_evidence(chunk_id: str) -> str:
        """Model-facing description lives in `docket.prompts.agent.READ_EVIDENCE_DESCRIPTION`."""
        try:
            resolved = resolver.resolve(chunk_id)
        except ChunkNotFoundError:
            return json.dumps({"error": f"chunk not found: {chunk_id}"})
        return json.dumps(
            {
                "chunk_id": resolved.chunk_id,
                "text": resolved.text,
                "citation_label": resolved.citation_label,
                "source_display_name": resolved.source_display_name,
                "heading": resolved.heading,
                "location": resolved.location,
                "formula_regions": resolved.formula_regions,
                "formula_region_scope": "document",
            }
        )

    return read_evidence


def _validation_error_json(exc: Exception) -> str:
    return json.dumps({"error": f"invalid tool arguments: {exc}"})


def make_read_range_tool(*, reader: WorkbookReader) -> BaseTool:
    """Build a `read_range` tool bound to a `WorkbookReader`. Returns the
    reader's JSON (see `docket.infra.evidence.workbook_reader`), or
    ``{"error": ...}`` for any refusal (ineligible/unknown chunk, bad range,
    missing sheet, ...) so a bad argument never crashes the agent loop."""

    @tool(description=READ_RANGE_DESCRIPTION)
    def read_range(chunk_id: str, range: str, sheet: str | None = None) -> str:  # noqa: A002
        """Model-facing description lives in `docket.prompts.agent.READ_RANGE_DESCRIPTION`."""
        try:
            return json.dumps(
                reader.read(chunk_id, range, sheet).to_json(), default=str, separators=(",", ":")
            )
        except WorkbookReadError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 -- a tool must hand back an error, not crash the loop
            return json.dumps({"error": f"read_range failed: {exc}"})

    read_range.handle_validation_error = _validation_error_json
    return read_range


class CalculateRef(BaseModel):
    chunk_id: str = Field(description="A chunk_id returned by search_knowledge; it selects the workbook.")
    sheet: str | None = Field(default=None, description="Sheet name; defaults to the chunk's own sheet.")
    cell: str | None = Field(default=None, description="A single cell, e.g. 'B7'. Give cell OR range.")
    range: str | None = Field(default=None, description="A range, e.g. 'B2:B7'. Give cell OR range.")


def make_calculate_tool(*, reader: WorkbookReader) -> BaseTool:
    """Build a `calculate` tool (see `docket.services.agent.calculator`): the
    tool reads its own inputs through `reader`, the model only names cells."""

    @tool(description=CALCULATE_DESCRIPTION)
    def calculate(operation: str, refs: list[CalculateRef], round_to: int | None = 2) -> str:
        """Model-facing description lives in `docket.prompts.agent.CALCULATE_DESCRIPTION`."""
        try:
            return json.dumps(safe_calculate(reader, operation, refs, round_to), default=str)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"calculate failed: {exc}"})

    calculate.handle_validation_error = _validation_error_json
    return calculate
