"""Read-only operational checks. The only HTTP operation is local GET /api/tags."""
from contextlib import closing
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import urllib.parse
import urllib.request

from alembic.config import Config
from alembic.script import ScriptDirectory
from docket.core.writer_lock import LOCK_FILENAME
from docket.infra.index.manifest import SCHEMA_VERSION, read_manifest
from docket.infra.inference.health import _installed_names, _normalize


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # OK, WARN or BLOCK
    detail: str
    fix: str = ""


def local_tags_url(host: str) -> str:
    parsed = urllib.parse.urlsplit(host if "://" in host else "http://" + host)
    name = parsed.hostname or ""
    try:
        local = name.lower() == "localhost" or ipaddress.ip_address(name).is_loopback
    except ValueError:
        local = name.lower() == "localhost"
    if (not local or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Doctor only contacts a local Ollama server over HTTP.")
    port = parsed.port or 11434
    authority = f"[{name}]:{port}" if ":" in name else f"{name}:{port}"
    return f"http://{authority}/api/tags"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Ollama redirected the model listing. Check OLLAMA_HOST.")


def fetch_tags(url: str):
    # Ignore HTTP proxy environment settings and refuse redirects off localhost.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(urllib.request.Request(url, method="GET"), timeout=1.5) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("Model listing is too large.")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("Invalid model listing.")
    return data


def migration_head() -> str:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "core/db/migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def lock_check(root: Path) -> Check:
    path = root / LOCK_FILENAME
    if not path.exists():
        return Check("Writer lock", "OK", "No library writer is active.")
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                owner = json.loads(os.read(fd, 4096))
                pid = int(owner["pid"])
                started = str(owner.get("started_at", "unknown")).splitlines()[0][:80]
                operation = str(owner.get("operation", "updating the library")).splitlines()[0][:120]
                detail = f"Process {pid}, started {started}, is {operation}."
            except (ValueError, KeyError, IndexError, TypeError):
                detail = "Another Docket process is updating the library."
            return Check("Writer lock", "WARN", detail, "Wait for that update to finish; reading documents is still available.")
        fcntl.flock(fd, fcntl.LOCK_UN)
        return Check("Writer lock", "OK", "No library writer is active; stale owner text is harmless.")
    except OSError:
        return Check("Writer lock", "BLOCK", "The library lock cannot be inspected.", "Check data-folder ownership and lock-file permissions.")
    finally:
        if fd is not None:
            os.close(fd)


def checks(settings, *, tags_fetcher=None, environment=None, terminal=None) -> list[Check]:
    """Never constructs AppContext, a database writer, or an inference gateway."""
    env = os.environ if environment is None else environment
    root = Path(settings.data_dir).expanduser().absolute()
    nearest = root
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    out = []
    writable = nearest.is_dir() and os.access(nearest, os.W_OK | os.X_OK)
    out.append(Check("Data folder", "OK" if writable else "BLOCK",
                     "Permission check allows writing (no probe file created)." if writable else "The data folder is not writable.",
                     "Choose a writable DOCKET_DATA_DIR or fix its ownership." if not writable else ""))
    try:
        free = shutil.disk_usage(nearest).free
        status = "BLOCK" if free < 256 * 1024**2 else "WARN" if free < 2 * 1024**3 else "OK"
        out.append(Check("Disk space", status, f"{free / 1024**3:.2f} GiB available.",
                         "Free disk space before indexing or backing up." if status != "OK" else ""))
    except OSError:
        out.append(Check("Disk space", "BLOCK", "Free space could not be checked.", "Check the data drive is mounted and accessible."))
    try:
        url = local_tags_url(env.get("OLLAMA_HOST") or "http://127.0.0.1:11434")
        installed = _installed_names((tags_fetcher or fetch_tags)(url))
        out.append(Check("Ollama", "OK", "Local model listing is reachable; no model was loaded."))
        missing = [name for name in dict.fromkeys((settings.gen_model, settings.embed_model)) if _normalize(name) not in installed]
        out.append(Check("Required models", "BLOCK" if missing else "OK",
                         "Missing: " + ", ".join(missing) if missing else "Generation and embedding models are installed.",
                         "Run: " + "; ".join(f"ollama pull {name}" for name in missing) if missing else ""))
    except Exception:
        out.append(Check("Ollama", "BLOCK", "The local model listing is unavailable or OLLAMA_HOST is not local.",
                         "Set OLLAMA_HOST=http://127.0.0.1:11434 and start ollama serve."))
        out.append(Check("Required models", "WARN", "Not checked while Ollama is unavailable.", "Restore local Ollama access and rerun docket doctor."))
    try:
        manifest = read_manifest(settings.index_manifest_path)
        if manifest is None:
            legacy = settings.lancedb_path.exists() and any(settings.lancedb_path.iterdir())
            out.append(Check("Index manifest", "WARN" if legacy else "OK",
                             "Legacy index has no manifest; compatibility is unknown." if legacy else "No index yet; its manifest will be created on first indexing.",
                             "Run docket reindex to record the embedding space." if legacy else ""))
        else:
            compatible = (manifest.schema_version == SCHEMA_VERSION and manifest.embed_model == settings.embed_model
                          and isinstance(manifest.embed_dimension, int) and manifest.embed_dimension > 0
                          and manifest.embed_instruction is None)
            out.append(Check("Index manifest", "OK" if compatible else "BLOCK",
                             "Manifest matches configured embedding model; live model dimensions were not probed." if compatible else "Manifest is incompatible with the configured embedding space.",
                             "Back up the library, then run docket reindex with the intended embedding model." if not compatible else ""))
    except Exception:
        # Do not print JSON contents or paths from a damaged manifest.
        out.append(Check("Index manifest", "BLOCK", "The index manifest cannot be read.", "Back up the library, repair its manifest, then run docket reindex."))
    db_path = settings.sqlite_path
    try:
        if not db_path.exists():
            out.append(Check("Migration revision", "OK", "No database yet; first launch initializes it."))
        else:
            with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
                revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            current = revision == migration_head()
            out.append(Check("Migration revision", "OK" if current else "BLOCK", f"Database revision: {revision}.",
                             "Back up the library and restart Docket to apply pending migrations." if not current else ""))
    except (sqlite3.Error, TypeError, IndexError):
        out.append(Check("Migration revision", "BLOCK", "The database revision could not be read.", "Check database permissions or restore a verified backup."))
    tty = sys.stdout.isatty() if terminal is None else terminal
    term = env.get("TERM", "")
    size = shutil.get_terminal_size(fallback=(80, 24))
    good_terminal = tty and term not in ("", "dumb") and size.columns >= 60 and size.lines >= 16
    out.append(Check("Terminal", "OK" if good_terminal else "WARN",
                     "Interactive terminal is available." if good_terminal else "Full-screen terminal capabilities are limited or unavailable.",
                     "Use a larger terminal, or docket query QUESTION for one-shot output." if not good_terminal else ""))
    display = bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))
    out.append(Check("Display", "OK" if display else "WARN", "Display environment is present (not actively probed)." if display else "No graphical display is advertised.",
                     "Enter folder paths in the terminal; the graphical folder picker is optional." if not display else ""))
    out.append(lock_check(root))
    return out
