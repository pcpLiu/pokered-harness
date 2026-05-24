# Task 05: Session Model + Polling

## Context

The HTTP server is "effectively stateless" — sessions are folders on disk, and the server holds a cache of in-memory emulator instances keyed by session ID. This task builds the session abstraction without yet adding the HTTP layer.

The disk folder is the source of truth. Every action persists. Server crash mid-action loses at most one action. Session folders are portable — zip and share for replay or debugging.

## Goal

A `harness/sessions.py` module that manages session folders, persists state after every action, and runs configurable snapshot polling that builds a queryable history.

## Inputs

- `harness/emulator.py` and `harness/telemetry.py` from Task 04.

## Outputs

- `harness/sessions.py`
- `harness/tests/test_sessions.py`

## Folder layout

```
sessions/
└── <session_id>/                  # e.g. pallet-explorer-7f3a
    ├── meta.json                   # session_id, created_at, rom_path, rom_sha1,
    │                               # snapshot_interval_frames, status, current_frame
    ├── current.state               # live PyBoy state (overwritten every action)
    ├── events.jsonl                # append-only event log since session start
    ├── actions.jsonl               # append-only action log (every POST received)
    ├── journal.md                  # agent's persistent notes
    ├── snapshots/                  # polled snapshots
    │   ├── 0000000060.json
    │   ├── 0000000120.json
    │   └── ...                     # name = zero-padded frame number
    └── saves/                      # named save states the agent created
        ├── pre-brock.state
        └── ...
```

## Session API

```python
@dataclass
class SessionMeta:
    session_id: str
    created_at: datetime
    rom_path: str
    rom_sha1: str
    snapshot_interval_frames: int   # 0 = polling disabled
    current_frame: int
    status: Literal["active", "ended"]

class Session:
    """Owns one Emulator + parser. Persists to a folder."""

    def __init__(self, folder: Path, meta: SessionMeta): ...

    @classmethod
    def create(cls, base_dir: Path, rom_path: str, name: str | None = None) -> "Session":
        """Create folder, load instrumented ROM, write initial meta.json."""

    @classmethod
    def open(cls, folder: Path) -> "Session":
        """Restore from existing folder. Loads current.state into PyBoy."""

    def press_button(self, button: str, count: int = 1) -> list[Event]: ...
    def wait(self, frames: int) -> list[Event]: ...

    def snapshot_now(self) -> Snapshot:
        """Fresh pull via wSnapshotRequest."""

    def get_snapshot_history(
        self,
        since_frame: int = 0,
        until_frame: int | None = None,
        limit: int = 100
    ) -> list[Snapshot]: ...

    def set_polling_interval(self, frames: int): ...

    def save_named_state(self, name: str): ...
    def load_named_state(self, name: str): ...

    def search_text(self, query: str, case_sensitive: bool = False) -> list[Event]: ...

    def append_journal(self, text: str): ...
    def read_journal(self) -> str: ...

    def end(self):
        """Mark session ended. Persist final state."""

    def persist(self):
        """Flush in-memory state to disk. Called after every action."""

class SessionRegistry:
    """In-memory cache of open Sessions, keyed by session_id. TTL eviction."""

    def __init__(self, base_dir: Path, ttl_seconds: int = 600): ...

    def get_or_load(self, session_id: str) -> Session:
        """Return cached session, or load from disk if not in cache."""

    def create(self, rom_path: str, name: str | None = None) -> Session: ...

    def list(self) -> list[SessionMeta]:
        """List all session folders under base_dir."""

    def end_session(self, session_id: str): ...
    def delete_session(self, session_id: str): ...
    def evict_idle(self): ...
```

## Snapshot polling

Each Session has a `snapshot_interval_frames`. When non-zero, the Session automatically requests a fresh snapshot every N frames during `press_button` / `wait` calls. The snapshot is parsed, written to `snapshots/<frame>.json`, and tracked in-memory for fast history queries.

This is what builds the queryable history. The polling interval controls granularity (fewer frames = more snapshots = more storage + finer history). 0 disables polling — snapshots only on explicit `snapshot_now()` call.

Default: 60 frames (one game-second).

Polling fires from inside `press_button` / `wait`, not via a background timer — keeps the implementation single-threaded and deterministic.

## Persistence rules

After every `press_button` / `wait` / `save_named_state` / `load_named_state`:

1. Append new events to `events.jsonl` (one JSON object per line).
2. Append the action to `actions.jsonl`.
3. Write `current.state` (PyBoy state).
4. Update `meta.json` (current_frame, last activity).

After every polled snapshot:

5. Write `snapshots/<frame>.json`.

Use atomic write pattern (`write to tmp + rename`) for the files that get rewritten (`meta.json`, `current.state`) to avoid corrupted state if the process crashes mid-write.

## Session ID format

Adjective-noun-hex by default: `pallet-explorer-7f3a`. Allow user-supplied names via the `name` parameter; if taken, append `-2`, `-3`, etc. Validate name as kebab-case alphanumeric for filesystem safety.

## Steps

1. Implement `SessionMeta` dataclass with JSON serialization.
2. Implement `Session.create` — mkdir, save initial meta, load ROM, write initial state.
3. Implement `Session.open` — read meta, restore PyBoy from `current.state`.
4. Implement `press_button` / `wait` wrappers that bundle: action → emulator step → drain telemetry → parse events → polling check → persist.
5. Implement snapshot polling loop inside `press_button` / `wait` — track frames since last poll; when interval reached, call snapshot, write to disk, update in-memory cache.
6. Implement `get_snapshot_history` — scan `snapshots/` directory, filter by frame range, parse JSON, return.
7. Implement `search_text` — load `events.jsonl`, filter on `text_display` payloads.
8. Implement `SessionRegistry` with TTL eviction.
9. Tests:
   - Create → press → end → reopen, verify state continuity (frame counter, party).
   - Polling at default interval produces expected number of snapshot files for known duration.
   - Search returns expected hits for known dialogue substring.
   - Disabling polling stops snapshot creation but `snapshot_now()` still works.
   - Registry: create, list, evict idle, reload-on-access.

## Out of scope

- HTTP routes (Task 06).
- SSE event subscription mechanism (Task 07).
- Composite actions (Task 08).

## Done when

- `Session.create()` produces a folder with all expected files.
- `Session.open()` on that folder restores fully — pressing the next button continues correctly from where state was saved.
- After N frames of activity at default polling, `snapshots/` contains ~N/60 files.
- `search_text("professor")` returns at least one match after viewing the Oak intro dialogue.
- All tests pass.
