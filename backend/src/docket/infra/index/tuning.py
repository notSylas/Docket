"""Runtime knobs for large-library retrieval (eligibility mode, ANN index,
compaction). Read from the environment at call time so tests and operators can
flip them without re-importing settings.

- DOCKET_ELIGIBILITY_MODE=scalable|legacy   (default scalable)
- DOCKET_ANN_MIN_ROWS=<int>                 (default 100000; below it: flat scan)
- DOCKET_ANN_NPROBES=<int>                  (default 50)
- DOCKET_ANN_REFINE_FACTOR=<int>            (default 20)
- DOCKET_COMPACT_AFTER_INGEST=true|false    (default true)
- DOCKET_COMPACT_KEEP_HOURS=<int>           (default 1: old table versions younger
  than this are kept, so an in-flight reader or a reindex undo still works)
- DOCKET_COMPACT_EVERY_FILES=<int>          (default 500; 0 = only at the end of a
  run) compact/optimize the vector table every N ingested files so fragment and
  version counts stay bounded during long runs
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def eligibility_mode() -> str:
    mode = os.environ.get("DOCKET_ELIGIBILITY_MODE", "scalable").strip().lower()
    return mode if mode in ("legacy", "scalable") else "scalable"


def ann_min_rows() -> int:
    return _int("DOCKET_ANN_MIN_ROWS", 100_000)


def ann_nprobes() -> int:
    return max(1, _int("DOCKET_ANN_NPROBES", 50))


def ann_refine_factor() -> int:
    return max(1, _int("DOCKET_ANN_REFINE_FACTOR", 20))


def compact_after_ingest() -> bool:
    return os.environ.get("DOCKET_COMPACT_AFTER_INGEST", "true").strip().lower() not in (
        "0", "false", "no", "off",
    )


def compact_keep_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("DOCKET_COMPACT_KEEP_HOURS", 1)))
    except ValueError:
        return 1.0


#: Mid-run compactions drop table versions older than this many hours
#: (capped by `compact_keep_hours`): a long ingest would otherwise keep every
#: per-file version for the full retention window.
MID_RUN_KEEP_HOURS = 0.1


def compact_every_files() -> int:
    return max(0, _int("DOCKET_COMPACT_EVERY_FILES", 500))
