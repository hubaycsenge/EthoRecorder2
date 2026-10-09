"""Entry point:  python -m ethorecorder2  [--demo] [--headless --duration S]"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from . import APP_NAME, __version__
from .config import Settings


def run_headless(settings: Settings, demo: bool, duration: float) -> int:
    """Connect, record ``duration`` seconds, save, exit.  For tests and scripts."""
    from .engine import Engine

    done = threading.Event()
    result = {}
    engine = Engine(settings, log=lambda m: print(m, flush=True), demo=demo)
    engine.on_finished = lambda summary: (result.update(summary), done.set())
    engine.connect()
    try:
        time.sleep(3.0)
        engine.start_recording()
        t_end = time.monotonic() + duration
        while time.monotonic() < t_end:
            time.sleep(0.2)
        engine.stop_recording()
        done.wait()
    finally:
        engine.disconnect()
    return 0 if result.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME, description="Synchronised Basler camera + audio recorder")
    ap.add_argument("--demo", action="store_true", help="use pylon's emulated cameras instead of real ones")
    ap.add_argument("--headless", action="store_true", help="record without a window, then exit")
    ap.add_argument("--duration", type=float, default=10.0, help="headless recording length in seconds")
    ap.add_argument("--output", help="output folder (overrides the saved setting for this run)")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = ap.parse_args()

    settings = Settings.load()
    if args.output:
        settings.output_dir = args.output
    if args.demo:
        import os

        os.environ.setdefault("PYLON_CAMEMU", "2")
    if args.headless:
        return run_headless(settings, args.demo, args.duration)
    from .gui import run_gui

    return run_gui(settings, args.demo)


if __name__ == "__main__":
    sys.exit(main())
