# Task 03: pret Fork — All Event Emissions

## Context

Scaffolding from Task 02 proved `text_display` works end-to-end. Now implement emissions for every other event in `events.yaml`. This is the bulk of the assembly work, but each individual emission is small and follows the same pattern.

## Goal

All events in `events.yaml` emit correctly from the instrumented build. Each insertion is minimally invasive: one `call EmitEvent_*` per emission site, no edits to game logic.

## Inputs

- `harness/events.yaml`.
- `pokered-fork` scaffolding from Task 02.

## Outputs

- Per-event wrapper routines in `engine/telemetry/emit.asm` (split into per-category files if it grows: `emit_battle.asm`, `emit_overworld.asm`, etc.).
- Single-line emission calls inserted at the trigger points specified in `events.yaml`.
- `engine/telemetry/CHECKLIST.md` tracking implementation status per event.

## Steps

1. Group events by category. Implement category by category, in this order: `display` → `overworld` → `menu` → `progress` → `battle` → `meta`. Battle is last because its state machine is the most complex and benefits from the simpler categories being shaken out first.
2. For each event:
   - Write a wrapper routine `EmitEvent_<EventName>` that builds the payload from current game state. Most payloads are small (a few bytes).
   - Insert `call EmitEvent_<EventName>` at the trigger location specified in events.yaml.
   - Build instrumented ROM. Trigger the event in-game. Verify serial output contains the expected bytes.
   - Tick the checklist.
3. Watch for **timing-sensitive code paths**. Avoid inserting hooks inside per-frame inner loops or sound-handling code. Prefer the entry or exit of high-level routines — pret's labeling (e.g., `PlaceString`, `LoadMapData`, `StartBattle`, `DrawHUD`) makes this easy.
4. **Snapshot mechanism (small addition).** Add code to the main loop that watches a specific WRAM byte (call it `wSnapshotRequest`). When set non-zero, emit a `snapshot` event containing: player coords, current map id, party count, party species/levels, badges bitfield, money. After emission, clear `wSnapshotRequest`. Define the snapshot event id in `event_ids.asm`.
5. Update `CHECKLIST.md` as you go. Each event line should say `[x]` only after manual in-game verification.

## Verification approach

For each event, you need a way to trigger it in-game:

- `text_display`: any dialogue
- `player_moved`: walking on the overworld
- `menu_open` / `menu_close`: pressing Start
- `battle_start`: walking into grass
- `move_used`, `damage_dealt`, `pokemon_fainted`: completing a battle
- `level_up`: winning a battle that crosses an XP threshold
- `badge_obtained`: defeating Brock (use a save state)
- `evolution`, `move_learning`: use save states near triggers

Maintain a directory `pokered-fork/test-states/` with named save states pre-positioned near each hard-to-reach event.

## Implementation notes

- Compound events (e.g., level-up triggers stat increases, potential move learning, potential evolution) should be emitted as several atomic events rather than one fat event. Finer-grained is easier to compose later.
- Payload size budget: keep individual events under 32 bytes when possible. Larger payloads slow down serial transmission noticeably (though it's still emulator-instant).
- If a payload doesn't fit in 256 bytes, frame it as `<event_id><length_low><length_high><bytes...>`. Most events won't need this.

## Out of scope

- Python-side parsing (Task 06).
- Narrative rendering (Task 08).
- Building automation (Task 04).

## Done when

- Every event in events.yaml has a corresponding emission in the instrumented ROM.
- Playing through Pallet Town → first battle → Pewter City reaches **all** of the following events at least once: text_display, player_moved, menu_open, menu_close, npc_interaction_start, battle_start, move_used, damage_dealt, pokemon_fainted, battle_end, xp_gained, map_loaded, badge_obtained.
- `CHECKLIST.md` is fully checked off.
- Snapshot request mechanism works: writing a non-zero byte to `wSnapshotRequest` produces a `snapshot` event with all expected fields within ~1 frame.
