"""Factory functions building the real `search_knowledge`/`read_evidence`
LangChain tools that the Agent Policy Gateway's allow-list is built from.

The validation spike (`spike/test_agent_policy_gateway.py`) used two
module-level `@tool`-decorated functions that returned hardcoded, canned
results -- fine for isolating the gateway's *enforcement* logic (the thing
that spike was actually validating), but useless for a real investigation:
nothing in them touched the real retrieval/resolution stack.

Here, `search_knowledge`/`read_evidence` need to call real code
(`attest.retrieval.hybrid.hybrid_search`, `attest.retrieval.resolver.
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
from typing import Any

from langchain_core.tools import BaseTool, tool

from attest.inference.gateway import InferenceGateway
from attest.retrieval.hybrid import hybrid_search
from attest.retrieval.resolver import ChunkNotFoundError, EvidenceResolver


def make_search_knowledge_tool(
    *, engine: Any, table: Any, gateway: InferenceGateway, top_k: int = 8
) -> BaseTool:
    """Build a `search_knowledge` tool bound to a specific engine/table/gateway.

    Replaces the spike's hardcoded fake result with a real
    `hybrid_search` call. Returns JSON: ``{"results": [{"chunk_id", "score"}, ...]}``
    -- deliberately just chunk_id + score, not the chunk text itself, so the
    model has to make a separate, deliberate `read_evidence` call (and
    therefore a separate, gateway-checked, budget-counted tool call) to see
    real content, mirroring the spike's two-tool split.
    """

    @tool
    def search_knowledge(query: str) -> str:
        """Search the local evidence index for chunks relevant to a query.
        Returns a JSON list of {chunk_id, score} results, best match first.
        Call read_evidence with a chunk_id to see its full text."""
        ranked = hybrid_search(engine=engine, table=table, gateway=gateway, query=query, top_k=top_k)
        return json.dumps({"results": [{"chunk_id": rc.chunk_id, "score": rc.score} for rc in ranked]})

    return search_knowledge


def make_read_evidence_tool(*, resolver: EvidenceResolver) -> BaseTool:
    """Build a `read_evidence` tool bound to a specific `EvidenceResolver`.

    Replaces the spike's hardcoded fake string with a real
    `resolver.resolve()` call. Returns JSON: ``{"text", "citation_label",
    "source_display_name", "heading"}``.

    If `chunk_id` doesn't exist (the model invented or mistyped one --
    plausible, since chunk_ids are opaque hashes it only ever saw as
    search_knowledge output), `ChunkNotFoundError` is caught here and turned
    into a JSON error object rather than allowed to propagate. A tool's job
    is to hand the model clean feedback it can react to (e.g. try a
    different chunk_id, or re-search); an uncaught exception would instead
    crash the whole agent loop over a single bad tool argument.
    """

    @tool
    def read_evidence(chunk_id: str) -> str:
        """Read the full text of a specific evidence chunk by its id (as
        returned by search_knowledge). Returns JSON with the chunk's text,
        citation_label, source_display_name, and heading."""
        try:
            resolved = resolver.resolve(chunk_id)
        except ChunkNotFoundError:
            return json.dumps({"error": f"chunk not found: {chunk_id}"})
        return json.dumps(
            {
                "text": resolved.text,
                "citation_label": resolved.citation_label,
                "source_display_name": resolved.source_display_name,
                "heading": resolved.heading,
            }
        )

    return read_evidence
