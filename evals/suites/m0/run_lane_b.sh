#!/usr/bin/env bash
# M0 phase 2b, Lane B: GPU-accelerated evaluation (flags off vs KAG-on; two judges) on the local AMD GPU.
#
#   evals/suites/m0/run_lane_b.sh [--out DIR] [--only STEP[,STEP...]] [--allow-cpu-fallback] [--skip-preflight]
#
# Idempotent and resumable: every step writes `<output>.done` when finished and is skipped on the next call.
# Runs under `systemd-run --user --scope -p MemoryMax=12G`, with PYTHONPATH=<this checkout>/backend/src and the
# MAIN checkout venv python (never `python -I`), DOCKET_PARSE_WORKERS=1, and SEQUENTIAL GPU use: models are
# unloaded (`ollama stop`) between answering, judging with gemma3:12b and judging with qwen3:14b so the 16 GB
# VRAM budget is never shared by two big models. Scratch data lives under ~/docket-scale/m0/ (copies of the
# existing clean-library data dir); ~/.local/share/docket is never touched.
# Environment: DOCKET_LANE_B_OUT (results dir), DOCKET_LANE_B_PYTHON, DOCKET_LANE_B_SCRATCH.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../../.." && pwd)"

if [ -z "${DOCKET_LANE_B_INNER:-}" ]; then
  export DOCKET_LANE_B_INNER=1
  if command -v systemd-run >/dev/null 2>&1; then
    exec systemd-run --user --scope -q -p MemoryMax=12G "$0" "$@"
  fi
  echo "warning: systemd-run not found; running without the 12G cap" >&2
fi

# Interpreter: $DOCKET_LANE_B_PYTHON, else this checkout's backend/.venv, else python3 on PATH. A linked git
# worktree has no .venv of its own: point DOCKET_LANE_B_PYTHON at the venv you installed Docket into.
py="${DOCKET_LANE_B_PYTHON:-}"
if [ -z "$py" ]; then
  if [ -x "$repo/backend/.venv/bin/python" ]; then py="$repo/backend/.venv/bin/python"; else py="$(command -v python3)"; fi
fi
export PYTHONPATH="$repo/backend/src${PYTHONPATH:+:$PYTHONPATH}"
export DOCKET_PARSE_WORKERS=1
export DOCKET_GPU_CARD="${DOCKET_GPU_CARD:-card0}"   # card0 = the discrete RX 9070 XT (card1 is the CPU iGPU)
unset DOCKET_KAG_SCOPING DOCKET_DOC_ROUTING DOCKET_FIELD_LOOKUP DOCKET_FAMILY_LATEST DOCKET_DATA_DIR
exec "$py" "$here/lane_b.py" "$@"
