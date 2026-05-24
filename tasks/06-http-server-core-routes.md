# Task 06: HTTP Server — Core Routes

## Context

This task wires the Session model behind a FastAPI HTTP server. The server is self-documenting via the GET-as-docs pattern: `GET /<route>` returns its schema + example; `POST /<route>` executes. Session ID rides in the `X-Session-Id` header on all session-scoped routes.

No streaming yet (Task 07). No composite actions yet (Task 08). This is the minimal route set for "agent connects, starts a game, presses buttons, reads state."

## Goal

A FastAPI server (`harness/server.py`) exposing the basic agent loop routes, fully self-documenting and operating on session folders via the `SessionRegistry` from Task 05.

## Inputs

- `harness/sessions.py` and `harness/emulator.py` from Tasks 04-05.
- FastAPI + uvicorn installed.

## Outputs

- `harness/server.py`
- `harness/server_docs.py` — hand-crafted route docs (one function per route)
- `harness/tests/test_server.py`
- `scripts/run_server.py` — entry point

## Routes

**Lifecycle (no `X-Session-Id` required):**

- `GET /` — server index (list of routes, header convention, version)
- `POST /sessions/start` — create a new session
- `POST /sessions/list` — list sessions on disk

**Session-scoped (require `X-Session-Id` header):**

- `POST /sessions/end` — gracefully end this session
- `POST /sessions/delete` — remove this session's folder
- `POST /press` — press button(s), returns events
- `POST /wait` — advance N frames without input, returns events
- `POST /state` — current snapshot (fresh pull)
- `POST /events` — drain unread events from buffer
- `POST /save` — save state to named slot
- `POST /load` — load state from named slot
- `POST /journal` — read or append journal
- `POST /screen` — latest screen as base64 PNG

Every POST route has a paired `GET /<same_path>` that returns docs.

## GET-as-docs format

Each route returns a dict shaped like:

```json
{
  "method": "POST",
  "path": "/press",
  "description": "Press a Game Boy button. The button is held briefly, then released. Game frames advance during both phases. Returns any events that fired during this time.",
  "requires_header": "X-Session-Id",
  "params": {
    "button": {
      "type": "string",
      "required": true,
      "enum": ["a", "b", "up", "down", "left", "right", "start", "select"]
    },
    "count": {"type": "integer", "default": 1, "description": "Number of times to press."},
    "hold_frames":    {"type": "integer", "default": 5},
    "release_frames": {"type": "integer", "default": 5}
  },
  "returns": {
    "events": "list of structured events fired during the press",
    "frame": "current frame number after the press"
  },
  "example": {
    "request":  {"button": "a"},
    "response": {"events": [{"id": "text_display", "payload": {"string": "HELLO!"}, "frame": 1252}], "frame": 1252}
  }
}
```

**Write these docs as hand-crafted dicts in `server_docs.py`** with prose tone for the `description` fields. Do NOT use FastAPI's automatic OpenAPI generator — LLMs respond to written-for-humans descriptions much better than to schema dumps.

## GET / index format

```json
{
  "version": "0.1",
  "session_header": "X-Session-Id",
  "session_lifecycle": [
    "POST /sessions/start",
    "POST /sessions/list",
    "POST /sessions/end",
    "POST /sessions/delete"
  ],
  "routes_requiring_session": [
    "/press", "/wait", "/state", "/events", "/save", "/load", "/journal", "/screen"
  ],
  "notes": "Every route is self-documenting via GET. POST to execute. Include the X-Session-Id header on every call except /sessions/start and /sessions/list."
}
```

## Error semantics

All error responses are JSON objects with at least an `"error"` field.

- Missing `X-Session-Id` on a route that needs it → `400` with `{"error": "missing X-Session-Id header"}`
- Unknown session id → `404` with `{"error": "no such session", "session_id": "..."}`
- Ended session → `410` with `{"error": "session ended", "hint": "POST /sessions/start to create a new one"}`
- Invalid button name → `400` with `{"error": "invalid button", "valid": [...]}`
- Unknown save slot on `/load` → `404` with `{"error": "no such save slot", "name": "..."}`

Clear bodies matter — LLMs read error messages and self-correct, but only if the message is explicit.

## Per-route notes

**`POST /sessions/start`** — Request body: `{"rom_path": "...", "name": "optional-session-name"}`. Response: `{"session_id": "...", "header_to_use": "X-Session-Id: ...", "folder": "...", "rom_sha1": "..."}`. Always include `header_to_use` as a complete string — saves the agent from formatting ambiguity.

**`POST /sessions/list`** — Returns session metadata for every folder under the base sessions directory.

**`POST /press`** — Body: `{"button": "a", "count": 1, "hold_frames": 5, "release_frames": 5}`. Drains telemetry around the presses. Response: `{"events": [...], "frame": ...}`.

**`POST /wait`** — Body: `{"frames": 60}`. Useful for letting animations finish. Response: `{"events": [...], "frame": ...}`.

**`POST /state`** — No body required. Triggers a fresh `wSnapshotRequest`, parses, returns full snapshot.

**`POST /events`** — Body (optional): `{"since_frame": 1247, "categories": ["display"]}`. Returns events from the buffer matching filters.

**`POST /save`** — Body: `{"name": "pre-brock"}`. Writes a `.state` file under the session's `saves/`.

**`POST /load`** — Body: `{"name": "pre-brock"}`. Restores from `.state` file.

**`POST /journal`** — Body: `{"op": "read"}` or `{"op": "append", "text": "..."}`. Returns journal contents.

**`POST /screen`** — No body required. Returns `{"image_base64": "...", "frame": ...}` (PNG-encoded screen).

## Steps

1. Create FastAPI app skeleton in `harness/server.py`.
2. Instantiate global `SessionRegistry` from CLI flags (base dir, ROM path).
3. Implement a dependency that resolves the session from `X-Session-Id` header, returning typed errors.
4. Implement lifecycle routes.
5. Implement session-scoped routes using the session dependency.
6. Define all docs in `server_docs.py` as a `ROUTE_DOCS` dict keyed by path. Register a generic GET handler that returns `ROUTE_DOCS[path]` for each registered POST path.
7. Implement `GET /` index handler.
8. Add structured logging — request, session_id, route, latency.
9. Tests:
   - Happy path: start → press → state → save → load → end
   - All documented error cases
   - GET /press returns the docs as written
   - GET / returns the index

## Out of scope

- SSE event streaming (Task 07).
- Snapshot history routes (Task 07).
- Composite actions (Task 08).
- Search routes (Task 08).
- Auth — localhost only for V1.

## Done when

- `python scripts/run_server.py` starts the server on a configurable port (default 8000).
- `curl http://localhost:8000/` returns the index doc.
- `curl http://localhost:8000/press` returns the press docs.
- `curl -X POST http://localhost:8000/sessions/start -H 'Content-Type: application/json' -d '{"rom_path":"path/to/pokered.gbc"}'` creates a session and returns its id + the `header_to_use` string.
- `curl -X POST http://localhost:8000/press -H 'X-Session-Id: <id>' -H 'Content-Type: application/json' -d '{"button":"start"}'` advances past the title screen and returns events.
- All error responses match the documented shape.
- Tests pass.
