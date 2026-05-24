# Task 02: pret Fork — Telemetry Scaffolding

## Context

With the event schema in hand (Task 01), fork pret/pokered and add the infrastructure for telemetry emission. **This task is the scaffolding only** — one working "hello world" hook proves the pipeline end-to-end. Full hook implementation is Task 03.

## Goal

A fork of pret/pokered that builds in two modes (vanilla vs. instrumented), with a working serial-port telemetry transport and the first emission hook (`text_display`) proven end-to-end.

## Inputs

- `harness/events.yaml` from Task 01.
- pret/pokered upstream.

## Outputs

- A new GitHub repo `pokered-fork` (fork via GitHub UI, then add as submodule under `pokered-fork/` in the harness repo) with:
  - `engine/telemetry/emit.asm` — the `EmitEvent` routine plus per-event wrappers.
  - `engine/telemetry/event_ids.asm` — constants for event IDs, matched to events.yaml.
  - Build flag `LLM_TELEMETRY` controlling conditional assembly via `IF DEF(...)`.
  - Hook call inserted at `PlaceString` for the `text_display` event.
  - Updated Makefile / build config supporting both builds.

## Steps

1. Fork pret/pokered on GitHub. Add as submodule under `pokered-fork/` in the harness repo.
2. Create `engine/telemetry/` directory.
3. Write `event_ids.asm` — assign byte IDs to each event from `events.yaml`. Document the mapping in a comment block at the top of the file.
4. Write `emit.asm`:
   - `EmitEvent` — takes event ID in `A`, payload pointer in `HL`, payload length in `B`. Writes event ID byte to serial (`$FF01` / `$FF02`), then writes payload bytes one at a time. Preserves all registers.
   - `EmitEvent_TextDisplay` wrapper — extracts string pointer plus length, calls `EmitEvent`.
5. Wrap **all** new code in `IF DEF(LLM_TELEMETRY)` / `ENDC` blocks. Vanilla build must contain zero new bytes.
6. Insert `call EmitEvent_TextDisplay` at the top of `PlaceString` (also wrapped in the `IF DEF`).
7. Update Makefile to support two builds:
   - `make` — vanilla, default
   - `make LLM_TELEMETRY=1` — instrumented
8. Confirm vanilla build is byte-identical to upstream pret (sha1 match).
9. Build instrumented ROM. Run it in PyBoy with serial logging enabled. Trigger a dialogue in-game (the intro is easiest). Confirm the expected event_id byte plus payload appears on serial.

## Implementation notes

- Serial transmission on Game Boy: write byte to `[$FF01]`, set `[$FF02]` to `$81` to start a transfer with internal clock. The transfer takes ~1 ms of in-game time. For our purposes (emulator), this is essentially instant.
- Preserve registers around the call — game code may depend on register state.
- The `text_display` payload should be the full decoded string (up through the terminator byte), not the tile-encoded version.

## Out of scope

- Implementing any other event emissions. Only `text_display`.
- Build automation / CI (that's Task 04). Manual builds are fine for now.

## Done when

- `make` produces a ROM byte-identical to upstream pret (verify sha1).
- `make LLM_TELEMETRY=1` produces an instrumented ROM that boots and runs.
- Running the instrumented ROM in PyBoy and triggering text in-game produces serial output with the expected `text_display` event ID byte followed by the dialogue payload.
- `engine/telemetry/` contains only the scaffolding files; no other engine files are touched except for the one `call` insertion in `PlaceString`.
