"""Screen A terminal UI prototype (fake data only; see Upgrade/15)."""

from __future__ import annotations

from docket.interfaces.cli.tui.app import STATES, DemoUI


def run_demo(state: str = "chat", *, reduced_motion: bool = False, ascii_mode: bool | None = None) -> None:
    """Run the full-screen prototype until the user exits."""
    DemoUI(state, reduced_motion=reduced_motion, ascii_mode=ascii_mode).run()


__all__ = ["STATES", "DemoUI", "run_demo"]
