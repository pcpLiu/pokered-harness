# Implementation Summary

Status of the three planned tasks plus the snapshot-payload follow-on, with
pointers to the files that were produced or changed.

## Task 01 — Event Schema Audit ✅

**Deliverables:**
- [`harness/events.yaml`](../harness/events.yaml) — 80 events across 6 categories
  (display 5, overworld 20, menu 11, progress 16, battle 21, meta 7) plus
  the snapshot event ID.
- [`harness/events-design-notes.md`](../harness/events-design-notes.md) —
  subsystem map, category counts, decisions made, open questions, gap-check
  vs. the brainstorm taxonomy, and a 5-entry spot-check that every
  `trigger.{file, function}` points to a real symbol in pret/pokered.

**Key decisions captured in design notes:**
- `text_display` payload is the decoded charmap stream, not tile-encoded.
- Compound battle transitions emit several atomic events, not one fat one.
- `move_used`, `damage_dealt`, `pokemon_fainted`, `pokemon_switched`,
  `status_applied` all carry a `side` byte (0=player, 1=enemy) — halves the
  vocabulary without losing information.
- Snapshot is pull-mode (Python writes a WRAM sentinel byte) and is the only
  length-prefixed event.

## Task 02 — Telemetry Scaffolding ✅

**Deliverables (all under [`pokered-fork/`](../pokered-fork/)):**
- `engine/telemetry/event_ids.asm` — 1-byte IDs for all 80 events, plus
  `EVENT_SNAPSHOT = $FF` reserved for the pull channel.
- `engine/telemetry/emit.asm` — `EmitEventByte`, `EmitEvent`,
  `EmitEvent_TextDisplay` (kept in HOME so PlaceString's DE pointer stays
  valid — moving the wrapper to ROMX would bank-swap the string data out
  from under us).
- `engine/telemetry/README.md` — build instructions and PyBoy capture notes.
- `Makefile` — adds `-D LLM_TELEMETRY` when invoked with `LLM_TELEMETRY=1`.
- `includes.asm`, `home.asm`, `main.asm` — one new `INCLUDE` line each.
- `home/text.asm` — three-line `IF DEF(LLM_TELEMETRY)` block at the top
  of `PlaceString` (the only edit to a game-logic file in Task 02).

**Verification:**
- `make` produces a ROM **byte-identical** to upstream pret —
  `make compare` returns OK for pokered.gbc, pokeblue.gbc, pokeblue_debug.gbc,
  and both `.patch` files.
- `make LLM_TELEMETRY=1` produces an instrumented ROM with `EmitEventByte`,
  `EmitEvent`, `EmitEvent_TextDisplay` in HOME bank.
- PyBoy smoke test confirmed `text_display` events fire (event ID `$01`
  followed by charmap bytes, terminator `$50`). The main menu text
  "NEW GAME / OPTION" decoded correctly.

**Notable finding:** PyBoy 2.x's serial stub never captures `rSB` writes
(pins SB to `0xFF`) and never clears `SC_START` after a transfer, so the
authoritative capture point is a hook on `EmitEventByte`'s entry address.
The serial writes are retained for documentation / future real-hardware
compatibility. Documented in the emit.asm header comment.

## Task 03 — All Event Emissions ✅ (with documented deferrals)

**Deliverables:**
- `engine/telemetry/wrappers.asm` — per-event wrapper routines for all 80
  events, living in a new `Telemetry Wrappers` ROMX section (rgblink
  auto-places into bank 1 alongside other engine code).
- `engine/telemetry/CHECKLIST.md` — status per event.
- `ram/wram.asm` — adds `wSnapshotRequest` byte in a new `Telemetry RAM`
  WRAM section.
- ~46 hook insertions across `home/`, `engine/battle/`, `engine/menus/`,
  `engine/events/`, `engine/overworld/`, `engine/items/`, `engine/pokemon/`,
  `engine/movie/`. Each hook is a small `IF DEF(LLM_TELEMETRY)` block
  pushing/popping AF/BC/HL around a `farcall EmitEvent_*`.

**Architecture:**
- Core (`EmitEventByte`, `EmitEvent`, `EmitEvent_TextDisplay`, `PollSnapshot
  trampoline`) lives in HOME bank — reachable from any bank via plain
  `call`.
- All per-event wrappers live in one ROMX section — bank 0 stays under its
  16KB ceiling.
- Hook sites use `farcall EmitEvent_X` with explicit register protection.
- `PollSnapshot` is farcalled every iteration of `OverworldLoop`. It checks
  `wSnapshotRequest`; if zero, returns immediately (no-op fast path);
  otherwise calls `EmitEvent_Snapshot` and clears the flag.

**Verification:**
- Vanilla build still byte-identical to upstream pret (`make compare` OK).
- Instrumented build green, no warnings.
- PyBoy smoke test observed: `title_screen_shown`, `new_game_started`,
  `oak_speech_done`, `text_display` (×24 during intro), `battle_start`
  (when a wild encounter triggered during automated movement).

**Deferred hooks (34 events have wrappers but no hook):**

Categorized by root cause in `pokered-fork/engine/telemetry/CHECKLIST.md`:

- **A. Home-bank space tight (9 events)** — bank 0 has ~17 free bytes after
  current hooks; each home-side hook costs ~14 bytes.
- **B. Need the right sub-label (9 events)** — wrapper exists, function
  exists, but the specific branch within the function isn't labeled.
- **C. Multiple emission sites (5 events)** — event fires from several
  places; need either multiple hooks or an upstream consolidating call.
- **D. Predef-dispatched (4 events)** — goes through pret's predef table;
  hooking inside affects every caller.
- **E. No single natural attachment point (5 events)** — state change is
  inline across many places; best resolved by polling a shadow WRAM byte
  against current value.

These deferrals do not block LLM use of the harness because:
1. `text_display` is hooked, and Pokemon Red writes basically all
   narrative as text. The LLM sees "A critical hit!", "CHARMANDER grew to
   LV. 9!", "You caught PIDGEY!" etc. directly.
2. The snapshot covers durable state (party, inventory, badges, money,
   dex, event flags) — so any state change is observable from snapshot
   diffs even if the corresponding event isn't hooked.

See `pokered-fork/engine/telemetry/CHECKLIST.md` for the full per-event
table and the recommended order to tackle remaining hooks.

## Follow-on — Snapshot payload expansion ✅

**Motivation:** The original 22-byte snapshot only carried map/pos/party
species+levels/badges/money/player_state. That was enough for "where am
I?" but not for "what's my current HP, am I in a battle, what does my
inventory look like?" Combined with the deferred event hooks, the LLM
would have had real gaps in its picture.

**Change:** Expanded `EmitEvent_Snapshot` from 22 to **202 bytes** of
payload (200 in the original expansion, +2 more in Task 09 for menu
cursor). Verified by a PyBoy decode test.

**Payload layout (full table in CHECKLIST.md):**

| Offset | Size | Field |
|--------|------|-------|
| 0-7    | 8    | world: map_id, last_map, x, y, direction, player_state, in_battle, text_box_id |
| 8      | 1    | party_count |
| 9-62   | 54   | party slots × 6 — species, level, hp_cur (2), hp_max (2), status, type1, type2 |
| 63-67  | 5    | active_mon_idx + 4 move ids of player's active mon |
| 68-71  | 4    | money (3 BCD) + badges bitfield |
| 72-112 | 41   | bag_count + 20 × (item_id, quantity) |
| 113-131| 19   | pokedex_owned (151 bits) |
| 132-150| 19   | pokedex_seen (151 bits) |
| 151-190| 40   | event_flags (NUM_EVENTS bits) |
| 191-199| 9    | enemy: species, level, hp (2), max_hp (2), status, type1, type2 |
| 200    | 1    | cursor_index (wCurrentMenuItem) |
| 201    | 1    | max_menu_item (wMaxMenuItem) |

`in_battle` (offset 6) signals when the battle-context bytes (191-199) are
meaningful vs. stale. Likewise, `text_box_id` (offset 7) signals when the
menu cursor bytes (200-201) reflect a currently-open menu.

HP / Max HP fields are big-endian per pret convention.

## Task 09 — Menu cursor visibility ✅

**Motivation:** The agent could tell *that* a menu was open (via
`text_box_id` / `menu_open`) but not *where the cursor was* or *what
option was highlighted*. `wCurrentMenuItem` lived in WRAM, used internally
by `/menu/select`, but never surfaced.

**Engine change:** Renamed the deferred `EVENT_MENU_CURSOR_MOVE` slot
(`$1C`) to `EVENT_MENU_CURSOR` and expanded its payload to a
length-prefixed frame:

  `$1C | length=$13 | cursor_index | max_menu_item | text_box_id | 16 tilemap bytes`

The 16 tilemap bytes start at `wMenuCursorLocation + 1` — one tile to the
right of the cursor arrow — and are raw charmap codes. Python trims
trailing `$7F` (space) tiles to recover `option_text`.

Single hook in `home/window.asm`: at the top of `HandleMenuInput`'s
`.loop1`, right after `call PlaceMenuCursor` (which sets
`wMenuCursorLocation`). The hook is a bare `farcall` (8 bytes, no
register save/restore) because the surrounding code is sandwiched between
two `call` instructions whose neither side relies on A/B/C/HL surviving
across this point — every byte counts in the tight home bank.

The single hook covers all menus that funnel through `HandleMenuInput`,
including:

- Start menu (`engine/menus/draw_start_menu.asm`) — wraps via re-entry.
- Battle menu's FIGHT/ITEM/PARTY/RUN (`engine/battle/core.asm`
  `.handleBattleMenuInput`) — both columns call `HandleMenuInput`.
- List menus / bag / party (`home/list_menu.asm`
  `DisplayListMenuIDLoop`) — scrolls via re-entry.

**Snapshot extension:** Appended `wCurrentMenuItem` and `wMaxMenuItem`
to the end of the snapshot payload (offsets 200-201 of the now-202-byte
payload). `EmitEvent_Snapshot` length-prefix bumped from `$C8` (200) to
`$CA` (202).

**Python harness:** `harness/events.yaml` renames `menu_cursor_move` →
`menu_cursor` with the full payload schema. `harness/telemetry.py` adds a
special-case parser branch alongside snapshot and text_display for
length-prefixed events, decodes the option_text via the existing charmap,
and extends `Snapshot` with `cursor_index` / `max_menu_item`. Tests in
`harness/tests/test_telemetry.py` cover the menu_cursor parser (basic,
trim-trailing-spaces, split-across-feeds, followed-by-other-event) and
the 202-byte snapshot decode. Server docs (`harness/server_docs.py`)
mention the new snapshot fields under `/state` and the menu_cursor event
under `/menu/select`.

**Verification:**
- `make compare` — vanilla build still byte-identical to upstream pret.
- `make LLM_TELEMETRY=1` produces clean instrumented ROMs for the
  primary targets (pokered, pokeblue, pokered_vc, pokeblue_vc).
  pokeblue_debug overflows ROM0 — known pre-existing limitation
  documented in `engine/telemetry/CHECKLIST.md`.
- PyBoy smoke test (`/tmp/test_menu_cursor_smoke.py`): boots to the main
  menu, observes `menu_cursor` with `cursor=0` and `option_text` starting
  "NEW GAME", presses Down → `cursor=1` "OPTION".
- `pytest harness/tests/` → 117 passed (telemetry: 22 tests, including
  4 new menu_cursor tests and the 202-byte snapshot decode).

## Out of scope

Per `tasks/README.md`, none of the following are touched:
- Python harness, event parser, narrative renderer, MCP server
- Agent integration
- Desktop app / visualization
- Benchmark system
- Build / diff CI automation (manual `make` and `make compare` are fine
  while iterating)

## Verification commands

From `pokered-fork/`:

```bash
make             # vanilla build
make compare     # confirms byte-identicality with upstream pret
make LLM_TELEMETRY=1   # instrumented build
```

PyBoy smoke tests (in `/tmp/`, not committed):
- `test_telemetry.py` — confirms `text_display` decoding.
- `test_telemetry_full.py` — counts distinct events observed during intro.
- `test_snapshot.py` — confirms request → emit → clear flag.
- `test_snapshot_v2.py` — decodes all 200 bytes of expanded snapshot.
- `test_snapshot_battle.py` — drives into a battle, confirms `battle_start`
  fires and `in_battle` flag tracks state correctly.

## File-by-file change index (pokered-fork)

**New files:**
- `engine/telemetry/event_ids.asm`
- `engine/telemetry/emit.asm`
- `engine/telemetry/wrappers.asm`
- `engine/telemetry/README.md`
- `engine/telemetry/CHECKLIST.md`

**Modified files (each gains an `IF DEF(LLM_TELEMETRY)` block, vanilla
build byte-identical):**
- `Makefile` — `LLM_TELEMETRY=1` flag handling
- `includes.asm`, `home.asm`, `main.asm` — INCLUDE of telemetry files
- `ram/wram.asm` — `wSnapshotRequest` section
- `home/text.asm` — `PlaceString` hook
- `home/overworld.asm` — `OverworldLoop` snapshot poll + player_moved hook,
  `LoadMapData` hook
- `home/text_script.asm` — `DisplayTextID` hook
- `home/start_menu.asm` — `CloseStartMenu` hook
- `engine/menus/{draw_start_menu, main_menu, save, pokedex, party_menu, pc,
  players_pc, naming_screen, start_sub_menus}.asm` — menu/save hooks
- `engine/events/{pokemart, pokecenter, in_game_trades, give_pokemon,
  black_out, pick_up_item, hidden_items}.asm` — scripted-event hooks
- `engine/pokemon/{add_mon, remove_mon, learn_move, evos_moves}.asm` —
  party / learning / evolution hooks
- `engine/overworld/{ledges, trainer_sight, player_animations}.asm` —
  overworld hooks
- `engine/items/{item_effects, town_map}.asm` — bicycle / surf / ball /
  rope / fly hooks
- `engine/battle/{core, end_of_battle, used_move_text, experience}.asm` —
  battle state-machine hooks
- `engine/movie/{title, hall_of_fame, credits}.asm`,
  `engine/movie/oak_speech/oak_speech.asm` — lifecycle hooks
