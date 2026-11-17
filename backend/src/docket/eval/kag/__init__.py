"""Lane A (CPU-only) KAG evaluation: gold schemas, loaders, deterministic metrics, review sheets.

Everything here reads a scratch SQLite library (never the user's data dir) and calls no model.
Labels carry provenance: `silver_unreviewed` (mechanically derived) or `human_reviewed`.
"""
