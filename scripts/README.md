# Scripts

Repository-level helpers (run from the repository root).

| Script | Purpose |
| --- | --- |
| [`ci-local.sh`](ci-local.sh) | Local equivalent of `.github/workflows/ci.yml`, memory-capped with `systemd-run`: Python unit lane, Rust `fmt`, `clippy` and tests (all `--locked`), plus the opt-in GPU lane (`--gpu`) and the KAG metrics report (`--kag-eval`). |
| [`migrate_layout.py`](migrate_layout.py) | Rewrites `from docket.<old>` / `import docket.<old>` lines to a new dotted prefix across `backend/src` and `backend/tests`, used for package regroups. |

`app/dev.sh` (run the desktop app against a scratch data directory) stays next to the Rust workspace it belongs to.
