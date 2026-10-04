"""Model-facing text for the bounded LangGraph investigation agent.

Covers three previously-inline sources:
- `agent/graph.py`'s `force_tool_use` node built two corrective
  `HumanMessage` strings inline; promoted here to
  `missing_required_tool_message`/`missing_any_tool_message`.
- `agent/tools.py`'s `search_knowledge`/`read_evidence` tool descriptions
  lived as the tool functions' own docstrings; promoted here to
  `SEARCH_KNOWLEDGE_DESCRIPTION`/`READ_EVIDENCE_DESCRIPTION`, passed via
  `@tool(description=...)` (confirmed: `langchain_core.tools.tool`'s
  `description` kwarg takes precedence over the function's docstring).
- `agent/policy_gateway.py`'s `ToolMessage` denial strings; promoted here
  to `policy_denied_tool`/`policy_denied_budget`.
"""

from __future__ import annotations

SEARCH_KNOWLEDGE_DESCRIPTION = (
    "Search the local evidence index for chunks relevant to a query. "
    "Returns a JSON list of {chunk_id, score} results, best match first. "
    "Call read_evidence with a chunk_id to see its full text."
)

READ_EVIDENCE_DESCRIPTION = (
    "Read the full text of a specific evidence chunk by its id (as "
    "returned by search_knowledge). Returns JSON with the chunk's id, "
    "text, citation_label, source_display_name, and heading."
)

READ_RANGE_DESCRIPTION = (
    "Read exact cells from the spreadsheet (.xlsx workbook) that a chunk came "
    "from, straight from the stored workbook -- use it instead of quoting a "
    "search snippet when you need exact values, or all rows/cells of a "
    "total. Arguments: chunk_id (any chunk_id returned by search_knowledge; "
    "it selects the workbook), range (A1 notation, e.g. 'A1:F12' or 'C7'; at "
    "most 200 cells / 60 rows per call), sheet (optional; defaults to the "
    "chunk's own sheet). Returns JSON with the sheet, header labels, rows of "
    "cells (value, header, number_format, formula and formula_text, blank, "
    "cached_value_missing, hidden rows), the units / fiscal-year / scope "
    "lines for the sheet, the source file, a coverage object (truncated "
    "true/false), and chunk_ids / citation_labels to cite. A blank cell is "
    "null with blank true -- it is NOT zero. A formula cell with "
    "cached_value_missing has no known value."
)

CALCULATE_DESCRIPTION = (
    "Do arithmetic on spreadsheet cells exactly. Never compute numbers "
    "yourself; use this for every sum, average, difference, ratio, "
    "percentage change, minimum, maximum or count. Arguments: operation "
    "(sum, average, difference, ratio, pct_change, min, max, count), refs "
    "(a list of {chunk_id, sheet (optional), cell OR range}; the tool reads "
    "the cells itself, you never type the numbers; difference = a - b, ratio "
    "= a / b and pct_change = (b - a) / a * 100 take exactly two single-cell "
    "refs), round_to (decimal places, default 2). Returns JSON with result, "
    "result_text, the exact inputs (value, cell, header, units), the "
    "expression, units, citation_labels and provenance 'derived'. It refuses "
    "inputs with different or ambiguous units, and reports blank, text or "
    "missing-value cells as errors instead of treating them as zero."
)


def missing_required_tool_message(require_tool_call: str | tuple[str, ...]) -> str:
    """Corrective `HumanMessage` text when the model tries to stop before a
    successful call to `require_tool_call` (see
    `agent.graph.build_agent`'s `force_tool_use` node). A tuple means any one
    of those tools satisfies the requirement."""
    if not isinstance(require_tool_call, str):
        names = list(require_tool_call)
        if len(names) == 1:
            require_tool_call = names[0]
        else:
            joined = " or ".join(names)
            return (
                f"You answered without a successful {joined} call. "
                "Every claim in your final answer must be grounded in "
                "evidence you actually retrieved -- call search_knowledge, "
                f"then call {joined} on one of its chunk_ids, and "
                "copy its citation_label (read_range returns citation_labels) "
                "into your answer, before answering."
            )
    return (
        f"You answered without a successful {require_tool_call} call. "
        "Every claim in your final answer must be grounded in "
        f"evidence you actually retrieved -- call search_knowledge, "
        f"then call {require_tool_call} on one of its chunk_ids, and "
        "copy its citation_label into your answer, before answering."
    )


def missing_any_tool_message() -> str:
    """Corrective `HumanMessage` text when the model tries to stop without
    ever calling any tool (see `agent.graph.build_agent`'s
    `force_tool_use` node)."""
    return (
        "You answered without calling any tool. Every claim in your "
        "final answer must be grounded in evidence you actually "
        "retrieved -- investigate using the available tools before "
        "answering."
    )


def policy_denied_tool(name: str) -> str:
    """`ToolMessage` content for a tool call whose name isn't in the
    allow-list (see `agent.policy_gateway.make_policy_gateway`)."""
    return f"POLICY DENIED: '{name}' is not an authorized tool."


def policy_denied_budget() -> str:
    """`ToolMessage` content once `max_tool_calls` has been reached for
    this investigation (see `agent.policy_gateway.make_policy_gateway`)."""
    return "POLICY DENIED: tool-call budget exhausted for this investigation."
