"""Tests for the HTTP server (Task 06 routes).

Uses FastAPI's TestClient (which doesn't require uvicorn).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.server import create_app
from harness.server_docs import ROUTE_DOCS, SESSION_HEADER, VERSION


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
    r = client.post("/sessions/start", json={"name": "test-run", "snapshot_interval_frames": 0})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# ---------------------------------------------------------------------------
# Index + docs (no session required)
# ---------------------------------------------------------------------------

def test_index_markdown(client):
    """GET / returns markdown by default."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    body = r.text
    assert "# Pokemon Red AI Harness" in body
    assert VERSION in body
    assert SESSION_HEADER in body
    assert "/sessions/start" in body


def test_index_json_via_format_param(client):
    r = client.get("/?format=json")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == VERSION
    assert "POST /sessions/start" in body["session_lifecycle"]


def test_get_routes_return_markdown(client):
    """GET /<path> returns the route's docs in markdown."""
    for path in ["/press", "/wait", "/state", "/sessions/start"]:
        r = client.get(path)
        assert r.status_code == 200, f"GET {path} failed: {r.text}"
        assert r.headers["content-type"].startswith("text/markdown"), path
        body = r.text
        # Spot-checks
        assert f"`POST {path}`" in body or f"`{path}`" in body
        # Should contain documentation prose from the dict
        assert ROUTE_DOCS[path]["description"].split(".")[0] in body


def test_get_routes_json_via_format_param(client):
    """The structured dict is still reachable via ?format=json for tools."""
    for path in ["/press", "/state"]:
        r = client.get(path + "?format=json")
        assert r.status_code == 200
        body = r.json()
        assert body == ROUTE_DOCS[path]


def test_all_documented_routes_have_get_handler(client):
    # Every entry in ROUTE_DOCS should be reachable via GET, and the
    # response should be non-empty markdown.
    for path in ROUTE_DOCS:
        r = client.get(path)
        assert r.status_code == 200, f"GET {path} -> {r.status_code} {r.text}"
        assert r.text, f"GET {path} returned empty body"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_sessions_start_returns_header_string(client):
    r = client.post("/sessions/start", json={"snapshot_interval_frames": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"]
    assert body["header_to_use"].startswith(f"{SESSION_HEADER}: ")
    assert body["rom_sha1"]


def test_sessions_start_requires_rom_if_no_default(tmp_path):
    app = create_app(base_dir=tmp_path / "sessions", rom_path=None)
    with TestClient(app) as c:
        r = c.post("/sessions/start", json={})
        assert r.status_code == 400
        assert "rom_path" in r.json()["detail"]["error"]


def test_sessions_list(client, session_id):
    r = client.post("/sessions/list")
    assert r.status_code == 200
    sids = [s["session_id"] for s in r.json()["sessions"]]
    assert session_id in sids


def test_sessions_end(client, session_id):
    r = client.post("/sessions/end", headers={SESSION_HEADER: session_id})
    assert r.status_code == 200
    assert r.json() == {"session_id": session_id, "status": "ended"}


def test_sessions_delete(client, session_id):
    r = client.post("/sessions/delete", headers={SESSION_HEADER: session_id})
    assert r.status_code == 200
    assert r.json() == {"deleted": session_id}


# ---------------------------------------------------------------------------
# Error semantics
# ---------------------------------------------------------------------------

def test_missing_session_header_returns_400(client):
    r = client.post("/press", json={"button": "a"})
    assert r.status_code == 400
    assert SESSION_HEADER in r.json()["detail"]["error"]


def test_unknown_session_returns_404(client):
    r = client.post("/press", headers={SESSION_HEADER: "no-such-session"}, json={"button": "a"})
    assert r.status_code == 404
    body = r.json()["detail"]
    assert body["error"] == "no such session"


def test_ended_session_returns_410(client, session_id):
    client.post("/sessions/end", headers={SESSION_HEADER: session_id})
    r = client.post("/press", headers={SESSION_HEADER: session_id}, json={"button": "a"})
    assert r.status_code == 410
    assert "ended" in r.json()["detail"]["error"]


def test_invalid_button_returns_400(client, session_id):
    r = client.post("/press", headers={SESSION_HEADER: session_id}, json={"button": "xyz"})
    assert r.status_code == 400
    body = r.json()["detail"]
    assert body["error"] == "invalid button"
    assert "valid" in body


def test_load_unknown_save_slot_returns_404(client, session_id):
    r = client.post("/load", headers={SESSION_HEADER: session_id}, json={"name": "nope"})
    assert r.status_code == 404
    body = r.json()["detail"]
    assert body["error"] == "no such save slot"
    assert body["name"] == "nope"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_press_returns_frame_and_events(client, session_id):
    r = client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 900})
    assert r.status_code == 200
    r = client.post("/press", headers={SESSION_HEADER: session_id},
                    json={"button": "start", "hold_frames": 10, "release_frames": 80})
    assert r.status_code == 200
    body = r.json()
    assert "events" in body
    assert "frame" in body
    assert body["frame"] >= 900


def test_save_and_load_round_trip(client, session_id):
    client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 120})
    pre = client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 1}).json()["frame"]

    r = client.post("/save", headers={SESSION_HEADER: session_id}, json={"name": "checkpoint"})
    assert r.status_code == 200
    assert r.json()["saved"] == "checkpoint"

    # Advance more frames
    client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 60})

    r = client.post("/load", headers={SESSION_HEADER: session_id}, json={"name": "checkpoint"})
    assert r.status_code == 200
    assert r.json()["frame"] == pre


def test_journal_read_and_append(client, session_id):
    r = client.post("/journal", headers={SESSION_HEADER: session_id},
                    json={"op": "append", "text": "Note 1"})
    assert r.status_code == 200
    body = r.json()
    assert "Note 1" in body["journal"]

    r = client.post("/journal", headers={SESSION_HEADER: session_id}, json={"op": "read"})
    assert r.status_code == 200
    assert "Note 1" in r.json()["journal"]


def test_journal_append_requires_text(client, session_id):
    r = client.post("/journal", headers={SESSION_HEADER: session_id}, json={"op": "append"})
    assert r.status_code == 400


def test_screen_returns_base64_png(client, session_id):
    client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 60})
    r = client.post("/screen", headers={SESSION_HEADER: session_id}, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["width"] == 160
    assert body["height"] == 144
    data = base64.b64decode(body["image_base64"])
    # Must start with PNG signature
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_events_query(client, session_id):
    # Generate some events first
    client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 900})
    client.post("/press", headers={SESSION_HEADER: session_id},
                json={"button": "start", "hold_frames": 10, "release_frames": 80})
    r = client.post("/events", headers={SESSION_HEADER: session_id},
                    json={"categories": ["display"], "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["events"], list)
    assert body["count"] == len(body["events"])
    # All returned must match the category filter
    for ev in body["events"]:
        assert ev["category"] == "display"


def test_wait_validates_frames(client, session_id):
    r = client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": 0})
    assert r.status_code == 400
    r = client.post("/wait", headers={SESSION_HEADER: session_id}, json={"frames": -5})
    assert r.status_code == 400
