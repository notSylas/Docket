"""Tier 1 spike: parse Docs/*.docx with Docling, chunk, and index into
SQLite FTS5 (lexical) + LanceDB (vector), keyed by a shared chunk_id.

Usage: python ingest.py
"""
import re
import sqlite3
from pathlib import Path

import lancedb
import ollama
import pyarrow as pa
from docling.document_converter import DocumentConverter

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "Docs"
SQLITE_PATH = Path(__file__).resolve().parent / "spike.sqlite3"
LANCEDB_PATH = Path(__file__).resolve().parent / ".lancedb"
EMBED_MODEL = "qwen3-embedding:0.6b"
CHUNK_WORDS = 200


def chunk_text(text: str, words_per_chunk: int = CHUNK_WORDS) -> list[str]:
    words = text.split()
    return [
        " ".join(words[i : i + words_per_chunk])
        for i in range(0, len(words), words_per_chunk)
        if words[i : i + words_per_chunk]
    ]


def setup_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute(
        """
        CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            source_file TEXT,
            section TEXT,
            chunk_index INTEGER,
            text TEXT
        )
        """
    )
    conn.execute("DROP TABLE IF EXISTS fts_chunks")
    conn.execute(
        "CREATE VIRTUAL TABLE fts_chunks USING fts5(chunk_id UNINDEXED, text, content='')"
    )
    conn.commit()


def embed(text: str) -> list[float]:
    resp = ollama.embed(model=EMBED_MODEL, input=text)
    return resp["embeddings"][0]


def main() -> None:
    converter = DocumentConverter()
    conn = sqlite3.connect(SQLITE_PATH)
    setup_sqlite(conn)

    db = lancedb.connect(str(LANCEDB_PATH))
    vector_rows = []

    docx_files = sorted(DOCS_DIR.glob("*.docx"))
    print(f"Found {len(docx_files)} docs to ingest.")

    for path in docx_files:
        print(f"Parsing {path.name} ...")
        result = converter.convert(str(path))
        doc = result.document

        markdown = doc.export_to_markdown()
        # naive section split on markdown headings, fallback to whole doc
        sections = re.split(r"\n(?=#{1,3} )", markdown) or [markdown]

        chunk_idx = 0
        for section in sections:
            section = section.strip()
            if not section:
                continue
            heading_match = re.match(r"^(#{1,3})\s+(.*)", section)
            heading = heading_match.group(2) if heading_match else "(no heading)"

            for piece in chunk_text(section):
                chunk_id = f"{path.stem}::{chunk_idx}"
                conn.execute(
                    "INSERT INTO chunks (chunk_id, source_file, section, chunk_index, text) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (chunk_id, path.name, heading, chunk_idx, piece),
                )
                conn.execute(
                    "INSERT INTO fts_chunks (chunk_id, text) VALUES (?, ?)",
                    (chunk_id, piece),
                )
                vector_rows.append(
                    {
                        "chunk_id": chunk_id,
                        "source_file": path.name,
                        "section": heading,
                        "text": piece,
                        "vector": embed(piece),
                    }
                )
                chunk_idx += 1

        conn.commit()
        print(f"  -> {chunk_idx} chunks")

    print(f"Total chunks: {len(vector_rows)}")
    if vector_rows:
        db.create_table("chunks", data=vector_rows, mode="overwrite")

    conn.close()
    print("Ingestion complete.")
    print(f"SQLite: {SQLITE_PATH}")
    print(f"LanceDB: {LANCEDB_PATH}")


if __name__ == "__main__":
    main()
