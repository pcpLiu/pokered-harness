"""Tests for /search/text and /search/events.

We seed events.jsonl directly with known content so the search assertions are
deterministic and don't depend on driving the ROM to a specific scene.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.search import search_text, search_events
from harness.server import create_app
from harness.server_docs import SESSION_HEADER
from harness.sessions import Session


ROM_PATH = Path(__file__).resolve().parents[2] / "pokered-fork" / "pokered.gbc"
ROM_AVAILABLE = ROM_PATH.exists()

pytestmark = pytest.mark.skipif(
    not ROM_AVAILABLE,
    reason=f"instrumented ROM not built: {ROM_PATH}.",
)


@pytest.fixture
def client(tmp_path):
    app = create_app(base_dir=tmp_path / "sessions", rom_path=str(ROM_PATH))
    with TestClient(app) as c:
        yield c


@pytest.fixture
def session_id(client):
    r = client.post("/sessions/start", json={"name": "search-test", "snapshot_interval_frames": 0})
    return r.json()["session_id"]


def _seed_events(client, session_id, events: list[dict]) -> None:
    registry = client.app.state.registry
    folder = registry.base_dir / session_id
    path = folder / "events.jsonl"
    with path.open("a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


# ---------------------------------------------------------------------------
# /search/text
# ---------------------------------------------------------------------------

def test_search_text_finds_seeded_dialogue(client, session_id):
    _seed_events(client, session_id, [
        {"id": "text_display", "category": "display",
         "payload": {"string": "Hello there! Welcome to the world of POKEMON!"}, "frame": 100},
        {"id": "text_display", "category": "display",
         "payload": {"string": "My name is OAK. People call me the POKEMON PROF."}, "frame": 200},
        {"id": "player_moved", "category": "overworld",
         "payload": {"x": 5}, "frame": 150},
    ])
    r = client.post("/search/text", headers={SESSION_HEADER: session_id},
                    json={"query": "OAK"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["matches"][0]["frame"] == 200


def test_search_text_case_insensitive_by_default(client, session_id):
    _seed_events(client, session_id, [
        {"id": "text_display", "category": "display",
         "payload": {"string": "PROFESSOR OAK"}, "frame": 100},
    ])
    r = client.post("/search/text", headers={SESSION_HEADER: session_id},
                    json={"query": "professor"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_search_text_respects_case_sensitive(client, session_id):
    _seed_events(client, session_id, [
        {"id": "text_display", "category": "display",
         "payload": {"string": "PROFESSOR OAK"}, "frame": 100},
    ])
    r = client.post("/search/text", headers={SESSION_HEADER: session_id},
                    json={"query": "professor", "case_sensitive": True})
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_search_text_limit(client, session_id):
    _seed_events(client, session_id, [
        {"id": "text_display", "category": "display",
         "payload": {"string": f"match {i}"}, "frame": i}
        for i in range(10)
    ])
    r = client.post("/search/text", headers={SESSION_HEADER: session_id},
                    json={"query": "match", "limit": 3})
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_search_text_requires_query(client, session_id):
    r = client.post("/search/text", headers={SESSION_HEADER: session_id}, json={})
    assert r.status_code == 400


def test_search_text_unit(tmp_path):
    base_dir = tmp_path / "sessions"
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        # Seed via the session's helper
        events_path = session.folder / "events.jsonl"
        with events_path.open("a") as f:
            f.write(json.dumps({"id": "text_display", "category": "display",
                                "payload": {"string": "Hello world"}, "frame": 1}) + "\n")
        result = search_text(session, "hello")
        assert result["count"] == 1
    finally:
        session.end()


# ---------------------------------------------------------------------------
# /search/events
# ---------------------------------------------------------------------------

def test_search_events_filter_by_id(client, session_id):
    _seed_events(client, session_id, [
        {"id": "battle_start", "category": "battle", "payload": {}, "frame": 100},
        {"id": "battle_end", "category": "battle", "payload": {"result": 0}, "frame": 200},
        {"id": "battle_start", "category": "battle", "payload": {}, "frame": 300},
        {"id": "text_display", "category": "display",
         "payload": {"string": "hi"}, "frame": 150},
    ])
    r = client.post("/search/events", headers={SESSION_HEADER: session_id},
                    json={"event_ids": ["battle_start"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    for m in body["matches"]:
        assert m["id"] == "battle_start"


def test_search_events_filter_by_category(client, session_id):
    _seed_events(client, session_id, [
        {"id": "battle_start", "category": "battle", "payload": {}, "frame": 100},
        {"id": "text_display", "category": "display",
         "payload": {"string": "hi"}, "frame": 200},
        {"id": "player_moved", "category": "overworld",
         "payload": {}, "frame": 300},
    ])
    r = client.post("/search/events", headers={SESSION_HEADER: session_id},
                    json={"categories": ["battle", "display"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2


def test_search_events_filter_by_frame_range(client, session_id):
    _seed_events(client, session_id, [
        {"id": "x", "category": "meta", "payload": {}, "frame": 50},
        {"id": "x", "category": "meta", "payload": {}, "frame": 150},
        {"id": "x", "category": "meta", "payload": {}, "frame": 250},
        {"id": "x", "category": "meta", "payload": {}, "frame": 350},
    ])
    r = client.post("/search/events", headers={SESSION_HEADER: session_id},
                    json={"since_frame": 100, "until_frame": 300})
    assert r.status_code == 200
    body = r.json()
    frames = [m["frame"] for m in body["matches"]]
    assert frames == [150, 250]


def test_search_events_limit(client, session_id):
    _seed_events(client, session_id, [
        {"id": "x", "category": "meta", "payload": {}, "frame": i}
        for i in range(10)
    ])
    r = client.post("/search/events", headers={SESSION_HEADER: session_id},
                    json={"limit": 4})
    assert r.status_code == 200
    assert r.json()["count"] == 4


def test_search_events_unit(tmp_path):
    base_dir = tmp_path / "sessions"
    session = Session.create(base_dir, ROM_PATH, snapshot_interval_frames=0)
    try:
        events_path = session.folder / "events.jsonl"
        with events_path.open("a") as f:
            f.write(json.dumps({"id": "battle_start", "category": "battle",
                                "payload": {}, "frame": 100}) + "\n")
        result = search_events(session, event_ids=["battle_start"])
        assert result["count"] == 1
    finally:
        session.end()
