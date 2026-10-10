"""Screen A terminal UI.

``docket tui-demo`` runs it on invented data (see Upgrade/15); ``docket ui``
runs the same views on the real services (see Upgrade/16).
"""

from __future__ import annotations

from typing import Any

from docket.interfaces.cli.tui.app import STATES, DemoUI, TuiApp


def run_demo(state: str = "chat", *, reduced_motion: bool = False, ascii_mode: bool | None = None) -> None:
    """Run the full-screen prototype until the user exits."""
    DemoUI(state, reduced_motion=reduced_motion, ascii_mode=ascii_mode).run()


def run_ui(
    context: Any,
    *,
    reduced_motion: bool = False,
    ascii_mode: bool | None = None,
    backend: Any = None,
) -> None:
    """Run the full-screen UI against real services until the user exits.

    Leaves the terminal restored on any exit path (prompt_toolkit owns raw mode
    and the alternate screen). If indexing was running when the user chose
    "Stop and exit", waits for it to stop after its current file first.
    """
    from docket.interfaces.cli.tui.real_backend import RealBackend

    ui = TuiApp(backend or RealBackend(context), reduced_motion=reduced_motion, ascii_mode=ascii_mode)
    try:
        ui.run()
    finally:
        if ui.active_workers():
            print("Finishing the file being indexed before exit (press Ctrl+C to leave immediately)...", flush=True)
            try:
                ui.wait_for_workers()
            except KeyboardInterrupt:
                pass


__all__ = ["STATES", "DemoUI", "TuiApp", "run_demo", "run_ui"]
