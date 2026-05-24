# Engine Telemetry Architecture — How the Harness Gets State

Date: 2026-05-19

Companion to `initial-design-discussion.md`. That doc covered product shape; this one covers the technical architecture for getting game state out of Pokemon Red and into the LLM.

## The core insight

The engine already has a clear separation between "compute game state" and "render game state for the player." The graphics pipeline is one renderer of that state. We can add a **second renderer in parallel** that targets text/events instead of pixels, sharing the same source.

```
                              ┌─→ graphics pipeline → pixels on screen
engine state / events ────────┤
                              └─→ telemetry pipeline → structured events to LLM channel
```

This is a better foundation than ad-hoc per-function hooks because:

- **Completeness by construction.** Whatever the engine decides to show the player, the LLM gets the equivalent. The same code path that draws "Charmander used Tackle!" emits `event: move_used, user: Charmander, move: TACKLE`.
- **Channels stay in sync.** Branching both outputs from the same source means screen and LLM observation can't diverge. No risk of missing transient state.
- **Self-narrating engine.** The Python harness becomes a thin pipe translating the engine's narration into LLM prompts. Much less reimplementation of game logic externally.

## Develop in a fork, ship as runtime patches

The mechanics:

1. **Fork pret/pokered** as the development environment. All telemetry code lives under `engine/telemetry/` so it's isolated and easy to follow.
2. **Write hooks in assembly** against pret's symbols, with full context. RGBDS assembles.
3. **Wrap everything in a build flag**: `IF DEF(LLM_TELEMETRY)`. The fork produces two builds — vanilla (byte-identical to upstream pret) and instrumented. Diff between them = your patch set.
4. **Ship the binary diff, not the fork.** A small table of `(offset, bytes_to_overwrite)` entries embedded in the harness. At runtime, PyBoy loads the user's vanilla ROM, harness writes the diff into emulator memory, instrumented game runs.
5. **Automate diff extraction in CI.** GitHub Actions builds both ROMs, computes the diff, updates the patches file. Otherwise you'll ship stale patches.

User never needs RGBDS, never sees pret, never modifies their ROM file on disk. They install the harness; it does the rest.

The fork can stay public as the source of truth for what the patches do — same legal posture as pret itself, which is the same gray zone that has held stable for years.

## Two complementary channels

**Push: event channel.** Engine emits discrete events on state changes. Sparse, well-suited to "what just happened since I last looked."

**Pull: snapshot on demand.** Harness writes a sentinel byte to a known register; engine's main loop sees the request and produces a full state dump. Better for "describe the world right now" than reconstructing from event history.

Build both. Events give you a clean play-by-play; snapshots give you ground truth on demand.

## Transport

Use the Game Boy serial I/O port (`$FF01` / `$FF02`). It's designed for byte-at-a-time output, PyBoy intercepts it cleanly, and it's the conventional telemetry channel in instrumented ROMs. Alternative is a ring buffer in WRAM that Python polls each frame — works, but serial is more idiomatic and matches the "channel" mental model better.

For snapshots (which are larger), still serial — just multiple bytes framed as a length-prefixed message.

## Event vocabulary

This is the place to spend design effort. The discipline that keeps it sane:

> If it doesn't cause a pixel to change on screen, you probably don't need an event for it.

This keeps the event stream roughly synchronized with player perception, which is what you want for an LLM agent.

First-cut event taxonomy (~30-50 types total):

**Display**
- `text_display` (payload: full string)
- `text_dismissed`
- `menu_open` (which menu)
- `menu_close`
- `menu_cursor_move` (new index)

**Overworld**
- `map_loaded` (map id)
- `player_moved` (new coords, direction)
- `player_turned` (direction)
- `npc_interaction_start` (npc id, script id)
- `item_picked_up` (item id)
- `door_entered`

**Battle**
- `battle_start` (trainer id or wild species)
- `battle_end` (outcome)
- `move_used` (user, move id)
- `damage_dealt` (target, amount)
- `status_applied` (target, status)
- `pokemon_fainted` (which side)
- `pokemon_caught` (species)
- `xp_gained` (amount)
- `level_up` (species, new level)

**State changes**
- `badge_obtained` (badge id)
- `flag_set` (event flag id) — for story progression
- `party_changed` (slot, new species)
- `money_changed` (new amount)

Each event is a few bytes: event ID + small payload. Decoding happens on the Python side.

## Attachment points in pret

Natural emission sites, by pret directory:

- `home/text.asm`, `engine/text/` — `PlaceString` and typewriter routines → text events
- `home.asm` MainLoop / `engine/overworld/` — movement, turn, map load
- `engine/battle/` — battle state machine transitions, move resolution, damage calc
- `engine/menus/` — menu open/close, cursor changes
- `engine/items/` — pickup events

One new file `engine/telemetry/emit.asm` defines:

```
EmitEvent: ; takes event id in A, payload in HL
    push af / bc / de / hl
    ; write event id byte to serial
    ; write payload bytes to serial
    pop hl / de / bc / af
    ret
```

Plus per-event-type wrappers (`EmitEvent_TextDisplay`, `EmitEvent_PlayerMoved`, etc.) that pack their specific payload format.

Each emission site adds one `call EmitEvent_*` line. The actual game logic is untouched.

## What this unlocks beyond LLM

Once the telemetry channel exists, it's useful for:

- Replay logging (record event stream → replay agent runs)
- Gameplay analytics (heatmaps, time-spent-per-map, failure-mode analysis)
- Validation in CI (deterministic event sequence for known input traces)
- Training data for fine-tuning agents
- Behavioral dashboards (the desktop app can subscribe to the same channel for visualization)

Worth naming the system accordingly — `ENGINE_TELEMETRY` is more accurate than `LLM_PIPELINE`. The LLM is one consumer of telemetry, not the only one.

## Pure-RAM extraction is still fine for static fields

Not everything needs an event. Player coords, party composition, money, badge bitfield, inventory — these are stable values sitting in known RAM locations. Read them directly when building a snapshot. The event channel is for *changes* and *narrative* — what just happened. The snapshot is for *state* — what is true right now.

Rough split:
- **Events (engine pushes):** dialogue, movement, battle actions, menu changes, item pickups, flag changes.
- **Snapshot (Python reads RAM, optionally augmented by engine-computed extras):** player coords, party stats, inventory, badges, current map id, money.
- **Snapshot extras the engine computes on request:** walkability around player, decoded current dialogue if any, current menu options, valid actions for current game mode.

## Build sequencing

1. Fork pret. Add `engine/telemetry/emit.asm` with a working `EmitEvent` routine writing to serial. Build with `LLM_TELEMETRY` flag. Confirm vanilla build is unchanged.
2. Add a single emission site — `text_display` is the highest-value first hook. Run instrumented ROM in PyBoy, confirm Python sees the byte stream.
3. Build the diff-extraction CI pipeline. Now you can ship without distributing the fork.
4. Add events incrementally, prioritized by what the agent struggles to perceive otherwise.
5. Implement snapshot-on-demand pathway (sentinel byte triggers a main-loop branch that walks state and emits).
6. Document the event schema. This is the harness's effective API contract.
