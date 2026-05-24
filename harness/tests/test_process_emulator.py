"""Smoke tests for the out-of-process emulator.

Process spawn is slow (~1-2s), so these tests are intentionally small. The
shared Emulator API surface is already covered by test_emulator.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROM_PATH = Path(__file__).resolve().parents[2] / "pokered-fork" / "pokered.gbc"
ROM_AVAILABLE = ROM_PATH.exists()

pytestmark = pytest.mark.skipif(
    not ROM_AVAILABLE,
    reason=f"instrumented ROM not built: {ROM_PATH}.",
)


@pytest.fixture(scope="module")
def emu():
    from harness.process_emulator import EmulatorProcess
    e = EmulatorProcess(str(ROM_PATH))
    try:
        yield e
    finally:
        e.close()


def test_process_emulator_loads(emu):
    assert emu.frame == 0


def test_process_emulator_step_returns_bytes(emu):
    raw = emu.step(60)
    assert isinstance(raw, bytes)


def test_process_emulator_press_button(emu):
    raw = emu.press_button("start", hold_frames=3, release_frames=3)
    assert isinstance(raw, bytes)


def test_process_emulator_screen(emu):
    arr = emu.get_screen()
    assert arr.shape == (144, 160, 3)
    assert arr.dtype.name == "uint8"


def test_process_emulator_ram(emu):
    emu.write_ram(0xC100, b"\xAB\xCD\xEF")
    assert emu.read_ram(0xC100, 3) == b"\xAB\xCD\xEF"


def test_process_emulator_state_round_trip(tmp_path):
    """Save/load round-trips the logical frame via the EmulatorProcess interface."""
    from harness.process_emulator import EmulatorProcess
    emu = EmulatorProcess(str(ROM_PATH))
    try:
        emu.step(120)
        pre = emu.frame
        state_path = tmp_path / "state.bin"
        emu.save_state(state_path)
        emu.step(60)
        assert emu.frame == pre + 60
        emu.load_state(state_path)
        assert emu.frame == pre
    finally:
        emu.close()


def test_process_emulator_session_integration(tmp_path):
    """A Session backed by EmulatorProcess behaves the same as the in-process one."""
    from harness.sessions import Session
    session = Session.create(
        tmp_path / "sessions",
        ROM_PATH,
        snapshot_interval_frames=0,
        use_process=True,
    )
    try:
        session.wait(120)
        evs = session.press_button("start", hold_frames=3, release_frames=3)
        assert isinstance(evs, list)
        # Reload from disk — frame counter should round-trip.
        pre = session.emulator.frame
    finally:
        session.end()

    session2 = Session.open(tmp_path / "sessions" / session.session_id, use_process=True)
    try:
        assert session2.emulator.frame == pre
    finally:
        session2.end()
