# Task 09: Menu Cursor Visibility — Event + Snapshot Extension

## Context

Today the agent can tell *that* a menu is open (via the snapshot's `text_box_id` and `menu_open` / `menu_close` events), but not *where the cursor is* or *what option is currently highlighted*. The cursor's position lives in `wCurrentMenuItem`, used internally by `/menu/select`, but never surfaced.

This task closes that gap with two complementary changes:

1. **A new `menu_cursor` event** that fires whenever the highlighted menu option changes — including when a menu first opens. Payload includes the cursor index, the menu's max index, the text box id, and the option text decoded from the tilemap row at the cursor.

2. **Two extra bytes appended to the snapshot** — `cursor_index` and `max_menu_item` — so the snapshot fully describes current menu state and history queries can include cursor info.

The event gives real-time push-based cursor awareness with the highlighted option text. The snapshot gives pullable current-state including for `/state` and `/snapshots/history`. Both are valuable; both should ship together.

## Inputs

- `pokered-fork/` with telemetry plumbing from Tasks 02-03 (build flag, `EmitEvent` machinery, snapshot mechanism).
- `harness/events.yaml`, `harness/telemetry.py`, `harness/server_docs.py` from Tasks 04-08.
- `tasks/IMPLEMENTATION.md` for current snapshot layout (200 bytes today).
- `pokered-fork/engine/telemetry/CHECKLIST.md` — confirms `menu_cursor_move` was deferred (likely category C: multiple emission sites).

## Outputs

**pokered-fork:**

- `engine/telemetry/event_ids.asm` — add `EVENT_MENU_CURSOR` ID.
- `engine/telemetry/wrappers.asm` — new `EmitEvent_MenuCursor` wrapper; extended `EmitEvent_Snapshot` (200 → 202 bytes).
- Hook insertion at the exit of the generic menu input handler (single hook covers most menus).
- Hook insertion(s) at menu-open paths so the event fires with the initial cursor position before any input.
- Additional targeted hooks for menus that bypass the generic handler (battle menu, list-scroll menus if applicable).
- CHECKLIST.md updated — `menu_cursor` (or `menu_cursor_move`) moves from deferred to done.

**harness/ (Python):**

- `events.yaml` — register `menu_cursor` event with payload schema.
- `telemetry.py` — parse the new event (length-prefixed payload, decoded option_text); extend `Snapshot` dataclass with `cursor_index` and `max_menu_item` fields; update snapshot decoder.
- `server_docs.py` — mention `cursor_index` / `max_menu_item` in `/state` returns; include `menu_cursor` example where helpful.
- `harness/tests/` — parser test for `menu_cursor`, snapshot test for the new fields.
- `tasks/IMPLEMENTATION.md` — bump snapshot length, note the event as implemented.

## Steps

### Engine side (assembly)

1. **Find the generic menu input handler.** Grep pret/pokered for `_HandleMenuInput`, `HandleMenuInput`, or `MenuInput`. The canonical handler is in `home/menu.asm` and is what most menus delegate to. Confirm it's the function that updates `wCurrentMenuItem` in response to dpad input.
2. **Identify menus that bypass it.** Likely candidates: the battle menu, the bag list-scroll, the party-menu list. Grep for direct writes to `wCurrentMenuItem` outside the generic handler. Each one needs its own hook.
3. **Allocate `EVENT_MENU_CURSOR`** in `event_ids.asm` (next free ID after the snapshot's `$FF`).
4. **Implement `EmitEvent_MenuCursor`** in `wrappers.asm`:
   - Read `wCurrentMenuItem` → `cursor_index`
   - Read `wMaxMenuItem` → `max_index`
   - Read `hTextBoxID` (or current `wTextBoxID`-equivalent) → `text_box_id`
   - Compute cursor screen row: `wTopMenuItemY + wCurrentMenuItem * 2` (verify spacing for each menu — most use 2-row, some use 1-row; default to `wMenuItemOffset` if pret exposes it)
   - Read ~14 tiles from `wTileMap` at the cursor row, starting one tile right of `wTopMenuItemX` (skip the cursor arrow tile)
   - Trim trailing space tiles (`$7F` in pret's tileset) and any terminator
   - Emit format: `EVENT_MENU_CURSOR`, `payload_length`, `cursor_index`, `max_index`, `text_box_id`, then the trimmed tile bytes (Python side decodes via charmap)
5. **Insert the primary hook** at the exit of the generic menu input handler — after the cursor has been updated, before the function returns. Wrap in `IF DEF(LLM_TELEMETRY)` and use `farcall EmitEvent_MenuCursor` with explicit register protection (push/pop AF, BC, HL — match the convention from other hooks).
6. **Hook menu-open paths.** Easiest approach: at the very first iteration of each menu loop (before any input has been read), emit the event so the initial cursor + option are visible. If the generic handler has an "initial draw" branch, hook there; otherwise, hook each menu's "open" entry point.
7. **Hook bypass menus.** For each menu identified in step 2, add a hook at the function that updates its cursor.
8. **Extend `EmitEvent_Snapshot`.** At the end of the current 200-byte payload, write two more bytes: `wCurrentMenuItem` then `wMaxMenuItem`. Update any `SNAPSHOT_LEN` / `SNAPSHOT_PAYLOAD_LEN` constant from 200 to 202. The payload's length-prefix at the front needs to reflect 202 as well.
9. **Build verification:**
   - `make` — vanilla build must remain byte-identical to upstream pret (`make compare`).
   - `make LLM_TELEMETRY=1` — instrumented build green, no warnings.
10. **Manual in-PyBoy verification:**
    - Open Start menu → observe `menu_cursor` event with `cursor=0` and `option_text` matching the first option.
    - Press Down → observe `menu_cursor` event with `cursor=1` and updated `option_text`.
    - Open bag → observe cursor events for bag navigation including scroll.
    - Trigger a wild battle → observe cursor events on the FIGHT/ITEM/RUN menu.
    - In any menu, request `/state` → confirm `cursor_index` and `max_menu_item` are present and correct.

### Harness side (Python)

11. **Add `menu_cursor` to `events.yaml`** with payload schema (`cursor_index: byte`, `max_index: byte`, `text_box_id: byte`, `option_text: text`). Category: `menu`. Include a narrative template.
12. **Update `harness/telemetry.py`:**
    - Add parser for `menu_cursor`. Format mirrors `text_display`: length-prefixed payload, fixed bytes followed by charmap text.
    - Extend `Snapshot` dataclass with `cursor_index: int`, `max_menu_item: int`.
    - Update `parse_snapshot()` to consume bytes 200 and 201 from the payload.
    - Update any snapshot length constant to 202.
13. **Update `harness/server_docs.py`:**
    - In `/state` docs, mention the new fields in the returns description.
    - In the index doc or per-route mentions where useful, note `menu_cursor` as a new event category that closes the menu-cursor gap.
14. **Tests:**
    - `tests/test_telemetry.py`: feed a synthetic `menu_cursor` byte sequence with known cursor/text; assert decoded fields match.
    - Snapshot test: feed a synthetic 202-byte payload; assert `cursor_index` and `max_menu_item` decoded correctly.
    - Backwards-compat: if there's a fixture with the old 200-byte snapshot, decide whether to keep it as a legacy fixture or regenerate it.
15. **Update `tasks/IMPLEMENTATION.md`** — snapshot is now 202 bytes; `menu_cursor` event implemented; cross off the corresponding deferred entry in CHECKLIST.md.

## Verification scenarios

These should all produce the expected observations after the change:

| Action | Expected observation |
|---|---|
| Open Start menu | `menu_cursor` event: `cursor=0`, `option_text` contains first option |
| Press Down in Start menu | `menu_cursor` event: `cursor=1`, `option_text` changes |
| Open Bag and scroll | One `menu_cursor` event per scroll step, with item names |
| Enter battle, hover FIGHT | `menu_cursor` event with FIGHT-related text |
| `POST /state` while inside any menu | Snapshot includes correct `cursor_index` and `max_menu_item` |
| `POST /snapshots/history` projecting `["frame", "cursor_index"]` | Returns cursor over time |

## Out of scope

- Hooking *every* cursor change in niche custom menus that don't go through any standard handler. If a particular menu doesn't emit, document it in CHECKLIST.md — don't chase every corner.
- Exposing the *full list* of visible menu options as one event. The agent can either piece them together from successive `menu_cursor` events (one per cursor stop) or rely on the burst of `text_display` events that fire at menu-open time.
- Adding a dedicated `POST /menu/state` route. The snapshot extension covers the pull case.
- Refactoring `/menu/select` to use the new snapshot fields instead of its internal RAM read. Worth doing eventually for consistency, but tracks as a small follow-up cleanup.

## Done when

- `make LLM_TELEMETRY=1` produces a ROM that emits `menu_cursor` events when navigating Start menu, bag, and battle menu.
- Vanilla `make` build remains byte-identical to upstream pret.
- `POST /state` returns a snapshot that includes `cursor_index` and `max_menu_item` matching the in-game cursor.
- `POST /snapshots/history` with a `fields` projection that includes the new fields works.
- All existing tests pass; new tests for the `menu_cursor` event and snapshot extension pass.
- Manual scenario in the harness: opening the Start menu and pressing Down twice produces a sequence of `menu_cursor` events with `cursor=0`, `cursor=1`, `cursor=2` and corresponding option text.
- `IMPLEMENTATION.md` updated to reflect the new snapshot length and the now-implemented event.
