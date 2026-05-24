"""Smoke tests that exercise the Emulator wrapper against the real instrumented
ROM. These require the ROM to have been built with `make LLM_TELEMETRY=1`.

Skipped automatically if the ROM file is absent so the test suite still runs on
machines that haven't built the fork yet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.emulator import Emulator, VALID_BUTTONS, rom_sha1
from harness.telemetry import TelemetryParser


ROM_PATH = Path(__file__).resolve().parents[2] / "pokered-fork" / "pokered.gbc"
ROM_AVAILABLE = ROM_PATH.exists()

pytestmark = pytest.mark.skipif(
    not ROM_AVAILABLE,
    reason=f"instrumented ROM not built: {ROM_PATH}. Run `make LLM_TELEMETRY=1` in pokered-fork/.",
)


@pytest.fixture
def emu():
    e = Emulator(str(ROM_PATH))
    try:
        yield e
    finally:
        e.close()


def test_emulator_loads_and_advances(emu):
    assert emu.frame == 0
    raw = emu.step(60)
    assert emu.frame == 60
    # Telemetry stream may or may not have bytes in first 60 frames — both fine
    # (the boot logo runs before any PlaceString calls). Just check the type.
    assert isinstance(raw, bytes)


def test_emulator_captures_text_display(emu):
    parser = TelemetryParser()
    # Run long enough to see at least the GAME FREAK intro PlaceString calls.
    raw = emu.step(900)
    events = parser.feed(raw, emu.frame)
    # At minimum we expect one text_display event during boot.
    text_events = [e for e in events if e.id == "text_display"]
    assert text_events, "expected at least one text_display event in first 900 frames"


def test_emulator_press_button_valid(emu):
    raw = emu.press_button("start", hold_frames=3, release_frames=3)
    assert isinstance(raw, bytes)
    # press_button itself ran 6 frames (3 hold + 3 release)
    assert emu.frame >= 6


def test_emulator_press_button_invalid(emu):
    with pytest.raises(ValueError):
        emu.press_button("xyz")


def test_emulator_buttons_enum():
    assert "a" in VALID_BUTTONS
    assert "select" in VALID_BUTTONS
    assert len(VALID_BUTTONS) == 8


def test_emulator_screen(emu):
    emu.step(10)
    screen = emu.get_screen()
    assert screen.shape == (144, 160, 3)
    assert screen.dtype.name == "uint8"


def test_emulator_ram_read_write(emu):
    emu.step(60)
    # WRAM region 0xC000-0xCFFF is general-purpose. Write a sentinel and read back.
    emu.write_ram(0xC100, b"\xAB\xCD\xEF")
    assert emu.read_ram(0xC100, 3) == b"\xAB\xCD\xEF"
    # Single-byte write
    emu.write_ram(0xC200, 0x42)
    assert emu.read_ram(0xC200, 1) == b"\x42"


def test_emulator_save_load_state_roundtrip(emu, tmp_path):
    emu.step(120)
    pre_frame = emu.frame
    state_path = tmp_path / "save.state"
    emu.save_state(state_path)
    assert state_path.exists()

    # Advance a bit further; then load — frame should reset to pre_frame.
    emu.step(60)
    assert emu.frame == pre_frame + 60
    emu.load_state(state_path)
    assert emu.frame == pre_frame


def test_emulator_save_load_state_bytes(emu):
    emu.step(60)
    pre = emu.frame
    data = emu.save_state_bytes()
    emu.step(30)
    assert emu.frame == pre + 30
    emu.load_state_bytes(data)
    assert emu.frame == pre


def test_rom_sha1_helper():
    h = rom_sha1(str(ROM_PATH))
    assert len(h) == 40  # SHA-1 hex digest length
    assert all(c in "0123456789abcdef" for c in h.lower())


def test_emulator_sha1_check(tmp_path):
    # Wrong SHA-1 should refuse to load
    with pytest.raises(ValueError, match="SHA-1 mismatch"):
        Emulator(str(ROM_PATH), expected_sha1="0" * 40)

    # Correct SHA-1 should load fine
    correct = rom_sha1(str(ROM_PATH))
    emu = Emulator(str(ROM_PATH), expected_sha1=correct)
    try:
        assert emu.frame == 0
    finally:
        emu.close()
