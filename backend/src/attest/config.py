from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATTEST_")

    data_dir: Path = Path.home() / ".local" / "share" / "attest"

    gen_model: str = "qwen3:14b"
    embed_model: str = "qwen3-embedding:0.6b"

    chunk_size_words: int = 200
    chunk_overlap_words: int = 40

    max_agent_iterations: int = 3
    max_agent_tool_calls: int = 8

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "attest.sqlite3"

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
