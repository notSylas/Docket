# Interfaces and User Experience

The detailed terminal design is [Screen A: chat with overlays](13-terminal-ui-screen-a-design.md). It defines screens, interactions, current-backend limitations, and the staged implementation. [The Codex handoff](13-codex-handoff.md) records ownership and validation requirements while accuracy work proceeds separately.

## Launching in its own window

Bare `docket` in a TTY opens the interactive session in a detached terminal window (`docket chat --in-window`) and returns the invoking shell at once. The code is `backend/src/docket/interfaces/cli/launcher.py`: it picks a terminal (`DOCKET_TERMINAL`, `$TERMINAL`, `x-terminal-emulator`, then known emulators), forwards every `DOCKET_*` variable plus `DOCKET_IN_WINDOW=1` (which prevents recursion), and starts it with `start_new_session=True`. It falls back to running in place with no display, over SSH, with `TERM=dumb`, when no terminal is found, off Linux, or with `DOCKET_NO_WINDOW=1`.

`docket launch` always opens a window, and `docket install-launcher` / `docket uninstall-launcher` manage a user-level `.desktop` entry and icon (nothing outside `~/.local/share`). Multiple windows can run at once; the index-mutation lock (doc 13, section 6.3) is still a later item. macOS and Windows are a follow-up.
