"""Launch the Pokemon Red AI Harness HTTP server.

Usage:
    python scripts/run_server.py [--host HOST] [--port PORT]
                                 [--rom PATH] [--base-dir DIR]

Defaults: localhost:8000, rom=pokered-fork/pokered.gbc, base-dir=sessions/.
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

# Allow running from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--rom", default=str(ROOT / "pokered-fork" / "pokered.gbc"),
        help="Default ROM used when /sessions/start is called without rom_path.",
    )
    parser.add_argument(
        "--base-dir", default=str(ROOT / "sessions"),
        help="Where session folders live.",
    )
    parser.add_argument("--ttl-seconds", type=int, default=600,
                        help="In-memory cache TTL for open sessions.")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument(
        "--window", default="null", choices=["null", "SDL2"],
        help="PyBoy window backend. 'null' is headless (default). 'SDL2' opens a visible Game Boy screen — useful for watching an agent play.",
    )
    parser.add_argument(
        "--process", action="store_true",
        help="Run each session's PyBoy in a separate process. Required with --window SDL2 on macOS so the SDL event loop has a dedicated main thread.",
    )
    parser.add_argument(
        "--auto-session", default=None,
        help="If set, pre-create a session with this name on startup so the SDL window appears immediately. Implies a working ROM.",
    )
    parser.add_argument(
        "--auto-session-interval", type=int, default=60,
        help="snapshot_interval_frames for the auto-created session.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Silence PyBoy's noisy sym-load warnings.
    logging.getLogger("pyboy.pyboy").setLevel(logging.ERROR)

    # Hide uvicorn's per-request log for the high-frequency polling endpoints
    # the browser viewer hits (otherwise every 200ms a "POST /screen ... 200" line
    # buries everything else).
    class _QuietAccessFilter(logging.Filter):
        QUIET = ("/screen", "/viewer", "/viewer/stream", "/snapshots/history", "/tick")
        def filter(self, record):
            msg = record.getMessage()
            return not any(p in msg for p in self.QUIET)
    logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())

    rom_path = args.rom if Path(args.rom).exists() else None
    if rom_path is None:
        print(
            f"warning: default ROM {args.rom!r} not found. /sessions/start will "
            "need an explicit rom_path."
        )

    # Auto-enable process mode when the user asks for the SDL window — on macOS
    # the native window won't work without it.
    use_process = args.process or (args.window == "SDL2")

    app = create_app(
        base_dir=args.base_dir,
        rom_path=rom_path,
        ttl_seconds=args.ttl_seconds,
        window=args.window,
        use_process=use_process,
    )

    if args.auto_session and rom_path:
        try:
            existing = {m.session_id for m in app.state.registry.list()}
            if args.auto_session in existing:
                session = app.state.registry.get_or_load(args.auto_session)
                print(f"resumed existing session: {session.session_id}")
            else:
                session = app.state.registry.create(
                    name=args.auto_session,
                    snapshot_interval_frames=args.auto_session_interval,
                )
                print(f"auto-created session: {session.session_id}")
            print(f"  X-Session-Id: {session.session_id}")
        except Exception as e:
            print(f"warning: failed to auto-create session: {e}")

    import uvicorn
    # `timeout_graceful_shutdown=2` so Ctrl-C doesn't wait 30s for the MJPEG
    # stream connections to drain on their own.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        timeout_graceful_shutdown=2,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
