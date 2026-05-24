# Pokemon Red AI Harness

A session-isolated HTTP harness for driving a telemetry-instrumented build of
Pokemon Red. Designed for LLM and scripted agents — every route is
self-documenting, the emulator only ticks during action POSTs (so the agent
has unbounded thinking time), and every session is persisted to a folder on
disk for portability and replay.

## Repository layout

```
.
├── harness/             Python package: emulator wrapper, session model,
│                        telemetry parser, FastAPI server, composites, search
├── scripts/             run_server.py, extract_map_data.py
├── start.sh             One-shot bootstrap (builds ROM, starts server, opens viewer)
├── demo.py              Minimal pipeline smoke test
├── tasks/               The original task plan
├── brainstorm/          Design discussion + architecture notes
└── pokered-fork/        Submodule → github.com/pcpLiu/pokered (the
                          instrumented Pokemon Red fork)
```

## Quick start

```bash
# Clone with submodule
git clone --recurse-submodules git@github.com:pcpLiu/pokered-harness.git
cd pokered-harness

# (or if already cloned without --recurse-submodules:)
git submodule update --init

# Boot everything (builds the ROM, sets up venv, launches server + viewer)
./start.sh --fresh
```

Open http://localhost:8000/viewer in a browser and play.

## Updating the fork

The ROM source lives in the [`pokered-fork` submodule](https://github.com/pcpLiu/pokered).

```bash
# Pull upstream pret/pokered changes into the fork:
cd pokered-fork
git fetch upstream                       # (after `git remote add upstream …`)
git merge upstream/master                # resolve conflicts in telemetry files
git push origin feat/llm-telemetry

# Bump the submodule pointer in the harness repo:
cd ..
git add pokered-fork
git commit -m "Bump pokered-fork to <commit>"
```

## Endpoints

The server is self-documenting — every route returns markdown when you `GET`
it, and executes when you `POST`. Start here:

- `GET /`                 — index of all routes
- `GET /press` / `POST /press`
- `GET /state` / `POST /state`
- `GET /map`   / `POST /map`
- `GET /walk`, `GET /talk`, `GET /menu/select`
- `GET /search/text`, `GET /search/events`
- `GET /events/stream`    — Server-Sent Events live stream

Add `?format=json` to any GET to get the structured doc dict instead of markdown.

## Architecture overview

See [`tasks/IMPLEMENTATION.md`](tasks/IMPLEMENTATION.md) for the full as-built
status of the ROM-side instrumentation, and `tasks/` for the original task
breakdowns (Tasks 01–08).
