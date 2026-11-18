"""Tier 1 spike: hybrid retrieval (FTS5 + LanceDB) with Reciprocal Rank Fusion,
then citation-grounded generation via Ollama.

Usage: python query.py "your question here"
"""
import sqlite3
import sys
from pathlib import Path

import lancedb
import ollama

SQLITE_PATH = Path(__file__).resolve().parent / "spike.sqlite3"
LANCEDB_PATH = Path(__file__).resolve().parent / ".lancedb"
EMBED_MODEL = "qwen3-embedding:0.6b"
GEN_MODEL = "qwen3:14b"
TOP_K = 8
RRF_K = 60

SYSTEM_PROMPT = """You are a careful assistant that answers ONLY from the provided \
context chunks. Every material factual claim in your answer must be followed by a \
citation in the form [source_file#chunk_id]. If the context does not contain enough \
information to answer, respond exactly with: "I don't know based on the available \
evidence." Do not use outside knowledge."""


def fts_search(conn: sqlite3.Connection, question: str, k: int) -> list[str]:
    # FTS5 MATCH needs simple tokenized query; strip punctuation-heavy chars
    terms = "".join(c if c.isalnum() or c.isspace() else " " for c in question)
    cur = conn.execute(
        "SELECT chunk_id FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
        (terms, k),
    )
    return [row[0] for row in cur.fetchall()]


def vector_search(table, question: str, k: int) -> list[str]:
    query_vec = ollama.embed(model=EMBED_MODEL, input=question)["embeddings"][0]
    results = table.search(query_vec).limit(k).to_list()
    return [r["chunk_id"] for r in results]


def reciprocal_rank_fusion(ranked_lists: list[list[str]], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return [cid for cid, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python query.py \"question\"")
        sys.exit(1)
    question = sys.argv[1]

    conn = sqlite3.connect(SQLITE_PATH)
    db = lancedb.connect(str(LANCEDB_PATH))
    table = db.open_table("chunks")

    fts_ranked = fts_search(conn, question, TOP_K)
    vec_ranked = vector_search(table, question, TOP_K)
    fused = reciprocal_rank_fusion([fts_ranked, vec_ranked])[:TOP_K]

    if not fused:
        print("No chunks retrieved. (Run ingest.py first.)")
        return

    rows = conn.execute(
        f"SELECT chunk_id, source_file, text FROM chunks WHERE chunk_id IN "
        f"({','.join('?' for _ in fused)})",
        fused,
    ).fetchall()
    row_by_id = {r[0]: r for r in rows}

    context_blocks = []
    for cid in fused:
        if cid in row_by_id:
            _, source_file, text = row_by_id[cid]
            context_blocks.append(f"[{source_file}#{cid}]\n{text}")
    context = "\n\n".join(context_blocks)

    print("--- Retrieved chunks (fused order) ---")
    for cid in fused:
        if cid in row_by_id:
            print(f"  {cid}  ({row_by_id[cid][1]})")
    print()

    prompt = f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
    response = ollama.generate(model=GEN_MODEL, system=SYSTEM_PROMPT, prompt=prompt)

    print("--- Answer ---")
    print(response["response"])

    conn.close()


if __name__ == "__main__":
    main()
