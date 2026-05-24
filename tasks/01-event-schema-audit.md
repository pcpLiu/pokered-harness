# Task 01: Event Schema Audit

## Context

The harness's central artifact is `events.yaml` — the full catalog of events the instrumented engine emits. Every assembly hook we write later references an event ID from this schema. The Python event parser uses it. The narrative renderer uses it. **This is the API contract for the entire harness.**

## Goal

Produce `harness/events.yaml`: a structured catalog of all player-observable game events in Pokemon Red, with trigger points in pret/pokered source, payload schemas, and category groupings. Target **40-80 event types**.

## Inputs

- pret/pokered source. Clone fresh: `git clone https://github.com/pret/pokered.git pokered-upstream`.
- Insights from Task 00's research note (especially observation lists from existing harnesses — they are a useful gap-check).

## Definition of an "event"

Something that:

- Causes a visible change on the player's screen, OR
- Advances the game's narrative or progression state.

NOT an event:

- Per-frame timer ticks.
- Sound playback (unless tied to a discrete narrative beat like a Pokemon cry).
- Internal flag updates that don't manifest visibly.
- Palette swaps that don't change meaning.

## Outputs

- `harness/events.yaml` — the schema.
- `harness/events-design-notes.md` — open questions, decisions made, things to revisit during implementation.

## Schema format

```yaml
- id: text_display
  category: display          # display | overworld | battle | menu | progress | meta
  trigger:
    file: home/text.asm
    function: PlaceString
  payload:
    string: text             # decoded dialogue string
  narrative_template: "Words appeared: '{string}'"
  notes: "Fires once per text box before typewriter animation"
```

Required fields: `id`, `category`, `trigger.file`, `trigger.function`.
Encouraged fields: `payload`, `narrative_template`, `notes`.

## Steps

1. **Subsystem map.** List engine subsystems by directory plus major include files. Draft outline into `events-design-notes.md`. Expect 15-25 subsystems (text, overworld movement, battle state machine, menus, items, scripting, save/load, intro/title, etc.).
2. **Per-subsystem candidate scan.** For each subsystem, grep for state-change function names: `Print`, `Place`, `Draw`, `Display`, `Show`, `Load`, `Start`, `End`, `Init`, `Update`. List candidate functions per subsystem.
3. **Define each event.** For each candidate, fill in the schema entry. Follow cross-references to flag-setting code when defining payloads (e.g., `battle_start` payload should distinguish wild vs trainer vs safari).
4. **Consolidation pass.** Merge duplicates, group by category, target the 40-80 range.
5. **Gap-check against existing harnesses' observation lists (from Task 00).** If they extract a field we don't have an event for, decide whether to add one.
6. **Special cases that are easy to forget:** evolution, move learning, fishing, HM use (cut, surf, fly, strength), escape attempts, status conditions (poison, paralysis, sleep ticks), trading screens, name entry, save/load, game over.

## Out of scope

- Implementing any assembly hooks. This is spec-writing only.
- Writing full narrative templates in detail — rough sketches are fine. Final templates belong with the description database (Task 07).

## Done when

- `events.yaml` has 40-80 entries.
- Every entry has `trigger.file` and `trigger.function` pointing to real locations in pret/pokered (spot-check 5 random entries by opening the referenced files).
- Categories cover at least: display, overworld, battle, menu, progress, meta.
- `events-design-notes.md` lists open questions plus the gap-check results from Step 5.
