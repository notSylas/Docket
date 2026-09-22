# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the REAL desktop sidecar (main.py + server.py, wired
# to the full `docket` backend: docling, lancedb, langgraph, alembic, ...).
# Rewritten for the IPC-wiring checkpoint from the earlier stub-only spec
# (which froze the old 7-line ping/echo `main.py`).
#
# Build with the dedicated CPU-torch venv, NOT `backend/.venv` (which has
# GPU-build torch pulled in transitively by docling -- this app never needs
# CUDA in-process; Ollama does all real LLM inference as its own process,
# and docling's OCR/layout models run fine on CPU):
#
#     cd desktop/sidecar
#     source .build-venv/bin/activate
#     pyinstaller app-sidecar-x86_64-unknown-linux-gnu.spec --noconfirm
#
# Real fixes required beyond the stub spec, found by an earlier
# de-risking spike (1, 2) and this checkpoint's own build (3):
#
# 1. Alembic's `env.py` is loaded by Alembic itself via `exec_module` (not a
#    normal `import` statement anywhere in the traced module graph), so
#    PyInstaller's static analyzer misses `env.py`'s own
#    `from logging.config import fileConfig` -- must be listed explicitly
#    as a hiddenimport.
# 2. `docling.__version__` (and `docling-slim`'s) reports "unknown" when
#    frozen unless each package's dist-info metadata is bundled explicitly
#    via `copy_metadata` -- affects `ChunkRecipe.parser_version`
#    (cosmetic today, matters for cache-key correctness later).
# 3. Docling discovers its own default OCR/layout/table models through a
#    `importlib.metadata.entry_points(group="docling")` plugin lookup (one
#    entry point: `docling_defaults -> docling.models.plugins.defaults`),
#    not a literal `import` statement anywhere in the traced source --
#    PyInstaller's static analyzer never sees that module is reachable, so
#    it's silently missing from the frozen bundle unless hiddenimport'd
#    explicitly. Without this, `sources.ingest` fails at parse time with
#    `No module named 'docling.models.plugins'` (confirmed by this
#    checkpoint's first frozen-binary standalone verification run).
# 4. `python-docx` ships non-Python XML template resources under
#    `docx/templates/` (e.g. `default-comments.xml`) that its own code
#    resolves at runtime relative to its OWN module file, e.g.
#    `docx/parts/comments.py` does
#    `os.path.join(os.path.split(__file__)[0], '..', 'templates', ...)` --
#    i.e. `docx/parts/../templates/default-comments.xml`, NOT a normalized
#    `docx/templates/default-comments.xml`. `collect_data_files('docx')`
#    (also pulled in automatically by PyInstaller's own community
#    `hook-docx.py`) correctly extracts `docx/templates/*` onto disk under
#    `sys._MEIPASS` -- but `docx`'s pure-Python modules (`docx/parts/*.py`
#    included) are pure Python, so by default PyInstaller packs them into
#    the zipped PYZ archive and never extracts them as loose files, which
#    means the `docx/parts/` DIRECTORY never exists on disk at all. Linux
#    path resolution walks a `..` component through its parent directory
#    on the real filesystem -- it does not algebraically simplify the
#    string -- so `docx/parts/../templates/default-comments.xml` raises
#    `FileNotFoundError` even though `docx/templates/default-comments.xml`
#    genuinely exists right next to it. Confirmed by directly comparing
#    `os.path.exists()` on both forms inside a live frozen process's
#    `_MEIPASS` during this checkpoint's investigation. Fixed by forcing
#    `docx`'s own modules to ALSO be collected as loose source files (not
#    just inside the PYZ) via `module_collection_mode`, so `docx/parts/`
#    exists as a real directory alongside `docx/templates/`.
#
# `alembic.ini` and the migrations folder are bundled as `datas` at the
# same relative layout `docket.cli.context.REPO_ROOT` expects (see that
# module's frozen-aware `REPO_ROOT` branch): `alembic.ini` at the bundle
# root, migrations at `src/docket/db/migrations` under the bundle root.

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = [
    ('../../backend/alembic.ini', '.'),
    ('../../backend/src/docket/db/migrations', 'src/docket/db/migrations'),
]
datas += copy_metadata('docling')
datas += copy_metadata('docling-slim')
datas += collect_data_files('docx')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['logging.config', 'docling.models.plugins.defaults'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
    # See finding #4 above: `docx/parts/*.py` do `__file__`-relative `..`
    # traversal to reach `docx/templates/*` data files, which requires the
    # `docx/parts/` directory to exist for real on disk, not just inside
    # the zipped PYZ archive.
    module_collection_mode={'docx': 'pyz+py'},
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='app-sidecar-x86_64-unknown-linux-gnu',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
