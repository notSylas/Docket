"""Real sidecar entrypoint: reads JSON request lines from stdin, dispatches
each to the backend via `server.dispatch`, writes one JSON response line
per request to stdout. Speaks the exact same wire protocol as
`_echo_test.py` (see that file's docstring for the shape) but backed by the
real `attest` package instead of a `ping`/`echo` stub.

Import wiring: run as a plain script (`python main.py`) from this
directory, not as an installed package -- `sidecar/` doesn't need to be
pip-installed for this checkpoint (or for PyInstaller later: both
`main.py` and `server.py` just need to land in the same bundle directory).
A `sys.path` insert of this file's own directory + a plain `import server`
is the simplest thing that works identically whether this is run directly
during dev (`python main.py` from `desktop/sidecar/`) or later as a frozen
PyInstaller entrypoint sitting next to `server.py` -- a relative import
(`from . import server`) would require `sidecar` to be recognized as a
package (an `__init__.py`, and *not* being invoked as `__main__` via a bare
script path) which adds friction for no benefit here.

stdout hygiene: importing `attest.*` and building `AppContext` transitively
pulls in docling/lancedb/langchain, and something in that chain has, in
practice, been known to print stray diagnostic output straight to real
stdout. Since stdout is the wire protocol's only channel back to the Rust
bridge (one JSON line per response, nothing else), any such stray print
would corrupt it -- the bridge has no way to distinguish a diagnostic
print from a response line. Two places need the guard, not just one:

1. Import + `build_context()` at startup -- `AppContext.__init__` itself is
   cheap (just `Settings()` + an Alembic check + opening the SQLite
   engine), but everything heavier (`.parser`, `.gateway`, ...) is a
   `functools.cached_property` built lazily on first *access*, not at
   `AppContext` construction. So startup alone isn't guaranteed to trigger
   the expensive imports/constructions.
2. Every `dispatch()` call, for exactly that reason -- the first
   `sources.ingest` request is what actually first touches `context.parser`
   (constructing `DoclingParser()`, which loads layout/OCR/table models),
   and the first `query.ask` first touches `context.gateway` and possibly
   builds the investigation agent. Either could happen well after startup,
   on an arbitrary request in the middle of a long-running session -- so
   the redirect has to wrap each dispatch call, not just the one at
   startup.

`contextlib.redirect_stdout(sys.stderr)` routes anything printed in either
window to stderr instead, where it's still visible for dev debugging (e.g.
`python main.py 2>debug.log`) without touching the JSON-line stdout stream.
The actual `print(json.dumps(response), ...)` call below always happens
OUTSIDE any redirect, straight to the real stdout.
"""

import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> None:
    with contextlib.redirect_stdout(sys.stderr):
        import server as sidecar_server
        from attest.cli.context import build_context

        context = build_context()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue  # malformed input line, nothing sane to respond to

        with contextlib.redirect_stdout(sys.stderr):
            response = sidecar_server.dispatch(context, request)

        print(json.dumps(response), flush=True)  # flush=True is load-bearing


if __name__ == "__main__":
    main()
