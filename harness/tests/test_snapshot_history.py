"""Tests for /snapshots/history — querying polled snapshots from disk."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harness.server import create_app
from harness.server_docs import SESSION_HEADER


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
    r = client.post("/sessions/start", json={"snapshot_interval_frames": 0})
    return r.json()["session_id"]


def _seed_snapshots(client, session_id, frames_count: int = 5) -> list[int]:
    """Directly write fake snapshot files to the session's folder. This lets us
    test history queries without driving the ROM all the way to the overworld."""
    # Find folder via /sessions/list
    metas = client.post("/sessions/list").json()["sessions"]
    meta = next(m for m in metas if m["session_id"] == session_id)
    folder = Path(meta.get("rom_path")).parent  # not right — let's fix

    # Better: SessionRegistry uses base_dir/session_id. Re-derive from client.
    # Use the FastAPI app's registry directly.
    registry = client.app.state.registry
    folder = registry.base_dir / session_id
    snaps_dir = folder / "snapshots"
    snaps_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i in range(frames_count):
        frame = (i + 1) * 60
        frames.append(frame)
        data = {
            "frame": frame,
            "map_id": 38,
            "x": 5 + i,
            "y": 6,
            "direction": 4,
            "party_count": min(i, 6),
            "money": 3000 + i * 100,
            "badges": 0,
        }
        (snaps_dir / f"{frame:010d}.json").write_text(json.dumps(data))
    return frames


def test_history_returns_all_in_range(client, session_id):
    seeded = _seed_snapshots(client, session_id, frames_count=5)
    r = client.post("/snapshots/history", headers={SESSION_HEADER: session_id},
                    json={"since_frame": 0, "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 5
    returned_frames = [s["frame"] for s in body["snapshots"]]
    assert returned_frames == seeded


def test_history_respects_frame_range(client, session_id):
    seeded = _seed_snapshots(client, session_id, frames_count=5)
    r = client.post("/snapshots/history", headers={SESSION_HEADER: session_id},
                    json={"since_frame": 120, "until_frame": 240})
    assert r.status_code == 200
    body = r.json()
    returned = [s["frame"] for s in body["snapshots"]]
    assert returned == [120, 180, 240]


def test_history_respects_limit(client, session_id):
    _seed_snapshots(client, session_id, frames_count=5)
    r = client.post("/snapshots/history", headers={SESSION_HEADER: session_id},
                    json={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert len(body["snapshots"]) == 2


def test_history_field_projection(client, session_id):
    _seed_snapshots(client, session_id, frames_count=3)
    r = client.post("/snapshots/history", headers={SESSION_HEADER: session_id},
                    json={"fields": ["map_id", "x", "y"]})
    assert r.status_code == 200
    snaps = r.json()["snapshots"]
    assert len(snaps) == 3
    for s in snaps:
        # frame always present; only fields requested are otherwise included
        assert set(s.keys()) <= {"frame", "map_id", "x", "y"}
        assert "money" not in s
        assert "party_count" not in s


def test_history_empty_when_no_snapshots(client, session_id):
    r = client.post("/snapshots/history", headers={SESSION_HEADER: session_id}, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["snapshots"] == []
