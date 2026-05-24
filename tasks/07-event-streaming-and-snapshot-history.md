# Task 07: Event Streaming + Snapshot History

## Context

Now that the core routes work, add live event streaming via SSE and the snapshot-history endpoints that let an agent query the state-over-time trace that polling has been building.

## Goal

Three new routes on top of the Task 06 server:

- `POST /events/stream` — Server-Sent Events of live events as the game runs.
- `POST /snapshots/history` — query polled snapshots from disk.
- `POST /snapshots/poll` — configure polling interval at runtime.

## Inputs

- Task 06's server.
- Session-side polling already implemented in Task 05.

## Outputs

- New routes added to `harness/server.py` (and `server_docs.py`).
- `harness/tests/test_streaming.py`
- `harness/tests/test_snapshot_history.py`

## Route details

### `POST /events/stream` (SSE)

Opens an SSE stream. Server pushes events as they fire on the session. Connection stays open until the client disconnects or the session ends.

Request body (optional filters):

```json
{
  "since_frame": 1247,
  "categories": ["display", "battle"]
}
```

SSE format:

```
event: text_display
data: {"id":"text_display","payload":{"string":"HELLO!"},"frame":1252}

event: player_moved
data: {"id":"player_moved","payload":{"x":5,"y":7},"frame":1255}
```

**Tempo note:** the emulator only ticks during action POSTs (turn-based). Between actions, no events flow. SSE just stays connected and idle. When the next action POST fires, events appear on both the action's response AND the SSE stream. Clients can dedupe by `frame` if they care; many won't need to.

### `POST /snapshots/history`

Query polled snapshots from disk.

Request body:

```json
{
  "since_frame": 0,
  "until_frame": 9999999,
  "limit": 100,
  "fields": ["map_id", "x", "y", "party_count", "money", "badges"]
}
```

`fields` is optional — when omitted, return full snapshots. When present, project only those fields (saves bandwidth on long queries).

Response:

```json
{
  "count": 42,
  "snapshots": [
    {"frame": 60,  "map_id": 38, "x": 5, "y": 6, "party_count": 0, "money": 3000, "badges": 0},
    {"frame": 120, "map_id": 38, "x": 5, "y": 7, "party_count": 0, "money": 3000, "badges": 0},
    ...
  ]
}
```

Pulls from session's `snapshots/` directory. Files are named by frame, so range queries are filesystem-cheap. Cap at `limit` results.

### `POST /snapshots/poll`

Configure polling interval at runtime.

Request body:

```json
{"interval_frames": 60}
```

`interval_frames: 0` disables polling — explicit `/state` calls still work; just no automatic history accumulation.

Response: `{"interval_frames": 60, "previous": 60}`.

## Implementation notes

**SSE.** Use `sse-starlette` (`pip install sse-starlette`) rather than rolling your own — handles disconnect detection, heartbeats, proper chunking.

**Per-session subscriber model.** Extend `Session` (Task 05) with a small subscription list:

```python
class Session:
    def subscribe_events(self) -> asyncio.Queue: ...
    def unsubscribe_events(self, queue): ...

    def _publish_event(self, event: Event):
        """Called internally after parsing each event."""
        for q in self._subscribers:
            q.put_nowait(event)
```

The SSE handler subscribes, loops on the queue, yields formatted SSE chunks, unsubscribes on disconnect.

**Snapshot history reads from disk.** Each call: list `snapshots/*.json`, filter by frame range, sort, parse top N. For sessions with thousands of snapshots, this is still fast (filenames are pre-sorted lexicographically because of zero-padding). Don't bother with indexing for V1.

**Polling config persistence.** Updating the polling interval should write through to `meta.json` so it survives session restart.

## Steps

1. Add subscription mechanism to `Session` class.
2. Wire `_publish_event` into the event-parse path so every parsed event reaches subscribers.
3. Implement SSE handler — subscribe, loop, format, handle disconnect.
4. Implement `/snapshots/history` reading from disk with field projection.
5. Implement `/snapshots/poll` updating session config and persisting to meta.json.
6. Write docs in `server_docs.py` for each.
7. Tests:
   - SSE: open stream (using `requests` with `stream=True` or `httpx` async), press button in a separate request, observe events on the stream.
   - History: poll for some duration, query with various filters, verify shape + count.
   - Field projection: request a subset, verify only those fields present.
   - Poll config: change interval mid-session, verify next action respects new value.

## Out of scope

- Composite actions (Task 08).
- Search routes (Task 08).
- WebSocket transport — SSE is enough for this use case.

## Done when

- An SSE client connects to `/events/stream` and observes events streaming when actions are POSTed in a separate connection.
- `/snapshots/history` returns shaped results matching the request.
- Field-projected history requests return only requested fields.
- Changing `/snapshots/poll` mid-session takes effect on the next action.
- All tests pass.
