# Pokemon Red AI Harness — Task Plan

This directory contains the task plan for the Pokemon Red AI Harness project.

The work splits into two streams:

1. **Pokemon Red source modifications** (Tasks 01-03) — done. Fork pret/pokered and add a telemetry channel that emits events via the Game Boy serial port plus a 200-byte snapshot pulled via WRAM sentinel. See `IMPLEMENTATION.md` for as-built status.

2. **HTTP harness server** (Tasks 04-08) — current work. Build a session-isolated, self-documenting HTTP server that lets any agent (LLM, scripted, RL) drive the instrumented ROM.

## Design context

Full design discussions live in `../brainstorm/`:

- `initial-design-discussion.md` — product framing, existing project landscape, legal considerations.
- `engine-telemetry-architecture.md` — telemetry channel design that drove Tasks 01-03.

Key decisions that shape the harness server (Tasks 04-08):

- **Plain HTTP, not MCP.** Lower complexity, wider client reach (any language can hit it with stdlib).
- **Self-documenting routes.** `GET /<route>` returns docs; `POST /<route>` executes. Inspired by pile.ly's pattern.
- **Session-folder isolation.** Each game session lives in its own folder under `sessions/<id>/`. Disk is the source of truth.
- **Effectively stateless server.** In-memory emulator cache keyed by session ID; full state persisted to folder after every action. Server can be killed and resumed without data loss.
- **Session header.** `X-Session-Id` on every session-scoped request.
- **Turn-based tempo.** Emulator only ticks during action POSTs. LLM has unbounded thinking time between calls — correct for evals; inference time should not penalize an agent.

## Tasks

### Pokemon Red modifications (done)

1. **[01 — Event Schema Audit](01-event-schema-audit.md)** — Catalog of 80 events with trigger points in pret/pokered. ✅
2. **[02 — Telemetry Scaffolding](02-pret-fork-telemetry-scaffolding.md)** — `emit.asm`, build flag, first hook proven end-to-end. ✅
3. **[03 — All Event Emissions](03-pret-fork-event-emissions.md)** — 46 hooks across the engine + 200-byte snapshot mechanism. ✅

### Harness server (current)

4. **[04 — Python Harness Foundation](04-python-harness-foundation.md)** — Emulator wrapper + event parser + snapshot decoder. No HTTP yet — just a callable Python module.
5. **[05 — Session Model + Polling](05-session-model-and-polling.md)** — Folder-isolated session lifecycle, snapshot polling controller, session registry with TTL cache.
6. **[06 — HTTP Server Core Routes](06-http-server-core-routes.md)** — FastAPI app with self-documenting routes for the basic agent loop (start, press, wait, state, events, save/load, journal).
7. **[07 — Event Streaming + Snapshot History](07-event-streaming-and-snapshot-history.md)** — SSE event stream, snapshot history queries, runtime polling control.
8. **[08 — Composite Actions + Search](08-composite-actions-and-search.md)** — `/walk`, `/talk`, `/menu`, plus text/event search routes.

## Suggested order

Do them in sequence. Each task's `Inputs` section names its dependencies. Task 04 only needs the pret fork being built locally (`make LLM_TELEMETRY=1` produces the instrumented ROM); Tasks 05-08 build on each other.

## Out of scope (for the minimal harness)

- Narrative rendering (the agent sees raw event payloads with decoded text; profile-based rewriting is a follow-on).
- Desktop app / visualization.
- Benchmark scoring system.
- Cross-repo build / diff CI (manual `make` is fine while iterating).
- Authentication (V1 is localhost only).
- Multi-agent concurrency on a single session (concurrent sessions are supported; single agent per session).
