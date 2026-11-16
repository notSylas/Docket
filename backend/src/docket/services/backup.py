"""Verified local snapshots, without inference or opening a live vector writer.

Only cooperating Docket writers are coordinated. Restore uses Linux's atomic
directory exchange and preserves the lock inode in both directories. Existing
windows must restart to discard their old database/vector handles.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from docket import __version__
from docket.core.writer_lock import LOCK_FILENAME, WriterLock
from docket.infra.index.manifest import read_manifest

MANIFEST = "backup.json"
ROOT_FILES = {"docket.sqlite3", "index_manifest.json", "preferences.json"}
ROOT_TREES = {"lancedb", "evidence_store", "feedback"}


class BackupError(ValueError):
    """A user-facing backup/restore failure."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError("The library or backup contains a symbolic link. Nothing was replaced.")
        if path.is_file():
            yield path
        elif not path.is_dir():
            raise BackupError("The library or backup contains a special file. Nothing was replaced.")


def _safe_relative(name: str) -> Path:
    pure = PurePosixPath(name)
    if (not name or "\\" in name or pure.is_absolute() or ".." in pure.parts
            or pure.as_posix() != name or not pure.parts):
        raise BackupError("The backup contains an unsafe file path.")
    if not ((len(pure.parts) == 1 and name in ROOT_FILES)
            or (len(pure.parts) > 1 and pure.parts[0] in ROOT_TREES)):
        raise BackupError(f"The backup contains an unexpected file: {name}")
    return Path(*pure.parts)


def _database(root: Path):
    path = root / "docket.sqlite3"
    if not path.is_file() or path.is_symlink():
        raise BackupError("The library database is missing.")
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)


def _index_raw(root: Path):
    path = root / "index_manifest.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError) as exc:
        raise BackupError("The index manifest is missing or damaged.") from exc


def _is_reference(root: Path, digest: str) -> bool:
    """True when the evidence manifest says this content is held by reference."""
    manifest = root / "evidence_store" / "manifests" / digest[:2] / f"{digest}.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("storage") == "reference"
    except (OSError, ValueError, AttributeError):
        return False


def check_library(root: Path, *, expected_index=None, expected_revision=None,
                  verified: frozenset[str] | set[str] = frozenset()) -> str:
    """Check original references and vector metadata locally, without models.

    ``verified`` holds content hashes whose bytes were already hashed in the
    same run (while copying or while checking the backup manifest); those are
    only checked for presence, so a snapshot is never read an extra time."""
    try:
        with closing(_database(root)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("The restored database failed its integrity check.")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("The restored database has broken references.")
            revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            if expected_revision is not None and revision != expected_revision:
                raise BackupError("The database migration revision does not match the backup manifest.")
            originals = db.execute(
                "SELECT DISTINCT content_hash FROM evidence_versions WHERE status IN ('READY', 'TEXT_READY')").fetchall()
            for (digest,) in originals:
                if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    raise BackupError("A stored original has an invalid content hash.")
                path = root / "evidence_store" / "objects" / digest[:2] / digest
                if not path.exists() and _is_reference(root, digest):
                    continue  # reference mode: the original stays in the user's folder
                if not path.is_file() or path.is_symlink() or (digest not in verified and _hash(path) != digest):
                    raise BackupError(f"A READY document's original is missing or damaged: {digest[:12]}.")
            ready_chunks = db.execute(
                "SELECT COUNT(*) FROM chunks c JOIN evidence_versions v "
                "ON c.evidence_version_id = v.id WHERE v.status = 'READY'").fetchone()[0]
        if _index_raw(root) != expected_index:
            raise BackupError("The index manifest does not match the backup manifest.")
        manifest = read_manifest(root / "index_manifest.json")
        vectors = root / "lancedb"
        tables = []
        if vectors.exists() and any(vectors.iterdir()):
            import lancedb

            db = lancedb.connect(str(vectors))
            token = None
            while True:
                page = db.list_tables(page_token=token)
                tables.extend(page.tables)
                token = page.page_token
                if not token:
                    break
            for name in ("chunks", "pages"):
                if name in tables:
                    if manifest is None:
                        raise BackupError("The vector store has no index manifest. Reindex before backing up.")
                    dimension = db.open_table(name).schema.field("vector").type.list_size
                    if dimension != manifest.embed_dimension:
                        raise BackupError("The vector dimensions do not match the index manifest.")
        if ready_chunks and "chunks" not in tables:
            raise BackupError("The library's document vector index is missing.")
        return revision
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("The library could not be verified. The database or index metadata is damaged.") from exc


def _copy_hashed(source: Path, target: Path) -> tuple[int, str]:
    """Copy ``source`` to ``target`` in one read pass: returns (size, sha256),
    and flushes the copy to disk so no later pass has to open it again."""
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as stream, target.open("wb") as out:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
            out.write(block)
        out.flush()
        os.fsync(out.fileno())
    return size, digest.hexdigest()


def _previous_objects(reuse_from: Path | None) -> dict[str, int]:
    """Evidence objects (digest -> size) that a previous directory backup already
    holds, verified when it was made. Anything unreadable simply is not reused."""
    if reuse_from is None:
        return {}
    try:
        previous = json.loads((reuse_from / MANIFEST).read_text(encoding="utf-8"))
        reusable: dict[str, int] = {}
        for name, record in previous["files"].items():
            parts = PurePosixPath(name).parts
            if (len(parts) == 4 and parts[:2] == ("evidence_store", "objects")
                    and record.get("sha256") == parts[3] and parts[2] == parts[3][:2]
                    and (reuse_from / name).is_file() and not (reuse_from / name).is_symlink()
                    and (reuse_from / name).stat().st_size == record["size"]):
                reusable[parts[3]] = record["size"]
        return reusable
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sync_tree(root: Path, *, files: bool = True) -> None:
    if files:
        for path in _files(root):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in root.rglob("*") if p.is_dir()),
                       key=lambda p: len(p.parts), reverse=True):
        _sync_directory(path)
    _sync_directory(root)


def _copy_database(source, copied) -> None:
    busy_since = None

    def progress(status, remaining, total):
        nonlocal busy_since
        if status in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            if busy_since is None:
                busy_since = time.monotonic()
            elif time.monotonic() - busy_since >= 5:
                raise BackupError("The database is busy. Try the backup again when the other update finishes.")
        else:
            busy_since = None

    source.backup(copied, pages=256, progress=progress, sleep=.05)
    copied.execute("PRAGMA journal_mode=DELETE")


def _needed_objects(database: Path) -> set[str]:
    """Content hashes the snapshot database still references (best effort)."""
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            return {r[0] for r in db.execute("SELECT DISTINCT content_hash FROM evidence_blob_references")}
    except sqlite3.Error:
        return set()


def backup(data_dir: Path, destination: Path | None = None, *, writer_lock: WriterLock | None = None,
           reuse_from: Path | None = None) -> Path:
    """Verified snapshot of the library.

    The writer lock is held only while the mutable parts (database, vectors,
    preferences, feedback, evidence manifests) are captured and the evidence
    object list is taken. Evidence objects are immutable and named by their
    sha256, so they are copied afterwards without blocking writers. Every byte is
    read once: the copy computes the manifest hash, flushes the copy, and (for
    objects) proves the file still matches its own name. ``reuse_from`` (a
    previous directory backup) lets unchanged objects be hard-linked instead of
    copied or re-read.
    """
    root = Path(data_dir).expanduser().resolve()
    target = (Path(destination).expanduser().absolute() if destination is not None
              else root.parent / f"docket-backup-{_stamp()}")
    target = target.resolve()
    if target == root or root in target.parents:
        raise BackupError("Choose a backup destination outside the library data directory.")
    if target.exists():
        raise BackupError("The backup destination already exists. Choose a new name.")
    reuse = Path(reuse_from).expanduser().resolve() if reuse_from is not None else None
    staging = None
    try:
        records: dict[str, tuple[int, str]] = {}
        pending: list[Path] = []
        with (writer_lock or WriterLock(root)).hold("backing up the library"):
            # Open read-only first: an uninitialized install must not acquire
            # an empty SQLite database merely by asking for a backup.
            with closing(_database(root)) as source:
                target.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".docket-backup-", dir=target.parent))
                payload = staging / "payload"
                payload.mkdir(mode=0o700)
                with closing(sqlite3.connect(payload / "docket.sqlite3")) as copied:
                    _copy_database(source, copied)
            database = payload / "docket.sqlite3"
            records["docket.sqlite3"] = (database.stat().st_size, _hash(database))
            with database.open("rb") as stream:
                os.fsync(stream.fileno())
            for name in sorted(ROOT_FILES - {"docket.sqlite3"}):
                path = root / name
                if path.is_symlink():
                    raise BackupError("The library contains a symbolic link.")
                if path.exists():
                    records[name] = _copy_hashed(path, payload / name)
            objects = Path("evidence_store") / "objects"
            for name in sorted(ROOT_TREES):
                tree = root / name
                if tree.is_symlink():
                    raise BackupError("The library contains a symbolic link.")
                if not tree.exists():
                    continue
                for path in _files(tree):
                    relative = path.relative_to(root)
                    if objects in relative.parents:
                        if not path.name.startswith(".tmp-"):
                            pending.append(path)  # immutable: copied after the lock is released
                        continue
                    records[relative.as_posix()] = _copy_hashed(path, payload / relative)
                    if name == "feedback":
                        (payload / "feedback").chmod(0o700)
                        (payload / relative).chmod(0o600)
        # -- lock released: evidence objects are write-once and content-named --
        needed = _needed_objects(database)
        reusable = _previous_objects(reuse)
        verified: set[str] = set()
        for path in pending:
            relative = path.relative_to(root)
            digest = path.name
            named = len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
            copied = payload / relative
            if digest in reusable and named:
                try:
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    os.link(reuse / relative, copied)
                    records[relative.as_posix()] = (reusable[digest], digest)
                    verified.add(digest)
                    continue
                except OSError:
                    copied.unlink(missing_ok=True)
            try:
                result = _copy_hashed(path, copied)
            except FileNotFoundError:
                trashed = root / "evidence_store" / "trash" / digest
                if digest in needed and trashed.is_file():
                    result = _copy_hashed(trashed, copied)  # moved to trash after the snapshot
                elif digest in needed:
                    raise BackupError("The library changed while it was being backed up. Try again.") from None
                else:
                    continue  # no longer referenced by the snapshot
            if named:
                if result[1] != digest:
                    raise BackupError(f"A stored original is damaged: {digest[:12]}.")
                verified.add(digest)
            records[relative.as_posix()] = result
        index = _index_raw(payload)
        revision = check_library(payload, expected_index=index, verified=verified)
        files = {}
        for p in _files(payload):
            relative = p.relative_to(payload).as_posix()
            size, digest = records[relative] if relative in records else (p.stat().st_size, _hash(p))
            files[relative] = {"size": size, "sha256": digest}
        manifest = {
            "format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "docket_version": __version__, "migration_revision": revision,
            "index_manifest": index, "files": files,
        }
        (payload / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        _sync_tree(payload, files=False)
        with (payload / MANIFEST).open("rb") as stream:
            os.fsync(stream.fileno())
        if target.suffix.lower() == ".zip":
            archive = staging / "snapshot.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
                for path in _files(payload):
                    relative = path.relative_to(payload)
                    # Originals are usually compressed already (pdf, docx, xlsx).
                    method = zipfile.ZIP_STORED if relative.parts[:2] == ("evidence_store", "objects") \
                        else zipfile.ZIP_DEFLATED
                    out.write(path, relative.as_posix(), compress_type=method)
            archive.chmod(0o600)
            with archive.open("rb") as stream:
                os.fsync(stream.fileno())
            # Publishing a ZIP must not replace a concurrently created
            # destination. Both paths are on the same filesystem.
            os.link(archive, target)
        else:
            os.rename(payload, target)
        _sync_directory(target.parent)
        return target
    except (BackupError,):
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError("The backup could not be written. Check disk space and destination permissions.") from exc
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def _read_snapshot(source: Path, staging: Path) -> dict:
    """Whitelist entries, stream them, and verify hashes before any swap."""
    archive = None
    try:
        if source.is_dir() and not source.is_symlink():
            files = {p.relative_to(source).as_posix(): p for p in _files(source)}
            if MANIFEST not in files or files[MANIFEST].stat().st_size > 32 * 1024 * 1024:
                raise BackupError("The backup manifest is missing or too large.")
            manifest = json.loads(files[MANIFEST].read_text(encoding="utf-8"))
            opener = lambda name: files[name].open("rb")
        else:
            archive = zipfile.ZipFile(source)
            entries = archive.infolist()
            files = {i.filename: i for i in entries}
            if len(files) != len(entries) or any(
                i.is_dir() or stat.S_ISLNK(i.external_attr >> 16) for i in entries):
                raise BackupError("The backup archive contains duplicate or unsafe entries.")
            if MANIFEST not in files or files[MANIFEST].file_size > 32 * 1024 * 1024:
                raise BackupError("The backup manifest is missing or too large.")
            manifest = json.loads(archive.read(MANIFEST))
            opener = archive.open
        if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
            raise BackupError("This backup format is not supported.")
        records = manifest.get("files")
        if not isinstance(records, dict) or "docket.sqlite3" not in records:
            raise BackupError("The backup has no database file record.")
        if set(files) != {*records, MANIFEST}:
            raise BackupError("The backup is incomplete or contains unlisted files.")
        if not isinstance(manifest.get("migration_revision"), str) or "index_manifest" not in manifest:
            raise BackupError("The backup is missing migration or index metadata.")
        for name, record in records.items():
            relative = _safe_relative(name)
            if not isinstance(record, dict) or type(record.get("size")) is not int or record["size"] < 0:
                raise BackupError("The backup contains an invalid file size.")
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with opener(name) as stream, path.open("wb") as out:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    size += len(block)
                    if size > record["size"]:
                        raise BackupError(f"A backup file has an incorrect size: {name}")
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if size != record["size"] or digest.hexdigest() != record.get("sha256"):
                raise BackupError(f"A backup file is missing or damaged: {name}")
            if relative.parts[0] == "feedback":
                (staging / "feedback").chmod(0o700)
                path.chmod(0o600)
        return manifest
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("The backup cannot be read or is corrupt. Nothing was replaced.") from exc
    finally:
        if archive is not None:
            archive.close()


def _exchange(left: Path, right: Path) -> None:
    """Linux renameat2(RENAME_EXCHANGE): neither library path disappears."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise BackupError("Safe directory restore is unavailable on this platform.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise BackupError("This filesystem does not support safe directory restore.")
        raise OSError(error, os.strerror(error))


def restore(data_dir: Path, source: Path, *, force: bool = False,
            writer_lock: WriterLock | None = None) -> Path:
    root = Path(data_dir).expanduser().resolve()
    source = Path(source).expanduser().resolve()
    if source == root or root in source.parents:
        raise BackupError("Restore from a backup outside the library data directory.")
    staging, swapped = None, False
    try:
        with (writer_lock or WriterLock(root)).hold("restoring the library"):
            if not force and any(p.name != LOCK_FILENAME for p in root.iterdir()):
                raise BackupError("The library data directory is not empty. Use --force to keep it as a dated side copy and restore.")
            staging = Path(tempfile.mkdtemp(prefix=f".{root.name}.previous-{_stamp()}-", dir=root.parent))
            manifest = _read_snapshot(source, staging)
            # _read_snapshot hashed every file against the manifest; an object
            # whose recorded hash is its own name is therefore already proven.
            proven = frozenset(
                PurePosixPath(n).name for n, r in manifest["files"].items()
                if PurePosixPath(n).parts[:2] == ("evidence_store", "objects") and r.get("sha256") == PurePosixPath(n).name)
            check_library(staging, expected_index=manifest["index_manifest"],
                          expected_revision=manifest["migration_revision"], verified=proven)
            # Waiting writers may already have opened this inode. Hard-link
            # it into the replacement to keep all of them on the same lock.
            os.link(root / LOCK_FILENAME, staging / LOCK_FILENAME)
            _sync_tree(staging, files=False)  # files were flushed as they were written
            _exchange(root, staging)
            swapped = True
            try:
                check_library(root, expected_index=manifest["index_manifest"],
                              expected_revision=manifest["migration_revision"], verified=proven)
                _sync_directory(root.parent)
            except BaseException:
                _exchange(root, staging)
                swapped = False
                _sync_directory(root.parent)
                raise
            return staging
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("Restore could not finish. Check disk space and permissions. The previous library has been kept.") from exc
    finally:
        if staging is not None and not swapped:
            shutil.rmtree(staging, ignore_errors=True)
