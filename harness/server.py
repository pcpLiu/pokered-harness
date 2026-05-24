"""FastAPI server for the Pokemon Red AI Harness.

Self-documenting design: `GET /<path>` returns the route's docs; `POST /<path>`
executes. Session ID rides in the X-Session-Id header on every session-scoped
call.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from .composites import abortable_walk, menu_select, talk
from . import maps as map_lookup
from .search import search_events, search_text
from .server_docs import INDEX_DOC, ROUTE_DOCS, SESSION_HEADER, VALID_BUTTONS
from .sessions import (
    DEFAULT_POLL_INTERVAL_FRAMES,
    Session,
    SessionEnded,
    SessionRegistry,
)
from .telemetry import Event


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app(
    base_dir: str | Path,
    rom_path: str | Path | None = None,
    ttl_seconds: int = 600,
    window: str = "null",
    use_process: bool = False,
) -> FastAPI:
    app = FastAPI(
        title="Pokemon Red AI Harness",
        version=INDEX_DOC["version"],
        docs_url=None,        # disable Swagger UI in favor of hand-crafted docs
        redoc_url=None,
        openapi_url=None,
    )
    registry = SessionRegistry(
        base_dir=base_dir,
        ttl_seconds=ttl_seconds,
        rom_path=str(rom_path) if rom_path else None,
        window=window,
        use_process=use_process,
    )
    app.state.registry = registry

    _register_lifecycle_routes(app)
    _register_session_routes(app)
    _register_viewer_route(app)
    _register_docs_routes(app)

    # Routes that fire many times a second from the viewer; logging each one
    # buries the genuinely interesting events. Logged at DEBUG instead.
    QUIET_PATHS = {"/screen", "/viewer", "/viewer/stream", "/snapshots/history", "/tick"}

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000
        sid = request.headers.get(SESSION_HEADER, "-")
        level = logging.DEBUG if request.url.path in QUIET_PATHS else logging.INFO
        log.log(
            level,
            "%s %s session=%s status=%s ms=%.1f",
            request.method, request.url.path, sid, response.status_code, elapsed_ms,
        )
        return response

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_session(request: Request, x_session_id: str | None) -> Session:
    if not x_session_id:
        raise HTTPException(status_code=400, detail={"error": f"missing {SESSION_HEADER} header"})
    registry: SessionRegistry = request.app.state.registry
    try:
        session = registry.get_or_load(x_session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail={"error": "no such session", "session_id": x_session_id})
    if session.ended:
        raise HTTPException(
            status_code=410,
            detail={"error": "session ended", "hint": "POST /sessions/start to create a new one"},
        )
    return session


def _events_payload(events: Iterable[Event]) -> list[dict]:
    return [ev.to_dict() for ev in events]


# ---------------------------------------------------------------------------
# Lifecycle routes
# ---------------------------------------------------------------------------

def _register_lifecycle_routes(app: FastAPI) -> None:

    @app.get("/")
    async def index(format: str | None = Query(default=None)):
        if format == "json":
            return JSONResponse(INDEX_DOC)
        return PlainTextResponse(
            _index_to_markdown(INDEX_DOC),
            media_type="text/markdown; charset=utf-8",
        )

    @app.post("/sessions/start")
    async def sessions_start(request: Request) -> dict:
        body = await _read_json(request)
        rom_path = body.get("rom_path") or app.state.registry.default_rom_path
        if not rom_path:
            raise HTTPException(
                status_code=400,
                detail={"error": "rom_path required (server has no default)"},
            )
        name = body.get("name")
        interval = body.get("snapshot_interval_frames", DEFAULT_POLL_INTERVAL_FRAMES)
        try:
            session = app.state.registry.create(
                rom_path=rom_path, name=name, snapshot_interval_frames=interval,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e)})
        return {
            "session_id": session.session_id,
            "header_to_use": f"{SESSION_HEADER}: {session.session_id}",
            "folder": str(session.folder),
            "rom_sha1": session.meta.rom_sha1,
        }

    @app.post("/sessions/list")
    async def sessions_list() -> dict:
        metas = app.state.registry.list()
        return {"sessions": [m.to_dict() for m in metas]}

    @app.post("/sessions/end")
    async def sessions_end(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        session.end()
        return {"session_id": session.session_id, "status": "ended"}

    @app.post("/sessions/delete")
    async def sessions_delete(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        if not x_session_id:
            raise HTTPException(status_code=400, detail={"error": f"missing {SESSION_HEADER} header"})
        registry: SessionRegistry = request.app.state.registry
        # Confirm existence first; delete only after.
        if not (registry.base_dir / x_session_id).exists():
            raise HTTPException(status_code=404, detail={"error": "no such session", "session_id": x_session_id})
        registry.delete_session(x_session_id)
        return {"deleted": x_session_id}


# ---------------------------------------------------------------------------
# Session-scoped routes
# ---------------------------------------------------------------------------

def _register_session_routes(app: FastAPI) -> None:

    # --- Actions ---

    @app.post("/press")
    async def press(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        button = (body.get("button") or "").lower()
        if button not in VALID_BUTTONS:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid button", "valid": VALID_BUTTONS},
            )
        count = int(body.get("count", 1))
        hold = int(body.get("hold_frames", 5))
        release = int(body.get("release_frames", 5))
        # Run the sync PyBoy work off the event loop so SSE subscribers stay live.
        events = await asyncio.to_thread(
            session.press_button, button,
            count=count, hold_frames=hold, release_frames=release,
        )
        return {"events": _events_payload(events), "frame": session.emulator.frame}

    @app.post("/wait")
    async def wait(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        try:
            frames = int(body.get("frames", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"error": "frames must be an integer"})
        if frames <= 0:
            raise HTTPException(status_code=400, detail={"error": "frames must be > 0"})
        events = await asyncio.to_thread(session.wait, frames)
        return {"events": _events_payload(events), "frame": session.emulator.frame}

    @app.post("/tick")
    async def tick(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        """Advance the game without recording an action.

        Used by the browser viewer to animate the game in real time. Unlike
        /wait, this doesn't add a row to actions.jsonl, and current.state is
        only persisted ~once per second to keep disk churn down.

        `target_fps` (default 60) is the wall-clock speed the server paces to —
        pass 30 for half-speed, 120 for 2×, 0 to disable pacing entirely.
        """
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        try:
            frames = int(body.get("frames", 1))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"error": "frames must be an integer"})
        if frames <= 0 or frames > 600:
            raise HTTPException(status_code=400, detail={"error": "frames must be 1..600"})
        try:
            target_fps = float(body.get("target_fps", 60.0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail={"error": "target_fps must be a number"})
        if target_fps < 0 or target_fps > 1000:
            raise HTTPException(status_code=400, detail={"error": "target_fps must be 0..1000"})
        events = await asyncio.to_thread(session.tick_observation, frames, target_fps)
        return {"events": _events_payload(events), "frame": session.emulator.frame}

    @app.post("/state")
    async def state(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        try:
            snap = await asyncio.to_thread(session.snapshot_now)
        except TimeoutError as e:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "snapshot timed out",
                    "hint": "PollSnapshot only runs in OverworldLoop. Try after the player is in the overworld.",
                    "detail": str(e),
                },
            )
        out = snap.to_dict()
        # Annotate map info — readable name + per-map static layout so callers
        # don't have to look up map_id elsewhere.
        m = map_lookup.lookup(snap.map_id)
        if m:
            out["map_name"] = m["display_name"]
            out["map"] = m
        else:
            out["map_name"] = f"map_{snap.map_id}"
        return {"snapshot": out}

    @app.post("/map")
    async def map_info(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        """Return map layout — either the player's current map (with live NPC
        positions read from WRAM) or any map the caller looks up by id or name.

        Body (optional):
          {"map_id": 37}              # lookup by numeric id (static only)
          {"name": "PALLET_TOWN"}     # lookup by pret constant name
          (empty)                     # current map, static + live
        """
        session = _get_session(request, x_session_id)
        body = await _read_json(request)

        # Explicit lookup by id or name — return static data, no live sprites
        # since the player isn't there.
        if "map_id" in body or "name" in body:
            requested_id = body.get("map_id")
            if requested_id is None:
                # name lookup
                requested = (body.get("name") or "").upper()
                # Linear scan; 248 maps, fine.
                from . import maps as _maps_mod
                all_maps = _maps_mod._load()  # cached
                for mid, m in all_maps.items():
                    if m.get("name") == requested:
                        requested_id = mid
                        break
                if requested_id is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"error": "no such map", "name": body.get("name")},
                    )
            try:
                requested_id = int(requested_id)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail={"error": "map_id must be an integer"})
            m = map_lookup.lookup(requested_id)
            if m is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "no such map", "map_id": requested_id,
                            "hint": "valid ids: 0..247"},
                )
            return {"map_id": requested_id, "map": m, "live": False}

        # Current map — include live sprite info.
        def _read():
            cur_map = session.emulator.read_ram(0xD35E, 1)[0]
            live = map_lookup.read_live_sprites(session.emulator)
            return cur_map, live

        cur_map, live = await asyncio.to_thread(_read)
        merged = map_lookup.merge_static_and_live(cur_map, live)
        return {"map_id": cur_map, "map": merged, "live": True}

    @app.post("/events")
    async def events_query(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        since = int(body.get("since_frame", 0))
        until = body.get("until_frame")
        until = int(until) if until is not None else None
        categories = body.get("categories")
        limit = int(body.get("limit", 200))

        results = _load_events_filtered(
            session, since_frame=since, until_frame=until, categories=categories, limit=limit,
        )
        return {"events": [e.to_dict() for e in results], "count": len(results)}

    @app.post("/save")
    async def save(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail={"error": "name required"})
        try:
            path = session.save_named_state(name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e)})
        return {"saved": name, "path": str(path)}

    @app.post("/load")
    async def load(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        name = body.get("name")
        if not name:
            raise HTTPException(status_code=400, detail={"error": "name required"})
        try:
            session.load_named_state(name)
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"error": "no such save slot", "name": name,
                        "available": session.list_named_states()},
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e)})
        return {"loaded": name, "frame": session.emulator.frame}

    @app.post("/journal")
    async def journal(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        op = body.get("op", "read")
        if op == "append":
            text = body.get("text", "")
            if not text:
                raise HTTPException(status_code=400, detail={"error": "text required for op=append"})
            session.append_journal(text)
        elif op == "read":
            pass
        else:
            raise HTTPException(status_code=400, detail={"error": "op must be 'read' or 'append'"})
        return {"journal": session.read_journal()}

    @app.post("/screen")
    async def screen(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        # Use the locked helper so we don't race with /tick or /press.
        img_arr, frame = await asyncio.to_thread(session.read_screen)
        png_bytes = _ndarray_to_png(img_arr)
        return {
            "image_base64": base64.b64encode(png_bytes).decode("ascii"),
            "frame": frame,
            "width": int(img_arr.shape[1]),
            "height": int(img_arr.shape[0]),
        }

    # --- Snapshot history + polling config (Task 07) ---

    @app.post("/snapshots/history")
    async def snapshots_history(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        snapshots = session.get_snapshot_history(
            since_frame=int(body.get("since_frame", 0)),
            until_frame=int(body["until_frame"]) if body.get("until_frame") is not None else None,
            limit=int(body.get("limit", 100)),
            fields=body.get("fields"),
        )
        return {"snapshots": snapshots, "count": len(snapshots)}

    @app.post("/snapshots/poll")
    async def snapshots_poll(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        if "interval_frames" not in body:
            return {"interval_frames": session.meta.snapshot_interval_frames}
        new = int(body["interval_frames"])
        try:
            prev = session.set_polling_interval(new)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e)})
        return {"interval_frames": new, "previous": prev}

    # --- Event streaming (Task 07) ---

    @app.post("/events/stream")
    async def events_stream(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ):
        # Imported lazily to avoid hard dep at import time.
        from sse_starlette.sse import EventSourceResponse

        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        since_frame = int(body.get("since_frame", 0))
        categories = body.get("categories")

        async def event_generator():
            # Replay any matching historical events first.
            for ev in _load_events_filtered(session, since_frame=since_frame, until_frame=None,
                                            categories=categories, limit=10_000):
                yield {"event": ev.id, "data": json.dumps(ev.to_dict())}
            queue = session.subscribe_events()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # Heartbeat (sse-starlette also emits its own)
                        continue
                    if categories and ev.category not in categories:
                        continue
                    yield {"event": ev.id, "data": json.dumps(ev.to_dict())}
            finally:
                session.unsubscribe_events(queue)

        return EventSourceResponse(event_generator())

    # --- Composites (Task 08) ---

    @app.post("/walk")
    async def walk(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        direction = (body.get("direction") or "").lower()
        if direction not in {"up", "down", "left", "right"}:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid direction", "valid": ["up", "down", "left", "right"]},
            )
        tiles = int(body.get("tiles", 1))
        if tiles < 1:
            raise HTTPException(status_code=400, detail={"error": "tiles must be >= 1"})
        result = abortable_walk(session, direction, tiles)
        return result

    @app.post("/talk")
    async def talk_route(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        count = int(body.get("count", 1))
        events = talk(session, count)
        return {"events": _events_payload(events)}

    @app.post("/menu/select")
    async def menu_select_route(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        target = body.get("target")
        target_index = body.get("target_index")
        if target is None and target_index is None:
            raise HTTPException(
                status_code=400,
                detail={"error": "supply either 'target' or 'target_index'"},
            )
        try:
            result = menu_select(session, target=target, target_index=target_index)
        except ValueError as e:
            raise HTTPException(status_code=400, detail={"error": str(e)})
        return result

    # --- Search (Task 08) ---

    @app.post("/search/text")
    async def search_text_route(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        query = body.get("query")
        if not query:
            raise HTTPException(status_code=400, detail={"error": "query required"})
        result = search_text(
            session, query,
            case_sensitive=bool(body.get("case_sensitive", False)),
            limit=int(body.get("limit", 20)),
        )
        return result

    @app.post("/search/events")
    async def search_events_route(
        request: Request,
        x_session_id: str | None = Header(None, alias=SESSION_HEADER),
    ) -> dict:
        session = _get_session(request, x_session_id)
        body = await _read_json(request)
        result = search_events(
            session,
            event_ids=body.get("event_ids"),
            categories=body.get("categories"),
            since_frame=int(body.get("since_frame", 0)),
            until_frame=int(body["until_frame"]) if body.get("until_frame") is not None else None,
            limit=int(body.get("limit", 50)),
        )
        return result


# ---------------------------------------------------------------------------
# Viewer — minimal HTML page that polls /screen and offers button presses
# ---------------------------------------------------------------------------

VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pokemon Red AI Harness — Viewer</title>
  <style>
    :root { color-scheme: dark; }
    body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #eee;
           margin: 0; padding: 1em; display: flex; flex-direction: column; align-items: center; }
    h1 { margin: 0 0 .5em; font-size: 1.1em; font-weight: 500; color: #aaa; }
    #screen-wrap { background: #000; padding: 0.5em; border-radius: 8px;
                   box-shadow: 0 0 20px rgba(255,255,255,0.04); }
    #screen { image-rendering: pixelated; width: 480px; height: 432px; display: block;
              background: #555; }
    .status { font-family: ui-monospace, Menlo, monospace; font-size: 0.85em;
              color: #999; margin: .8em 0; min-height: 1.2em; }
    .controls { display: grid; grid-template-columns: repeat(3, 64px); gap: .4em;
                margin-top: .5em; }
    .controls button { background: #222; color: #eee; border: 1px solid #444;
                       border-radius: 4px; padding: .6em 0; font-size: 1em; cursor: pointer;
                       font-family: inherit; }
    .controls button:hover { background: #333; }
    .controls button:active { background: #444; }
    .controls button.wide { grid-column: span 3; }
    .controls button.action { grid-column: span 1; background: #803030; }
    .controls button.action:hover { background: #a04040; }
    .row { display: flex; gap: .6em; align-items: center; margin-top: .8em; }
    .events { font-family: ui-monospace, Menlo, monospace; font-size: 0.78em;
              max-height: 180px; overflow-y: auto; background: #1a1a1a; padding: .5em;
              border-radius: 4px; width: 480px; box-sizing: border-box; }
    .events div { padding: 2px 0; border-bottom: 1px solid #222; white-space: pre-wrap;
                  word-break: break-word; }
    .events div:last-child { border-bottom: none; }
    input[type=text] { background: #1a1a1a; color: #eee; border: 1px solid #333;
                       padding: .4em; border-radius: 4px; font-family: inherit; width: 200px; }
    label { color: #aaa; font-size: .9em; }
  </style>
</head>
<body>
  <h1>Pokemon Red AI Harness — Viewer</h1>
  <div id="screen-wrap"><img id="screen" alt="game screen"></div>
  <div class="status" id="status">connecting…</div>

  <div class="row">
    <label>session: <input type="text" id="session" value="__DEFAULT_SESSION__"></label>
    <button id="playpause" type="button">⏸ pause</button>
    <label>speed:
      <select id="speed">
        <option value="0.5">0.5×</option>
        <option value="1" selected>1×</option>
        <option value="2">2×</option>
        <option value="4">4×</option>
        <option value="8">8×</option>
      </select>
    </label>
  </div>

  <div class="controls">
    <button></button>
    <button data-button="up">▲</button>
    <button></button>
    <button data-button="left">◀</button>
    <button data-button="select">SEL</button>
    <button data-button="right">▶</button>
    <button class="action" data-button="b">B</button>
    <button data-button="down">▼</button>
    <button class="action" data-button="a">A</button>
    <button class="wide" data-button="start">START</button>
    <button class="wide" data-wait="60">wait 60</button>
    <button class="wide" data-wait="300">wait 300</button>
  </div>

  <div class="row"><label>recent events:</label></div>
  <div class="events" id="events"></div>

<script>
// The screen image is fed by an MJPEG stream (server pushes new frames over a
// single long-lived HTTP connection). No client-side polling of /screen.
// Game animation: the browser drives /tick on a 30 Hz interval while the
// "play" toggle is on, advancing the configured number of frames per call.
const screen = document.getElementById("screen");
const status = document.getElementById("status");
const sessionInput = document.getElementById("session");
const speedSelect = document.getElementById("speed");
const playPauseBtn = document.getElementById("playpause");
const eventsDiv = document.getElementById("events");

let playing = true;
let tickInProgress = false;
let tickTimer = null;

function sessionId() { return sessionInput.value.trim(); }
function headers() { return {"Content-Type": "application/json", "X-Session-Id": sessionId()}; }

function setStream() {
  const sid = encodeURIComponent(sessionId());
  // Stream pushes new PNGs whenever the frame counter changes; 30 Hz cap.
  screen.src = `/viewer/stream?session=${sid}&fps=30&_=${Date.now()}`;
}

function updatePlayPauseLabel() {
  playPauseBtn.textContent = playing ? "⏸ pause" : "▶ play";
}

function setStatus(frame) {
  if (typeof frame === "number") status.textContent = "frame " + frame;
}

function logEvents(evs) {
  if (!evs || !evs.length) return;
  for (const ev of evs) {
    const div = document.createElement("div");
    let label;
    if (ev.id === "text_display") {
      label = "f" + ev.frame + " text: " + JSON.stringify(ev.payload.string);
    } else {
      label = "f" + ev.frame + " " + ev.id + " " + JSON.stringify(ev.payload);
    }
    div.textContent = label;
    eventsDiv.prepend(div);
  }
  while (eventsDiv.children.length > 80) eventsDiv.removeChild(eventsDiv.lastChild);
}

async function pressButton(b) {
  try {
    const r = await fetch("/press", {method: "POST", headers: headers(),
                                     body: JSON.stringify({button: b})});
    const j = await r.json();
    logEvents(j.events);
    setStatus(j.frame);
  } catch (e) { console.error(e); }
}

async function waitFrames(n) {
  try {
    const r = await fetch("/wait", {method: "POST", headers: headers(),
                                    body: JSON.stringify({frames: n})});
    const j = await r.json();
    logEvents(j.events);
    setStatus(j.frame);
  } catch (e) { console.error(e); }
}

async function doTick() {
  if (!playing || tickInProgress) return;
  tickInProgress = true;
  try {
    // We always ask for 4 frames; the server enforces wall-clock pacing
    // based on target_fps, so the actual game speed is determined by
    // the speed selector, not by how often we poll.
    const mult = parseFloat(speedSelect.value);
    const target_fps = 60 * mult;
    const r = await fetch("/tick", {method: "POST", headers: headers(),
                                    body: JSON.stringify({frames: 4, target_fps})});
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      status.textContent = "tick error " + r.status + ": " + (j.detail?.error || "");
      playing = false;
      updatePlayPauseLabel();
      return;
    }
    const j = await r.json();
    logEvents(j.events);
    setStatus(j.frame);
  } catch (e) {
    // Network blip — keep trying.
  } finally {
    tickInProgress = false;
  }
}

function startTickLoop() {
  if (tickTimer) return;
  // We poll as fast as ~60Hz (16ms); the server will block on each call
  // until enough wall-clock time has elapsed to satisfy target_fps. With the
  // tickInProgress guard, only one request is in flight at a time.
  tickTimer = setInterval(doTick, 16);
}
function stopTickLoop() {
  if (!tickTimer) return;
  clearInterval(tickTimer);
  tickTimer = null;
}

playPauseBtn.addEventListener("click", () => {
  playing = !playing;
  updatePlayPauseLabel();
  if (playing) startTickLoop(); else stopTickLoop();
});

document.querySelectorAll("[data-button]").forEach(btn => {
  btn.addEventListener("click", () => pressButton(btn.dataset.button));
});
document.querySelectorAll("[data-wait]").forEach(btn => {
  btn.addEventListener("click", () => waitFrames(parseInt(btn.dataset.wait, 10)));
});

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT") return;
  const map = {ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
               z: "b", x: "a", Enter: "start", Shift: "select"};
  if (e.key === " " || e.code === "Space") {
    // Spacebar toggles play/pause to mirror common video players.
    e.preventDefault();
    playPauseBtn.click();
    return;
  }
  if (map[e.key]) { e.preventDefault(); pressButton(map[e.key]); }
});

sessionInput.addEventListener("change", setStream);
setStream();
updatePlayPauseLabel();
startTickLoop();
status.textContent = "streaming…";
</script>
</body>
</html>
"""


def _register_viewer_route(app: FastAPI) -> None:
    registry: SessionRegistry = app.state.registry

    @app.get("/viewer")
    async def viewer() -> HTMLResponse:
        # If exactly one session exists, default the input to its id;
        # otherwise default to "default".
        sessions = registry.list()
        default_session = sessions[0].session_id if len(sessions) == 1 else "default"
        html = VIEWER_HTML.replace("__DEFAULT_SESSION__", default_session)
        # Aggressive no-cache so changes to the viewer JS are picked up on
        # plain refresh (otherwise the user keeps a stale page across server
        # restarts and wonders why the new behaviour doesn't apply).
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/viewer/stream")
    async def viewer_stream(request: Request, session: str = "default", fps: int = 10):
        """MJPEG (multipart/x-mixed-replace) video stream of the Game Boy screen.

        The browser opens this in an `<img src=...>` tag and keeps the
        connection open — the server pushes a new frame whenever the game's
        frame counter changes. Avoids the per-poll request flood that
        cluttered the access log.
        """
        try:
            sess = registry.get_or_load(session)
        except KeyError:
            raise HTTPException(
                status_code=404,
                detail={"error": "no such session", "session_id": session},
            )

        fps = max(1, min(int(fps), 60))
        sleep_s = 1.0 / fps
        boundary = "frame"

        async def gen():
            last_frame = -1
            while True:
                if await request.is_disconnected():
                    return
                # Locked read so we don't race with /tick or /press.
                arr, cur = await asyncio.to_thread(sess.read_screen)
                if cur != last_frame:
                    png = _ndarray_to_png(arr)
                    last_frame = cur
                    header = (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/png\r\n"
                        f"Content-Length: {len(png)}\r\n\r\n"
                    ).encode()
                    yield header + png + b"\r\n"
                await asyncio.sleep(sleep_s)

        return StreamingResponse(
            gen(),
            media_type=f"multipart/x-mixed-replace; boundary={boundary}",
            # No caching — each chunk is live.
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )


# ---------------------------------------------------------------------------
# Docs routes — one GET per documented POST path
#
# The convention is: GET /<path> returns hand-written markdown documentation
# describing how to use the route; POST /<path> executes the route. Markdown
# is the default since it's more readable to humans and LLMs both. Pass
# `?format=json` for the structured ROUTE_DOCS dict (useful for tooling).
# ---------------------------------------------------------------------------

def _route_doc_to_markdown(doc: dict) -> str:
    """Render a ROUTE_DOCS entry as readable markdown."""
    method = doc.get("method", "POST")
    path = doc.get("path", "")
    lines: list[str] = []
    lines.append(f"# `{method} {path}`")
    lines.append("")
    lines.append(doc.get("description", "").strip())
    lines.append("")
    if doc.get("requires_header"):
        lines.append(f"**Required header:** `{doc['requires_header']}: <session-id>`")
        lines.append("")
    params = doc.get("params") or {}
    if params:
        lines.append("## Request body")
        lines.append("")
        lines.append("JSON object with these fields:")
        lines.append("")
        lines.append("| Field | Type | Required | Default | Description |")
        lines.append("|---|---|---|---|---|")
        for name, spec in params.items():
            ptype = str(spec.get("type", ""))
            required = "yes" if spec.get("required") else "no"
            default = spec.get("default", "")
            if default is None:
                default = "—"
            elif default == "":
                default = "—"
            desc_parts: list[str] = []
            if spec.get("description"):
                desc_parts.append(str(spec["description"]).strip())
            if spec.get("enum"):
                desc_parts.append(f"one of: `{', '.join(spec['enum'])}`")
            lines.append(
                f"| `{name}` | {ptype} | {required} | `{default}` | {' '.join(desc_parts) or '—'} |"
            )
        lines.append("")
    else:
        lines.append("## Request body")
        lines.append("")
        lines.append("_(no parameters — POST with an empty body or `{}`)_")
        lines.append("")
    returns = doc.get("returns") or {}
    if returns:
        lines.append("## Response")
        lines.append("")
        for k, v in returns.items():
            lines.append(f"- `{k}` — {v}")
        lines.append("")
    example = doc.get("example")
    if example:
        lines.append("## Example")
        lines.append("")
        if "request" in example:
            lines.append("Request:")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(example["request"], indent=2, ensure_ascii=False))
            lines.append("```")
            lines.append("")
        if "response" in example:
            lines.append("Response:")
            lines.append("")
            resp = example["response"]
            if isinstance(resp, str):
                lines.append("```")
                lines.append(resp)
                lines.append("```")
            else:
                lines.append("```json")
                lines.append(json.dumps(resp, indent=2, ensure_ascii=False))
                lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _index_to_markdown(index_doc: dict) -> str:
    lines: list[str] = []
    lines.append("# Pokemon Red AI Harness")
    lines.append("")
    lines.append(f"Version `{index_doc.get('version','?')}`")
    lines.append("")
    lines.append((index_doc.get("notes") or "").strip())
    lines.append("")
    lines.append(f"**Session header:** `{index_doc.get('session_header','X-Session-Id')}: <id>`")
    lines.append("")
    lines.append("## Lifecycle routes (no session header)")
    lines.append("")
    for r in index_doc.get("session_lifecycle", []):
        lines.append(f"- `{r}`")
    lines.append("")
    lines.append("## Session-scoped routes")
    lines.append("")
    for r in index_doc.get("routes_requiring_session", []):
        lines.append(f"- `{r}`")
    lines.append("")
    lines.append("Every route is self-documenting: `GET /<path>` returns markdown "
                 "describing how to use it; `POST /<path>` executes. Add "
                 "`?format=json` to a GET for the structured doc dict.")
    lines.append("")
    return "\n".join(lines)


def _register_docs_routes(app: FastAPI) -> None:
    for path, doc in ROUTE_DOCS.items():
        def make_handler(d=doc):
            async def handler(format: str | None = Query(default=None)):
                if format == "json":
                    return JSONResponse(d)
                return PlainTextResponse(
                    _route_doc_to_markdown(d),
                    media_type="text/markdown; charset=utf-8",
                )
            return handler

        op_id = "doc_" + path.replace("/", "_").strip("_")
        app.add_api_route(path, make_handler(), methods=["GET"], name=op_id)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

async def _read_json(request: Request) -> dict:
    """Read JSON body, tolerating empty / non-JSON bodies."""
    try:
        body = await request.body()
        if not body:
            return {}
        return json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail={"error": "invalid JSON body"})


def _load_events_filtered(
    session: Session,
    since_frame: int,
    until_frame: int | None,
    categories: list[str] | None,
    limit: int,
) -> list[Event]:
    """Read events.jsonl and apply filters. Used by both /events and /events/stream replay."""
    path = session.folder / "events.jsonl"
    if not path.exists():
        return []
    until = until_frame if until_frame is not None else float("inf")
    cat_set = set(categories) if categories else None
    results: list[Event] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            frame = obj.get("frame", 0)
            if frame < since_frame or frame > until:
                continue
            if cat_set and obj.get("category") not in cat_set:
                continue
            results.append(Event(
                id=obj["id"],
                category=obj.get("category", ""),
                payload=obj.get("payload", {}),
                frame=frame,
            ))
            if len(results) >= limit:
                break
    return results


def _ndarray_to_png(arr) -> bytes:
    """Encode an (H, W, 3) uint8 RGB ndarray as PNG using stdlib only."""
    import zlib
    import struct

    h, w, _ = arr.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter byte: None
        raw.extend(arr[y].tobytes())
    compressed = zlib.compress(bytes(raw), level=6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit depth, color type 2 (RGB)
    return signature + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
