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

    # Texts per embedding request (`docket.infra.inference.gateway.embed_texts`).
    embed_batch_size: int = 32

    chunk_size_words: int = 200
    chunk_overlap_words: int = 40

    # Single source of truth for the fused-retrieval result count. Read as
    # each call site's own `top_k` parameter default (`retrieval.hybrid.
    # hybrid_search`, `query.service.QueryService`, `agent.graph.
    # build_investigation_agent`, `agent.tools.make_search_knowledge_tool`,
    # `eval.runner.EvalRunner`/`run_eval`) so a future tuning change can't
    # silently apply to only some of them.
    default_top_k: int = 8

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
    max_agent_iterations: int = 4
    max_agent_tool_calls: int = 8

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
