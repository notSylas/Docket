from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCKET_")

    data_dir: Path = Path.home() / ".local" / "share" / "docket"

    gen_model: str = "qwen3:14b"
    embed_model: str = "qwen3-embedding:0.6b"

    # Ollama's default context window is 4096 tokens when a model file sets
    # none (confirmed via `ollama ps` on qwen3:14b) -- an 8-chunk retrieval
    # prompt is commonly ~2.6-3.2k tokens on its own, before any conversation
    # history or thinking-mode output, so the default risks silent
    # truncation. num_predict is set generously so thinking mode can't
    # exhaust the answer budget before producing a final answer.
    num_ctx: int = 8192
    num_predict: int = 4096

    chunk_size_words: int = 200
    chunk_overlap_words: int = 40

    max_agent_iterations: int = 3
    max_agent_tool_calls: int = 8

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "docket.sqlite3"

    @property
    def lancedb_path(self) -> Path:
        return self.data_dir / "lancedb"

    @property
    def evidence_store_path(self) -> Path:
        return self.data_dir / "evidence_store"

    def ensure_data_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lancedb_path.mkdir(parents=True, exist_ok=True)
        for sub in ("objects", "manifests", "quarantine", "trash"):
            (self.evidence_store_path / sub).mkdir(parents=True, exist_ok=True)


settings = Settings()
