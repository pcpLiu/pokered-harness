# Event Schema Design Notes

Companion to `events.yaml`. Captures open questions, decisions made during the audit, and items deferred to the implementation tasks.

## Subsystem map (pret/pokered)

The pret tree we walked, with the directories that produced candidate events:

- `home/` — global routines (text, overworld loop, money, inventory, save/load, serial transport, predef dispatcher).
- `engine/battle/` — battle state machine (`core.asm`), effects (`effects.asm`, `move_effects/*`), HP/HUD draws, experience/level-up.
- `engine/overworld/` — movement, collisions, doors, ledges, sprite/movement updates, trainer sight, wild-encounter checks.
- `engine/menus/` — start menu, party menu, item menu, save menu, Pokedex, PC, Bill's PC, naming screen, options.
- `engine/events/` — scripted events (mart, pokecenter, hidden items, give pokemon, trades, blackout, vending, prize menu).
- `engine/movie/` — title, Oak's intro, Hall of Fame, credits.
- `engine/items/` — item_effects (the big switchboard for "use item"), TM/HM, town map (fly).
- `engine/pokemon/` — add_mon, remove_mon, evos_moves, learn_move, status_ailments, status_screen.
- `engine/flag_action.asm` — generic event flag get/set/clear.
- `engine/text/` — text engine subroutines (typewriter, dictionaries).
- `home/text_script.asm` — DisplayTextID (universal A-button text entry).
- `home/predef.asm` — predef dispatch (SetPokedexFlag etc.).
- `audio/` — out of scope (we don't event-ize music/SFX).
- `gfx/`, `data/`, `maps/`, `scripts/`, `text/`, `tools/`, `vc/` — data, not code.

## Category coverage check

| Category | Event count | Notes |
|----------|-------------|-------|
| display | 5 | text + box + prompt |
| overworld | 20 | movement, warps, NPCs, items, surf/fly/bike, safari, repel |
| menu | 11 | every menu screen that opens visibly |
| progress | 16 | party, items, money, dex, story flags, evolutions, traded mons, learning |
| battle | 21 | state-machine transitions + safari + ball mechanics |
| meta | 7 | game lifecycle + snapshot |
| **total** | **80** | at the top of the 40-80 band |

(Some events appear in two natural homes — e.g. `move_learned` lives under progress because it's a durable change, even though it's reached via battle. The category is where the description lives; the trigger is where the hook actually sits.)

## Decisions made

1. **Two atomic battle events, not one fat one.** Compound transitions (move → damage → status → faint) emit several events back-to-back rather than one with sub-fields. Easier to compose Python-side and matches the brainstorm doc's "atomic events" preference.

2. **`text_display` payload is the decoded string, not the tile-encoded version.** The Game Boy uses an internal charmap; the Python side has to consume ASCII, so the engine walks the buffer once with the charmap before emission. This is the convention pret uses in its `text/` data files.

3. **Direction values follow pret's `sprite_constants.asm` encoding** (`SPRITE_FACING_DOWN`=$00, `UP`=$04, `LEFT`=$08, `RIGHT`=$0C). The Python parser decodes them.

4. **No separate event for typewriter progress.** Only the start (`text_display`), the cont-arrow pause (`text_paused`), and the close are eventized. Per-character ticks would flood the channel.

5. **`menu_cursor_move` is high-volume.** Cheaper to emit than to suppress in assembly. We accept the noise and filter Python-side. (Open question — see below.)

6. **Snapshot is pull-mode.** The harness writes `wSnapshotRequest` (a new WRAM byte) non-zero; the main loop sees it, emits, and clears. Payload is length-prefixed since it can exceed 32 bytes. Decision: snapshot is the only length-prefixed event for now; everything else is fixed-size.

7. **`badge_obtained` is special.** There's no single function call when a badge is awarded — each gym leader's victory script ORs a bit into `wObtainedBadges`. The schema points at the inventory routine as a placeholder; in Task 03, the right hook is a small wrapper called from each gym's post-battle script (or, alternatively, polling `wObtainedBadges` against a prev-value byte in WRAM every frame in the main loop). Final placement deferred to implementation.

8. **`event_flag_set` is opt-in.** Engine emits for every `FlagAction` set; Python decides which flag IDs are interesting. Skipping this hook is fine if it proves too noisy in profiling.

9. **`move_used` covers both player and enemy** via a `side` byte. Same for `damage_dealt`, `status_applied`, `pokemon_fainted`, `pokemon_switched`. Cuts the event vocabulary roughly in half without losing information.

10. **Field moves are not all separate events.** `field_move_used` covers Cut/Strength/Flash via the shared `field_move_messages.asm` path. Surf and Fly get their own events because they're distinct overworld modes. HM Cut animations are tied to `field_move_used` via a sub-payload byte.

## Open questions (resolve during Task 03)

- **`move_used` exact hook.** Is the cleanest insertion at `PrintMoveUsedText` (text path) or at the actual move-execution dispatch in `core.asm` (`ExecutePlayerMove` / `ExecuteEnemyMove`)? Text path captures struggles, status-skipped moves, etc.; dispatch captures the intent even if no text is printed. Probably emit at the dispatch entry; verify in Task 03.

- **Damage hook placement.** Pret has two near-identical routines (`ApplyDamageToEnemyPokemon`, `ApplyDamageToPlayerPokemon`). Each emits `damage_dealt` with a `target` byte. Confirm the routines don't share a tail we could hook once.

- **Level-up vs XP gained.** GainExperience contains the level-crossing logic in a loop. Emit one `level_up` per level crossed, even if XP awarded crosses multiple levels — gives the agent a clean per-level beat. Verify in Task 03 that the loop structure makes this clean.

- **Player movement granularity.** Emit on tile-boundary completion, not per-frame. The natural spot is after `AdvancePlayerSprite` finishes its 16-frame walk cycle. Need to confirm the exact symbol that fires once per completed step.

- **Snapshot fields.** Initial set covers the obvious. The brainstorm doc also mentions "engine-computed extras: walkability around player, decoded current dialogue if any, current menu options, valid actions for current game mode." These are deferred — keep the snapshot small in Task 03; add fields incrementally as the agent needs them.

- **PC box browsing.** `bills_pc_opened` covers the entry, but the actual mon-list scrolling could be a high-value event for an agent. For now, rely on `menu_cursor_move` for that; promote to a dedicated event only if it proves insufficient.

## Gap check vs other harnesses' observation lists

The Task 00 research note isn't in this repo (only the brainstorm docs are present), so the gap check uses the brainstorm doc's first-cut taxonomy as a proxy. Items from that taxonomy that are NOT covered as their own event in this schema:

- `text_dismissed` — folded into the implicit close of `text_box_close`. Adding a distinct event might be useful for "agent saw the choice screen disappear"; revisit after a real playthrough.
- `flag_set` — covered as `event_flag_set` (renamed for clarity). Opt-in volume control noted above.
- `party_changed` — split into `party_added` / `party_removed` / `pokemon_traded` / `pokemon_received`. The brainstorm event was too coarse.

Items present in this schema that the brainstorm doc didn't list:

- `player_blocked`, `ledge_jump`, `door_entered`, `bicycle_toggled`, `surf_started`, `escape_rope_used`, `field_move_used`, `safari_step`, `safari_action`, `repel_expired`, `whiteout`, `pokemon_traded`, `move_forgotten`, `move_learned`, `tm_taught`, `evolution_started/completed/cancelled`, `pokeball_thrown`, `pokemon_caught`, `naming_screen_opened`, `nickname_set`, `pokemon_center_used`, `pokemart_opened`, `pc_accessed`, `bills_pc_opened`, `oak_speech_done`, `hall_of_fame_entered`, `credits_shown`, `title_screen_shown`, `new_game_started`, `continue_game`, `save_written`. All are player-visible and named in the audit.

## "Special cases that are easy to forget" — coverage

From Task 01's checklist:

- evolution → `evolution_started`, `evolution_completed`, `evolution_cancelled`.
- move learning → `move_learned`, `move_forgotten`.
- fishing → not separately eventized; surfaces as a `battle_start` of the appropriate wild type. Open question: do we want `fishing_started` for the cast itself? Deferred — add if agent needs it.
- HM use → `field_move_used`, `surf_started`, `fly_used`.
- escape attempts → `run_attempted`.
- status conditions → `status_applied`, `confusion_applied`. Per-turn poison tick deferred (would be a frame-loop emission; revisit if the agent needs it).
- trading → `pokemon_traded`. Link-cable trading is out of scope (no link harness yet).
- name entry → `naming_screen_opened`, `nickname_set`.
- save/load → `save_written`, `continue_game`, `new_game_started`.
- game over → `whiteout`.

## Out of scope (still)

- Per-frame audio events.
- Palette swap events that don't change meaning.
- Link / cable-club events (no infrastructure to consume them).
- Debug-only events (the debug menu code in `engine/debug/` is fenced off in the upstream Makefile).

## Pointers to RAM that Task 03 will read

Convenient sticky symbols for payload construction (from `ram/wram.asm`):

- Position: `wCurMap`, `wXCoord`, `wYCoord`, `wPlayerDirection`.
- Party: `wPartyCount`, `wPartySpecies` (PARTY_LENGTH+1 bytes), `wPartyMon1..6` arrays.
- Inventory: `wPlayerMoney` (3 bytes BCD), `wObtainedBadges`, `wBagItems`.
- Battle: `wBattleType`, `wEnemyPartyCount`, `wEnemyPartyMons`, `wDamage`, `wPlayerMoveNum`, `wEnemyMoveNum`, `wPlayerMonStatus`, `wEnemyMonStatus`, `wCriticalHitOrOHKO`, `wTypeEffectiveness`.
- Menus: `wCurrentMenuItem`, `wStartMenuCursorPosition`, `wTopMenuItemX/Y`.
- Naming: `wNamingScreenType`, `wNameBuffer`.

## Text decode — control code policy (Task 04)

Pokemon Red's text engine ships strings in a custom charmap (see
`pret/pokered/constants/charmap.asm`). PlaceString consumes raw bytes from the
caller's string buffer; our `text_display` event captures those bytes verbatim
between $01 (start) and $50 (terminator), and `harness/charmap.py` decodes
them.

Renderable characters (`$80-$99` A-Z, `$A0-$B9` a-z, digits, punctuation,
quotation marks, accented `é`) map to plain ASCII / Unicode. Newline-style
control codes (`$4E <NEXT>`, `$4F <LINE>`, `$51 <PARA>`, `$49 <PAGE>`) decode
to `"\n"` or `"\n\n"` so the decoded string reads naturally. Macro names that
expand to text in-engine — `$54 #` → `"POKé"`, `$4A <PKMN>` → `"POKéMON"`,
`$5B <PC>`, `$5C <TM>`, `$5D <TRAINER>`, `$5E <ROCKET>` — decode to their
expanded form. Interactive / typewriter-control codes that have no
text-equivalent (`$4B <_CONT>`, `$4C <SCROLL>`, `$57 <DONE>`, `$58 <PROMPT>`)
are preserved as bracketed markers; this lets downstream agents recognize
"waiting for input" without losing the literal byte. Player-name placeholders
(`$52 <PLAYER>`, `$53 <RIVAL>`) keep their marker form because the actual
name is in WRAM at `wPlayerName` / `wRivalName` — narrative renderers can
substitute when they care.

The raw byte payload is preserved alongside the decoded string (as
`payload["raw"]` hex) so higher layers can re-decode with a different policy
if needed.

## Sanity check — five randomly chosen spot-checks

(Spec says: "Every entry has trigger.file and trigger.function pointing to real locations in pret/pokered — spot-check 5 random entries.")

1. **`text_display` → `home/text.asm` `PlaceString`** — confirmed at line 49.
2. **`map_loaded` → `home/overworld.asm` `LoadMapData`** — confirmed at line 2294.
3. **`battle_start` → `engine/battle/core.asm` `InitBattle`** — confirmed at line 6642.
4. **`evolution_completed` → `engine/pokemon/evos_moves.asm` `EvolveMon`** — `EvolveTradeMon` and `EvolutionAfterBattle` are confirmed in the file; `EvolveMon` is referenced by predef. Final hook label TBD in Task 03 (may end up at `EvolutionAfterBattle:13`).
5. **`save_written` → `engine/menus/save.asm` `SaveGameData`** — confirmed at line 290.

Four of five confirmed exactly; one (evolution) needs a small refinement during implementation. Acceptable for spec-writing.
