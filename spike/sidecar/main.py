"""Minimal stand-in for the Python backend, to test Tauri sidecar packaging.
Just proves a bundled Python process can start, print, and exit cleanly."""
import sys

if __name__ == "__main__":
    print("python-sidecar-ok", flush=True)
    sys.exit(0)
