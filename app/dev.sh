#!/usr/bin/env bash
# Run docketd + docket-app against a scratch data dir.
# Usage: app/dev.sh [data_dir]   (default: a fresh dir under ${TMPDIR:-/tmp})
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
data="${1:-$(mktemp -d "${TMPDIR:-/tmp}/docket-dev.XXXXXX")}"
mkdir -p "$data"
export DOCKET_DATA_DIR="$data"
export DOCKET_SOCKET="${DOCKET_SOCKET:-$data/docketd.sock}"
export PYTHONPATH="$repo/backend/src${PYTHONPATH:+:$PYTHONPATH}"
# The venv's `docket` script imports the code on PYTHONPATH (this checkout).
docket_bin="${DOCKET_BIN:-$repo/backend/.venv/bin/docket}"
[ -x "$docket_bin" ] || docket_bin="$(command -v docket)"
export DOCKET_WORKER_CMD="${DOCKET_WORKER_CMD:-$docket_bin worker}"
cargo build --manifest-path "$here/Cargo.toml" -q
export DOCKETD_BIN="$here/target/debug/docketd"
echo "data dir: $data" >&2
exec "$here/target/debug/docket-app"
