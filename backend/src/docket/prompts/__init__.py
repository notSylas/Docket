"""Cross-cutting static prompt text, centralized out of the modules that use it.

Before this package existed, model-facing strings (system prompts, tool
descriptions, corrective/denial messages) were defined inline in whichever
module happened to need them first (`query/prompts.py`, `eval/judge.py`,
`eval/draft.py`, `ingestion/pipeline.py`, `agent/graph.py`, `agent/tools.py`,
`agent/policy_gateway.py`), with no shared import between them even where
the wording was identical or nearly so. This package is the shared home:

- `shared` -- text more than one prompt reuses (`ABSTENTION_PHRASE`,
  `JSON_ONLY_REPLY`, the `content_not_instructions` helper).
- `query` -- the fast-path/agent-path system prompts (moved byte-for-byte
  from `query/prompts.py`, which is now a thin re-export shim there).
- `judge` / `draft` -- the eval harness's judge and drafting prompts.
- `vision` -- the ingestion pipeline's page-description and
  formula-transcription vision prompts.
- `agent` -- the investigation agent's tool descriptions and its
  corrective/denial message builders.

Logic that is merely prompt-*adjacent* (citation-tag validation, context-
block formatting) is deliberately NOT here -- see `query/citations.py`.
"""
