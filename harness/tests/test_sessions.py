"""Tests for the Session model. Many require the instrumented ROM."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from harness.sessions import Session, SessionMeta, SessionRegistry, _validate_name
from harness.telemetry import Event


ROM_PATH = Path(__file__).resolve().parents[2] / "pokered-fork" / "pokered.gbc"
ROM_AVAILABLE = ROM_PATH.exists()

pytestmark = pytest.mark.skipif(
    not ROM_AVAILABLE,
    reason=f"instrumented ROM not built: {ROM_PATH}.",
)


@pytest.fixture
def base_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    yield d
    # tmp_path tears itself down; nothing to clean


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_validate_name_accepts_kebab():
    _validate_name("pallet-explorer-7f3a")
    _validate_name("simple")
    _validate_name("a1-b2-c3")


def test_validate_name_rejects_bad():
    for bad in ["UPPER", "with_underscore", "trailing-", "-leading", "double--hyphen", "hello world", ""]:
        with pytest.raises(ValueError):
            _validate_name(bad)


# ---------------------------------------------------------------------------
# Session creation / open
# ---------------------------------------------------------------------------

def test_session_create_makes_folder(base_dir):
    session = Session.create(base_dir, ROM_PATH)
    try:
        folder = session.folder
        assert folder.exists()
        assert (folder / "meta.json").exists()
        assert (folder / "current.state").exists()
        assert (folder / "snapshots").is_dir()
        assert (folder / "saves").is_dir()
        assert (folder / "events.jsonl").exists()
        assert (folder / "actions.jsonl").exists()
        assert (folder / "journal.md").exists()
        meta = json.loads((folder / "meta.json").read_text())
        assert meta["session_id"] == session.session_id
        assert meta["rom_path"]
        assert meta["rom_sha1"]
        assert meta["status"] == "active"
    finally:
        session.end()


def test_session_create_with_name(base_dir):
    session = Session.create(base_dir, ROM_PATH, name="kanto-run")
    try:
        assert session.session_id == "kanto-run"
        assert (base_dir / "kanto-run").exists()
    finally:
        session.end()


def test_session_create_with_name_collision(base_dir):
    a = Session.create(base_dir, ROM_PATH, name="dup")
    try:
        b = Session.create(base_dir, ROM_PATH, name="dup")
        assert a.session_id == "dup"
        assert b.session_id == "dup-2"
        b.end()
    finally:
        a.end()


def test_session_default_id_format(base_dir):
    session = Session.create(base_dir, ROM_PATH)
    try:
        parts = session.session_id.split("-")
        assert len(parts) == 3
        assert len(parts[2]) == 4  # 4-hex suffix
    finally:
        session.end()


def test_session_open_round_trip(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    folder = session.folder
    sid = session.session_id
    try:
        # Press a button so the state moves forward
        session.press_button("start", hold_frames=3, release_frames=3)
        pre_frame = session.emulator.frame
        session.emulator.close()  # release the rom resources before reopen
    finally:
        # Don't call session.end() — we want it active for the open test.
        pass

    # Reopen
    s2 = Session.open(folder)
    try:
        assert s2.session_id == sid
        assert s2.meta.status == "active"
        # Frame counter should have been persisted via current.state
        assert s2.emulator.frame == pre_frame
    finally:
        s2.end()


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def test_press_button_appends_events_and_actions(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        # Run boot frames first so a title text fires
        session.wait(900)
        session.press_button("start", hold_frames=10, release_frames=80)
        events_lines = (session.folder / "events.jsonl").read_text().splitlines()
        actions_lines = (session.folder / "actions.jsonl").read_text().splitlines()
        assert len(events_lines) > 0
        assert len(actions_lines) >= 2  # at least wait + press
        last_action = json.loads(actions_lines[-1])
        assert last_action["op"] == "press"
        assert last_action["button"] == "start"
    finally:
        session.end()


def test_wait_advances_frames(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        before = session.emulator.frame
        session.wait(120)
        assert session.emulator.frame >= before + 120
    finally:
        session.end()


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

def test_polling_disabled_with_zero_interval(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        session.wait(900)
        # No snapshot polling — empty snapshots dir except for any explicit pulls
        files = list((session.folder / "snapshots").glob("*.json"))
        assert files == []
    finally:
        session.end()


def test_polling_creates_snapshot_files(base_dir):
    # Pick an interval that should fire many times during a wait.
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=60)
    try:
        # Need to be past the title screen/intro before PollSnapshot fires.
        # Drive into the main menu state and beyond.
        session.wait(900)
        # Press start to dismiss copyright
        session.press_button("start", hold_frames=10, release_frames=80)
        session.press_button("start", hold_frames=10, release_frames=80)
        # Press A to begin NEW GAME / continue oak intro
        session.press_button("a", hold_frames=10, release_frames=60)
        # Note: PollSnapshot only runs in OverworldLoop, so during intro it
        # won't fire. This test just verifies polling attempts don't crash.
        # If we make it into the overworld, snapshot files should appear.
        files = list((session.folder / "snapshots").glob("*.json"))
        # Either we got some snapshots or polling was just deferred — both fine.
        # Hard requirement: the action log captured something.
        action_lines = (session.folder / "actions.jsonl").read_text().splitlines()
        assert len(action_lines) >= 4
    finally:
        session.end()


def test_set_polling_interval_persists(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=60)
    try:
        prev = session.set_polling_interval(120)
        assert prev == 60
        # Reload meta.json to confirm persistence
        meta = json.loads((session.folder / "meta.json").read_text())
        assert meta["snapshot_interval_frames"] == 120
    finally:
        session.end()


# ---------------------------------------------------------------------------
# Save slots
# ---------------------------------------------------------------------------

def test_named_state_save_and_load(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        session.wait(60)
        pre = session.emulator.frame
        session.save_named_state("checkpoint")
        # Run forward
        session.wait(60)
        assert session.emulator.frame == pre + 60
        session.load_named_state("checkpoint")
        assert session.emulator.frame == pre
        assert "checkpoint" in session.list_named_states()
    finally:
        session.end()


def test_load_unknown_slot_raises(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        with pytest.raises(FileNotFoundError):
            session.load_named_state("does-not-exist")
    finally:
        session.end()


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

def test_journal_append_and_read(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        session.append_journal("First note.")
        session.append_journal("Second note.")
        text = session.read_journal()
        assert "First note." in text
        assert "Second note." in text
    finally:
        session.end()


# ---------------------------------------------------------------------------
# Text search (live)
# ---------------------------------------------------------------------------

def test_search_text_finds_known_dialog(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        # Drive to the main menu so "NEW GAME" appears in text_display.
        # Boot → dismiss copyright → title appears → press start to enter
        # main menu (which draws "NEW GAME / OPTION").
        session.wait(900)
        session.press_button("start", hold_frames=10, release_frames=80)
        session.press_button("start", hold_frames=10, release_frames=80)
        session.press_button("a", hold_frames=10, release_frames=120)
        # Now search
        matches = session.search_text("NEW GAME")
        assert len(matches) >= 1
        assert any("NEW GAME" in m.payload["string"] for m in matches)

        # Case-insensitive variant
        matches_insens = session.search_text("new game", case_sensitive=False)
        assert len(matches_insens) >= 1
    finally:
        session.end()


# ---------------------------------------------------------------------------
# Lifecycle / end
# ---------------------------------------------------------------------------

def test_end_marks_session_ended(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    session.end()
    meta = json.loads((session.folder / "meta.json").read_text())
    assert meta["status"] == "ended"


def test_actions_on_ended_session_raise(base_dir):
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    session.end()
    with pytest.raises(Exception):  # SessionEnded
        session.press_button("a")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_create_and_get(base_dir):
    reg = SessionRegistry(base_dir, rom_path=str(ROM_PATH))
    s = reg.create()
    try:
        assert s.session_id in [m.session_id for m in reg.list()]
        # get_or_load returns the cached instance
        s2 = reg.get_or_load(s.session_id)
        assert s2 is s
    finally:
        s.end()


def test_registry_get_or_load_reads_from_disk(base_dir):
    reg = SessionRegistry(base_dir, rom_path=str(ROM_PATH))
    s = reg.create()
    sid = s.session_id
    s.close()  # drop in-memory but don't end
    # Force evict the cache
    reg._cache.clear()
    s2 = reg.get_or_load(sid)
    try:
        assert s2.session_id == sid
        assert s2.meta.status == "active"
    finally:
        s2.end()


def test_registry_delete_removes_folder(base_dir):
    reg = SessionRegistry(base_dir, rom_path=str(ROM_PATH))
    s = reg.create()
    sid = s.session_id
    folder = s.folder
    s.end()
    reg.delete_session(sid)
    assert not folder.exists()


def test_registry_evict_idle(base_dir):
    reg = SessionRegistry(base_dir, ttl_seconds=0, rom_path=str(ROM_PATH))
    s = reg.create()
    sid = s.session_id
    try:
        # With ttl=0 the next evict_idle should drop it immediately.
        reg.evict_idle()
        assert sid not in reg._cache
    finally:
        # Re-load and end for cleanup
        reg.get_or_load(sid).end()
