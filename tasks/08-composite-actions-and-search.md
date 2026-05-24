# Task 08: Composite Actions + Search

## Context

The minimal harness can already do everything button-by-button. This task adds two comfort features: composite action helpers (so the agent doesn't burn context on 10-button walks) and search routes over the event / text history (so the agent can answer "have I seen this before?" questions without re-traversing context).

These are thin wrappers — they don't change the engine, they're just well-documented compositions of `press_button` calls on the server side.

## Goal

New routes on top of Tasks 06-07:

**Composite actions:**

- `POST /walk` — walk N tiles in a direction, abort on interruption.
- `POST /talk` — press A (face the NPC yourself first via `/walk`).
- `POST /menu/select` — navigate a menu cursor to a target and press A.

**Search:**

- `POST /search/text` — full-text search over `text_display` event history.
- `POST /search/events` — filter event history by structured criteria.

## Inputs

- Server from Tasks 06-07.

## Outputs

- New routes in `harness/server.py` and `server_docs.py`.
- `harness/composites.py` — composite action implementations.
- `harness/search.py` — search backend.
- `harness/tests/test_composites.py`
- `harness/tests/test_search.py`

## Composite actions

### `POST /walk`

Request:

```json
{"direction": "up", "tiles": 3}
```

Implementation: presses the directional button `tiles` times. After each press, check events. If any of the following fire, abort and return: `battle_start`, `npc_interaction_start`, `menu_open`, `map_loaded` (entering a new map mid-walk). Return everything captured plus a status field.

Response:

```json
{
  "completed": true,
  "tiles_traversed": 3,
  "events": [...],
  "abort_reason": null
}
```

On abort:

```json
{
  "completed": false,
  "tiles_traversed": 2,
  "events": [...],
  "abort_reason": "battle_start"
}
```

### `POST /talk`

Press A once. That's it. The "talk to NPC" interaction in Pokemon Red is just pressing A while facing an NPC. The agent positions itself via `/walk` then calls `/talk`.

Request: optional `{"count": 1}` to press A multiple times (advance multi-page dialogue in one call).

Response: `{"events": [...]}`.

### `POST /menu/select`

Request:

```json
{"target": "ITEM"}
```

Or:

```json
{"target_index": 2}
```

Implementation: read the current menu state (via snapshot or by inspecting tilemap region — start with snapshot's `text_box_id` + an additional small RAM read if needed). Determine current cursor position and target position. Press up/down to navigate. Press A to confirm.

If `target` is a string, search visible menu options for an exact (case-insensitive) match. If no match, return `400` with the visible options listed.

Response:

```json
{
  "completed": true,
  "events": [...]
}
```

## Search routes

### `POST /search/text`

Full-text search over `text_display` events in `events.jsonl`.

Request:

```json
{
  "query": "professor",
  "case_sensitive": false,
  "limit": 20
}
```

Response:

```json
{
  "matches": [
    {
      "frame": 142,
      "text": "Hello there! Welcome to the world of POKEMON! My name is OAK! People call me the POKEMON PROF.!"
    },
    ...
  ],
  "count": 3
}
```

### `POST /search/events`

Filter event history by structured criteria.

Request:

```json
{
  "event_ids": ["battle_start", "battle_end"],
  "since_frame": 0,
  "until_frame": 999999,
  "limit": 50
}
```

Or filter by category:

```json
{
  "categories": ["battle"],
  "limit": 50
}
```

Response:

```json
{
  "matches": [
    {"id": "battle_start", "category": "battle", "payload": {...}, "frame": 1247},
    ...
  ],
  "count": 14
}
```

## Implementation notes

**Composites live entirely server-side.** No engine work needed. Each composite is a sequence of `Session.press_button` calls with event inspection between them.

**Search loads `events.jsonl` from disk each call.** For sessions under ~100k events, this is fast enough (tens of ms). Don't add indexing for V1. If performance becomes a problem later, swap in SQLite or a simple in-memory index — but only after profiling shows it matters.

**Menu cursor navigation.** Pokemon Red exposes `wCurrentMenuItem` and `wMaxMenuItem` in RAM. Reading them via `Session.emulator.read_ram` gives the cursor position directly. For the "what options are visible" part, you can either read the tilemap region the menu occupies and decode it (more work, more general) or hard-code the few common menus by their `text_box_id` + RAM positions. Start with the hard-coded approach for the Start menu and battle menu — they're the most common and most useful.

**Don't add `/walk_to(x, y)` pathfinding.** Stay minimal. The agent should explore. If/when pathfinding becomes valuable, it deserves its own task — and probably wants to be a separate module because it needs the static map data extracted from the pret fork.

## Steps

1. Implement `composites.py`:
   - `walk(session, direction, tiles)` — loop pressing direction, check events between presses.
   - `talk(session, count)` — call `press_button("a", count)`.
   - `menu_select(session, target_or_index)` — read cursor, navigate, press A.
2. Wire each composite to a route.
3. Implement `search.py`:
   - `search_text(session, query, case_sensitive, limit)` — load events.jsonl, filter on `text_display` payloads.
   - `search_events(session, filters, limit)` — filter on event_id, category, frame range.
4. Wire search routes.
5. Write hand-crafted docs in `server_docs.py` for each new route.
6. Tests:
   - Walk down a known corridor; verify all tiles traversed.
   - Walk into a wall; verify it stops and reports incomplete.
   - Walk into tall grass; verify it aborts on `battle_start`.
   - Talk to Oak (load a known save state); verify dialogue events come back.
   - Open Start menu, select ITEM; verify menu navigation works.
   - Search for known dialogue substring; verify match.
   - Search for `battle_start`; verify match count.

## Out of scope

- Pathfinding (`/walk_to(x, y)`).
- Pokemon-specific helpers ("/catch", "/heal"). The agent figures these out via observation and button presses.
- Search performance optimization (indexing, SQLite).

## Done when

- `POST /walk {"direction":"up","tiles":5}` traverses 5 tiles when nothing interrupts.
- Walking interrupts cleanly on encounter, NPC interaction, or menu pop.
- `POST /talk` advances dialogue.
- `POST /menu/select` correctly navigates a Start menu cursor.
- `POST /search/text` returns matches for a known dialogue substring.
- `POST /search/events` returns matches for a known event id.
- All tests pass.
