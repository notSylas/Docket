# ADR 0001: Repository layout

Status: accepted, implemented in the repository restructuring.

## Context

The repository had grown organically: a Rust workspace (`app/`), the Python engine (`backend/`), a paused Tauri prototype (`desktop/`), the original validation spike (`spike/`), numbered research (`Upgrade/`), product documents (`Docs/`), and evaluation material (`backend/eval-public`, `backend/eval-private`) nested inside `backend/`. `Docs/` and a separate untracked `docs/` differed only by case, which collides on case-insensitive filesystems, and the evaluation folders were not importable packages but were reached through paths computed from `__file__`.

## Decision

- Keep the working architecture and its paths: Rust workspace `app/`, Python package `backend/src/docket`. Installing with `pipx install ./backend` and the CI working directories do not change.
- Archive, do not delete: `desktop/` becomes `archive/desktop-tauri/`, `spike/` becomes `archive/spike/`. Both moved with `git mv`, so history follows.
- One lowercase `docs/` tree (`product`, `architecture`, `api`, `ui`, `research`, `adr`). Untracked design and UI-review documents from other working checkouts were copied in at paths that cannot collide with their original locations, and verified byte-for-byte.
- Evaluation suites and fixtures live under `evals/` (`evals/suites`, `evals/private-kit`; see its README). The `docket eval` defaults locate them from a checkout, `$DOCKET_EVAL_SUITES` or the working directory, because an installed wheel does not ship them. The original gold set carries its own corpus fixture instead of depending on where product documents live.
- Private data, models, databases and vector indexes stay outside Git.

## Consequences

- Old paths in point-in-time documents are mapped in [`../README.md`](../README.md).
- Other checkouts that hold untracked copies of the UI documents keep working; when they next merge this layout nothing is overwritten, and their untracked copies become redundant and can be deleted by their owner.
- Moving `app/` or `backend/` was deliberately not done: it would break the install command, CI and in-flight UI work for no functional gain.
