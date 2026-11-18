#!/usr/bin/env bash
# Local equivalent of .github/workflows/ci.yml, capped at 6 GB via systemd-run.
#   ./ci-local.sh          CPU lane: python unit suite (-m "not gpu and not integration") + cargo fmt --check, clippy -D warnings, test --workspace (all --locked)
#   ./ci-local.sh --gpu    GPU lane: tests marked `gpu` (needs Ollama + ROCm; opt-in via DOCKET_GPU_LANE=1)
#                          The full GPU evaluation (answers + judges) is evals/suites/m0/run_lane_b.sh.
#   ./ci-local.sh --python-only | --rust-only
#   ./ci-local.sh --kag-eval   OPTIONAL, non-GPU: print the Lane A KAG metrics table for a scratch library
#                              (DOCKET_KAG_EVAL_DB=<scratch docket.sqlite3>; build one with `docket eval kag library`).
#                              Prints a skip notice when unset; never touches ~/.local/share/docket. Silver labels only.
# Python: $DOCKET_CI_PYTHON, else backend/.venv, else python3. PYTHONPATH is pinned to THIS checkout's
# backend/src (never `python -I`). Scratch data lives under a temp dir, never ~/.local/share/docket.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${DOCKET_CI_INNER:-}" ]; then
  export DOCKET_CI_INNER=1
  if command -v systemd-run >/dev/null 2>&1; then
    exec systemd-run --user --scope -q -p MemoryMax=6G "$0" "$@"
  fi
  echo "warning: systemd-run not found; running without the 6G cap" >&2
fi

mode="all"
case "${1:-}" in
  --gpu) mode="gpu" ;;
  --python-only) mode="py" ;;
  --rust-only) mode="rust" ;;
  --kag-eval) mode="kag" ;;
  "") ;;
  *) echo "usage: $0 [--gpu|--python-only|--rust-only|--kag-eval]" >&2; exit 2 ;;
esac

py="${DOCKET_CI_PYTHON:-}"
if [ -z "$py" ]; then
  if [ -x "$here/backend/.venv/bin/python" ]; then py="$here/backend/.venv/bin/python"; else py="python3"; fi
fi
scratch="$(mktemp -d "${TMPDIR:-/tmp}/docket-ci.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
export DOCKET_DATA_DIR="$scratch/data"
export PYTHONPATH="$here/backend/src${PYTHONPATH:+:$PYTHONPATH}"

run_python() {
  (cd "$here/backend" && "$py" -m pytest tests/unit -m "not gpu and not integration" -q)
}
run_rust() {
  (cd "$here/app" && cargo fmt --all --check \
    && cargo clippy --workspace --all-targets --locked -- -D warnings \
    && cargo test --workspace --locked)
}
run_kag_eval() {
  if [ -z "${DOCKET_KAG_EVAL_DB:-}" ]; then
    echo "kag-eval: skipped (set DOCKET_KAG_EVAL_DB to a scratch library docket.sqlite3)"
    return 0
  fi
  (cd "$here/backend" && "$py" -m docket.interfaces.cli.main eval kag run --db "$DOCKET_KAG_EVAL_DB")
}
run_gpu() {
  (cd "$here/backend" && DOCKET_GPU_LANE=1 "$py" -m pytest tests -m gpu -q)
}

case "$mode" in
  all) run_python; run_rust ;;
  py) run_python ;;
  rust) run_rust ;;
  gpu) run_gpu ;;
  kag) run_kag_eval ;;
esac
