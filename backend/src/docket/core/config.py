from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCKET_")

    data_dir: Path = Path.home() / ".local" / "share" / "docket"

    gen_model: str = "qwen3:14b"
    embed_model: str = "qwen3-embedding:0.6b"
    # Vision-language model used to describe page images for the visual
    # retrieval index (checkpoint 2) -- pulled locally and confirmed via
    # `ollama list`. Only ever called when `visual_index_enabled` is True.
    vision_model: str = "qwen2.5vl:7b"

    # Off by default: this checkpoint builds the page-image capture +
    # description + embedding infrastructure, but its retrieval value is
    # unmeasured. Nothing should call the VLM or write to the pages LanceDB
    # table until this is explicitly turned on (checkpoint 3 or a later
    # explicit decision).
    visual_index_enabled: bool = False

    # Discovery ignore rules (hidden folders, virtualenvs, site-packages,
    # node_modules, Office lock files, `.docketignore`). Escape hatch:
    # DOCKET_INGEST_IGNORE_ENABLED=false walks everything, as before.
    ingest_ignore_enabled: bool = True

    # Off by default, same posture as `visual_index_enabled`: this checkpoint
    # builds the formula-region crop + VLM transcription + storage
    # infrastructure (`docket.infra.parsing.formula_crop`,
    # `FormulaTranscriber.transcribe`), but produces only
    # unverified transcriptions (`EvidenceVersion.formula_transcriptions_json`)
    # -- never promoted into searchable/citable evidence. Nothing should call
    # the VLM for formula regions until this is explicitly turned on, and
    # turning it on is never itself sufficient to make transcriptions
    # citable (see Docs/accuracy-evaluation.md's "Formula evidence and
    # experiments" section -- that requires a separate, later, manually
    # verified decision).
    formula_transcription_enabled: bool = False

    # Ollama's default context window is 4096 tokens when a model file sets
    # none (confirmed via `ollama ps` on qwen3:14b) -- an 8-chunk retrieval
    # prompt is commonly ~2.6-3.2k tokens on its own, before any conversation
    # history or thinking-mode output, so the default risks silent
    # truncation. num_predict is set generously so thinking mode can't
    # exhaust the answer budget before producing a final answer.
    num_ctx: int = 8192
    num_predict: int = 4096

    # Sampling on the answering path (`QueryService` fast path and citation
    # repair), set explicitly so it does not depend on a model's Modelfile.
    # Both defaults were chosen by measurement on the public gold set (qwen3:14b):
    # - Thinking stays ON. Turning it off made a correct, retrieved answer
    #   (RETRY_WAIT transitions) fail 3/3 and a gate-name answer terse and
    #   imprecise, at 0.3-3 s per answer against 8-18 s with thinking. Set
    #   `DOCKET_ANSWER_THINK=false` to trade that accuracy for latency.
    # - Temperature is 0.6, Qwen3's recommended value for thinking mode.
    #   Greedy decoding (0) is discouraged for it and measured worse here
    #   (strict 29/33 vs 31/33, 2 judged failures vs 0).
    # Thinking tokens count against `num_predict`, so keep it generous. Passed
    # per call; other `generate` callers are unaffected.
    answer_temperature: float = 0.6
    answer_think: bool = True
    # Follow-up query rewrite (Upgrade doc 05 section 5): with history on the
    # fast path, one local-model call turns the latest question into a
    # standalone search query; retrieval then fuses the original and the
    # rewrite. `rewrite_model=None` means the generation model. With thinking
    # on, thinking tokens count against `rewrite_num_predict`.
    # Defaults chosen by measurement (qwen3:14b, follow-up questions + probes):
    # thinking off at temperature 0 gave the same rewrites and the same
    # top-8 recall as temperature 0.6 (0.1-0.6 s per call, longer on a cold
    # model); thinking on cost 3-20 s, returned an EMPTY response at
    # num_predict=128 (thinking tokens consume it), and recalled no more.
    rewrite_enabled: bool = True
    rewrite_model: str | None = None
    rewrite_temperature: float = 0.0
    rewrite_think: bool = False
    rewrite_num_predict: int = 128
    # Query signals (Upgrade doc 05 section 6), each ablatable. Scope: a question
    # that EXPLICITLY names exactly one indexed file searches inside that file
    # only. Ambiguity: a question with no stated period whose evidence spans
    # same-sheet workbooks of different fiscal years is answered per fiscal year
    # and ends by asking which one was meant. Off = previous behaviour exactly.
    query_scope_enabled: bool = True
    query_ambiguity_enabled: bool = True
    # Tokens kept free for the answer when fitting the prompt into `num_ctx`
    # (`QueryService` drops lowest-ranked chunks past `num_ctx - reserve`).
    answer_token_reserve: int = 1024

    # Texts per embedding request (`docket.infra.inference.gateway.embed_texts`).
    embed_batch_size: int = 32

    chunk_size_words: int = 200
    chunk_overlap_words: int = 40

    # Upper bound on tokens per chunk, enforced by every chunker
    # (`docket.infra.parsing.tokens`). Sized for `embed_model`'s context.
    chunk_max_tokens: int = 512
    # Sheet/period context (fiscal-year labels, units from Notes sheets/title
    # rows, month names) stored in each spreadsheet row's `locator_json["context"]`
    # and rendered into the INDEXED text only (doc 05 step 3). Read at chunking
    # time: already-ingested workbooks keep their old locators until their rows
    # are re-chunked (an unchanged file hash makes ingestion skip them), and only
    # then does `docket reindex` pick the context up. Set false to ablate.
    xlsx_period_context_enabled: bool = True
    # Hugging Face tokenizer used to count those tokens; keep it matched to
    # `embed_model` (Ollama `qwen3-embedding:0.6b` is Qwen/Qwen3-Embedding-0.6B).
    # "heuristic" skips the download and uses a character-count estimate.
    embed_tokenizer: str = "Qwen/Qwen3-Embedding-0.6B"

    # Single source of truth for the fused-retrieval result count. Read as
    # each call site's own `top_k` parameter default (`retrieval.hybrid.
    # hybrid_search`, `query.service.QueryService`, `agent.graph.
    # build_investigation_agent`, `agent.tools.make_search_knowledge_tool`,
    # `eval.runner.EvalRunner`/`run_eval`) so a future tuning change can't
    # silently apply to only some of them.
    default_top_k: int = 8

    # Per-leg candidate pool for hybrid retrieval (env DOCKET_RETRIEVAL_POOL_K).
    # `None` (default): each leg requests `top_k`, as always. When set, each
    # leg requests this many and the fused list is still cut to `top_k`
    # (experiment E4: pool 30 / send 12 raises all-spans recall).
    retrieval_pool_k: int | None = None

    # Deterministic compute stage in the fast path (Upgrade doc 05 section 8
    # candidate move; env DOCKET_COMPUTE_STAGE_ENABLED). When on and the
    # question looks arithmetic/aggregate over retrieved spreadsheet chunks, one
    # short JSON-planning model call names cells and an allow-listed operation,
    # the existing `calculate` tool executes it, and the answering call gets the
    # result as a pinned note. Any failure falls back to the normal path. Off
    # (default) = behaviour is unchanged. The planner never supplies numbers.
    compute_stage_enabled: bool = False
    compute_model: str | None = None  # None = the generation model
    compute_num_predict: int = 384
    compute_max_steps: int = 4

    # 4, not 3: the minimum a well-behaved investigation needs is 3 agent
    # turns (search_knowledge -> read_evidence -> cited final answer). 3
    # left no headroom at all for `docket.services.agent.graph.build_agent`'s
    # `force_tool_use` retry (added so the model can't skip straight to an
    # uncited answer -- see that module's docstring for the real-eval bug
    # this closes), which costs one extra turn whenever the model tries to
    # answer before actually calling read_evidence. Confirmed against a real
    # regression: `test_agent_mode_end_to_end_answers_with_real_tool_trace_
    # citations` started failing at 3 (truncated mid-tool-call, empty
    # answer) once that retry was added, and passes again at 4. Only
    # matters when a retry actually happens -- the common case (tools called
    # correctly from turn 1) is unaffected either way.
    #
    # Raised to 8 / 14 for the deterministic spreadsheet tools (doc 05 section
    # 8): the chain search_knowledge -> read_evidence -> read_range (several
    # ranges for a multi-workbook question) -> calculate (possibly several) ->
    # cited answer needs 6+ model turns and ~8 tool calls on its own, and a
    # `force_tool_use` retry (one extra turn) or one rejected tool call must
    # still fit. Each iteration is an `agent` + `gateway` node pair, so 8
    # iterations stay well inside the recursion_limit of 50 set in
    # `QueryService._ask_agent`.
    max_agent_iterations: int = 8
    max_agent_tool_calls: int = 14

    # Upgrade doc 03 section 6: how long a TOMBSTONED source's retention
    # countdown runs before it's eligible to advance to HARD_DELETE_PENDING
    # (`SourceManager.sweep_expired_retentions`). A single global default,
    # not per-source/per-connector-type -- decided as simple to start with;
    # can be split later once a real connector exists to justify the extra
    # config surface.
    tombstone_retention_days: int = 30

    # Upgrade doc 03 section 8: how long a zero-referenced blob sits in
    # `trash/` before `ContentAddressedStore.sweep_trash` permanently
    # deletes it. Deliberately independent of, and shorter than,
    # `tombstone_retention_days` -- this window is a last-resort safety
    # margin against a buggy purge job, not a chance for a deleted source to
    # reappear. Conflating the two would leave a blob double-counted against
    # the storage budget (part 01 section 7) for up to 30 days for no real
    # benefit.
    blob_trash_grace_days: int = 7

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "docket.sqlite3"

    @property
    def lancedb_path(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def index_manifest_path(self) -> Path:
        # Next to (not inside) the LanceDB directory, so LanceDB never sees it.
        return self.data_dir / "index_manifest.json"

    @property
    def evidence_store_path(self) -> Path:
        return self.data_dir / "evidence_store"

    def ensure_data_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lancedb_path.mkdir(parents=True, exist_ok=True)
        for sub in ("objects", "manifests", "quarantine", "trash"):
            (self.evidence_store_path / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
