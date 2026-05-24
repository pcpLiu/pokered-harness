"""Quick demo: load the instrumented ROM, press Start a few times, print events.

Usage:
    python demo.py [path/to/pokered.gbc]

If no path is given, defaults to ./pokered-fork/pokered.gbc.
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("pyboy.pyboy").setLevel(logging.ERROR)

from harness.emulator import Emulator
from harness.telemetry import TelemetryParser, request_snapshot


def main(rom_path: str) -> int:
    print(f"Loading ROM: {rom_path}")
    emu = Emulator(rom_path)
    parser = TelemetryParser(events_yaml_path="harness/events.yaml")

    # Drive through boot → title → main menu so we land somewhere with
    # readable text.
    print("Booting (900 frames)...")
    raw = emu.step(900)
    events = parser.feed(raw, frame=emu.frame)
    _print_events(events, prefix="boot")

    print("\nPressing START to dismiss copyright...")
    raw = emu.press_button("start", hold_frames=10, release_frames=80)
    events = parser.feed(raw, frame=emu.frame)
    _print_events(events, prefix="post-start-1")

    print("\nPressing START again to leave title...")
    raw = emu.press_button("start", hold_frames=10, release_frames=80)
    events = parser.feed(raw, frame=emu.frame)
    _print_events(events, prefix="post-start-2")

    print("\nPressing A on main menu, waiting for Oak intro...")
    raw = emu.press_button("a", hold_frames=10, release_frames=120)
    events = parser.feed(raw, frame=emu.frame)
    _print_events(events, prefix="post-a")

    raw = emu.step(600)
    events = parser.feed(raw, frame=emu.frame)
    _print_events(events, prefix="oak-intro")

    # Request a snapshot. Snapshots are only emitted from OverworldLoop, so
    # during the intro this will time out — we still call it to demonstrate the
    # API. After a session is in the overworld this works reliably.
    print("\nRequesting snapshot (may time out during intro)...")
    try:
        snap = request_snapshot(emu, parser, timeout_frames=120)
        print(
            f"  snapshot ok: frame={snap.frame} map_id={snap.map_id} "
            f"x={snap.x} y={snap.y} party_count={snap.party_count} "
            f"in_battle={snap.in_battle} text_box_id={snap.text_box_id}"
        )
    except TimeoutError as e:
        print(f"  snapshot timeout (expected pre-overworld): {e}")

    emu.close()
    return 0


def _print_events(events, prefix: str = "") -> None:
    if not events:
        print(f"  [{prefix}] (no events)")
        return
    print(f"  [{prefix}] {len(events)} events:")
    for ev in events[:20]:
        payload = ev.payload
        if ev.id == "text_display":
            print(f"    frame={ev.frame:>6} {ev.id}: {payload['string']!r}")
        elif ev.id == "snapshot":
            print(
                f"    frame={ev.frame:>6} snapshot map={payload['map_id']} "
                f"x={payload['x']} y={payload['y']} party={payload['party_count']}"
            )
        else:
            print(f"    frame={ev.frame:>6} {ev.id} {payload}")
    if len(events) > 20:
        print(f"    ... and {len(events) - 20} more")


if __name__ == "__main__":
    rom = sys.argv[1] if len(sys.argv) > 1 else "pokered-fork/pokered.gbc"
    if not Path(rom).exists():
        print(f"ROM not found: {rom}")
        print("Build with: cd pokered-fork && make LLM_TELEMETRY=1")
        sys.exit(1)
    sys.exit(main(rom))
