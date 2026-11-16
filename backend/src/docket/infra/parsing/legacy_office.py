"""Legacy Office formats (.doc, .xls, .ppt, .rtf) via headless LibreOffice.

The legacy file is converted into a throwaway temp directory
(`soffice --headless --convert-to docx|xlsx|pptx`) and the converted copy is
handed to the existing docx/xlsx/pptx parsers. The converted copy is NEVER an
evidence artifact: callers keep citing (hashing, storing, pathing) the
original, and the temp directory disappears when the `with` block ends.

Safety properties:
- conversions run one at a time (a module-level lock; LibreOffice does not
  like parallel headless instances);
- a private `-env:UserInstallation` profile directory, so the user's own
  LibreOffice profile is never read or modified;
- a per-file timeout; on timeout (or any exit) the whole process group is
  killed so no stray `soffice`/`soffice.bin` outlives the conversion;
- LibreOffice missing (or disabled) is reported with `ConverterUnavailable`,
  which the pipeline turns into an explicit "skipped" entry, not a failure.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterator

# legacy extension -> modern format handed to the existing parsers
LEGACY_TARGETS: dict[str, str] = {
    ".doc": "docx",
    ".rtf": "docx",
    ".xls": "xlsx",
    ".ppt": "pptx",
}
LEGACY_EXTENSIONS = frozenset(LEGACY_TARGETS)

DEFAULT_TIMEOUT_S = 120.0

# One conversion at a time, process-wide.
_CONVERT_LOCK = threading.Lock()


class ConversionError(Exception):
    """LibreOffice ran but did not produce a usable converted copy."""


class ConversionTimeout(ConversionError):
    """The conversion exceeded its per-file time limit and was killed."""


class ConverterUnavailable(Exception):
    """No usable LibreOffice (not installed, or disabled by configuration)."""


def find_soffice(configured: str | None = None) -> str | None:
    """Resolve the soffice executable: an explicit path, else PATH lookup."""
    if configured:
        found = shutil.which(configured)
        return found or (configured if os.access(configured, os.X_OK) else None)
    return shutil.which("soffice") or shutil.which("libreoffice")


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()


def _run_subprocess(cmd: list[str], timeout_s: float) -> int:
    """Run `cmd` in its own process group; kill the group on timeout and
    always afterwards (soffice may leave `soffice.bin` behind)."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)
        raise ConversionTimeout(
            f"LibreOffice conversion timed out after {timeout_s:g} s"
        ) from exc
    finally:
        _kill_group(proc)


class LegacyConverter:
    """Converts a legacy Office file to a temporary modern copy.

    `runner(cmd, timeout_s) -> returncode` is injectable so tests never need
    LibreOffice. `soffice` may be a path, a bare command name, or None to
    auto-detect; `enabled=False` makes the converter report unavailable.
    """

    def __init__(
        self,
        soffice: str | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        enabled: bool = True,
        runner: Callable[[list[str], float], int] | None = None,
    ) -> None:
        self._configured = soffice
        self._timeout_s = timeout_s
        self._enabled = enabled
        self._runner = runner or _run_subprocess
        self._resolved: str | None = None
        self._resolved_done = False
        self._profile_dir: str | None = None

    # -- availability ------------------------------------------------------

    def executable(self) -> str | None:
        if not self._enabled:
            return None
        if not self._resolved_done:
            self._resolved = find_soffice(self._configured)
            self._resolved_done = True
        return self._resolved

    def available(self) -> bool:
        return self.executable() is not None

    def unavailable_reason(self) -> str:
        if not self._enabled:
            return "legacy Office conversion is disabled"
        return "LibreOffice (soffice) is not installed"

    # -- conversion --------------------------------------------------------

    def _profile(self) -> str:
        """A private LibreOffice profile, reused between conversions (a cold
        profile costs seconds) and removed at interpreter exit."""
        if self._profile_dir is None or not os.path.isdir(self._profile_dir):
            self._profile_dir = tempfile.mkdtemp(prefix="docket-lo-profile-")
            atexit.register(shutil.rmtree, self._profile_dir, True)
        return self._profile_dir

    def command(self, exe: str, src: Path, fmt: str, outdir: Path) -> list[str]:
        profile_uri = Path(self._profile()).as_uri()
        return [
            exe,
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            "--nologo",
            "--convert-to",
            fmt,
            "--outdir",
            str(outdir),
            str(src),
        ]

    @contextlib.contextmanager
    def converted(self, path: Path) -> Iterator[Path]:
        """Yield the path of a temporary converted copy of `path`; the copy
        (and everything LibreOffice wrote) is deleted on exit."""
        path = Path(path)
        fmt = LEGACY_TARGETS.get(path.suffix.lower())
        if fmt is None:
            raise ConversionError(f"{path.suffix or 'this file type'} is not a legacy Office format")
        exe = self.executable()
        if exe is None:
            raise ConverterUnavailable(self.unavailable_reason())
        with tempfile.TemporaryDirectory(prefix="docket-lo-out-") as tmp:
            outdir = Path(tmp)
            try:
                with _CONVERT_LOCK:
                    code = self._runner(self.command(exe, path, fmt, outdir), self._timeout_s)
            except (ConversionError, ConverterUnavailable):
                raise
            except OSError as exc:
                raise ConversionError(f"could not run LibreOffice: {exc}") from exc
            produced = outdir / f"{path.stem}.{fmt}"
            if not produced.is_file():
                candidates = sorted(outdir.glob(f"*.{fmt}"))
                produced = candidates[0] if candidates else produced
            if not produced.is_file() or produced.stat().st_size == 0:
                raise ConversionError(
                    f"LibreOffice could not convert this file (exit code {code}); "
                    "the file may be unreadable"
                )
            yield produced
