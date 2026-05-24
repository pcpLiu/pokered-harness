# Task 04: Python Harness Foundation

## Context

The pret fork from Tasks 02-03 emits structured events via the Game Boy serial port and exposes a 200-byte snapshot via a WRAM sentinel byte. We now need the Python side: a module that loads the instrumented ROM in PyBoy, captures the serial stream, parses events per `harness/events.yaml`, decodes the snapshot, and exposes a clean API for higher layers to use.

**No HTTP yet.** This task produces a pure-Python module callable from a script or REPL. The HTTP server (Task 06) sits on top.

Important practical note from `tasks/IMPLEMENTATION.md`: PyBoy 2.x's serial stub does not actually capture `rSB` writes (it pins SB to `0xFF` and never clears `SC_START`). The authoritative capture point is a hook at the entry address of `EmitEventByte` in the instrumented ROM. Look up the symbol address from the build map file.

## Goal

Two Python modules:

- `harness/emulator.py` — PyBoy wrapper with telemetry capture, button input, save/load, RAM access.
- `harness/telemetry.py` — Event parser + 200-byte snapshot decoder + charmap decoder.

Plus a `demo.py` smoke script that loads the ROM, presses Start, and prints events that fire.

## Inputs

- `harness/events.yaml` (existing, from Task 01).
- `pokered/` instrumented build — running `make LLM_TELEMETRY=1` in the pokered fork produces `pokered.gbc`.
- The build's symbol/map file for resolving `EmitEventByte` and `wSnapshotRequest` addresses.
- PyBoy installed. Pin a working version in `requirements.txt`.

## Outputs

- `harness/emulator.py`
- `harness/telemetry.py`
- `harness/tests/test_emulator.py`
- `harness/tests/test_telemetry.py`
- `requirements.txt`
- `demo.py`

## Emulator API

```python
class Emulator:
    def __init__(self, rom_path: str, expected_sha1: str | None = None):
        """Load instrumented ROM. Optionally verify SHA-1."""

    def step(self, frames: int = 1) -> bytes:
        """Advance N frames. Return raw telemetry bytes captured during those frames."""

    def press_button(self, button: str, hold_frames: int = 5, release_frames: int = 5) -> bytes:
        """Press, hold, release. Return raw telemetry captured throughout."""

    def get_screen(self) -> np.ndarray:
        """Current screen as HxWx3 numpy array."""

    def read_ram(self, addr: int, length: int = 1) -> bytes: ...
    def write_ram(self, addr: int, data: bytes): ...

    def save_state(self, path: str): ...
    def load_state(self, path: str): ...

    @property
    def frame(self) -> int:
        """Current frame counter."""

    def close(self): ...
```

## Telemetry API

```python
@dataclass
class Event:
    id: str            # e.g. "text_display"
    category: str      # display | overworld | menu | progress | battle | meta
    payload: dict      # decoded payload fields
    frame: int

@dataclass
class Snapshot:
    """Decoded 200-byte snapshot. Layout per IMPLEMENTATION.md."""
    map_id: int
    last_map: int
    x: int
    y: int
    direction: int
    player_state: int
    in_battle: bool
    text_box_id: int
    party_count: int
    party: list[dict]          # species, level, hp_cur, hp_max, status, type1, type2
    active_mon_idx: int
    active_mon_moves: list[int]
    money: int
    badges: int                # bitfield
    bag_count: int
    bag: list[dict]            # item_id, quantity
    pokedex_owned: list[int]   # species ids
    pokedex_seen: list[int]
    event_flags: bytes
    enemy: dict | None         # populated only when in_battle

class TelemetryParser:
    def __init__(self, events_yaml_path: str):
        """Load schema. Build event-id -> definition lookup."""

    def feed(self, raw_bytes: bytes, frame: int) -> list[Event]:
        """Parse bytes. Stateful — keeps a partial buffer between calls."""

    def parse_snapshot(self, raw_payload: bytes) -> Snapshot:
        """Decode 200-byte snapshot payload."""

def request_snapshot(
    emulator: Emulator,
    parser: TelemetryParser,
    timeout_frames: int = 30
) -> Snapshot:
    """Write 1 to wSnapshotRequest. Step until snapshot event arrives. Parse and return."""
```

## Steps

1. **Install + smoke.** Pin a PyBoy version. Confirm `pyboy.PyBoy("pokered.gbc")` loads the instrumented ROM and runs without errors. Verify a known title-screen text appears in the rendered screen.
2. **Serial capture via PC breakpoint.** Read the build map file to find the address of `EmitEventByte`. Use PyBoy's hook/breakpoint mechanism (`pyboy.hook_register(bank, addr, callback)` or equivalent) to fire on every entry. In the callback, read register A and append to an internal buffer.
3. **Build the parser.** Load events.yaml at parser init. Build `event_id_byte -> event_def`. Implement `feed()` as a state machine that handles variable-length payloads (text_display has a length prefix per the schema; snapshot is 200 fixed bytes after the header).
4. **Charmap decoder.** Copy pret's charmap table into a Python dict. Terminator is `$50` ("@" in pret notation). Control codes inside dialogue (line breaks, scroll, party-member name placeholders) should be either preserved as readable markers or stripped — pick one and document it in `events-design-notes.md`.
5. **Snapshot decoder.** Decode the 200-byte payload field-by-field per the layout in `IMPLEMENTATION.md`. HP fields are big-endian. Pokedex bitfields are 19 bytes each (151 bits used). Skip enemy block when `in_battle == 0`.
6. **Button input.** Use PyBoy's button send/release API. Wrap with hold/release timing. Drain telemetry buffer at end of each press.
7. **Save/load state.** Use PyBoy's state serialization. Round-trip test.
8. **`request_snapshot()` helper.** Write `0x01` to the WRAM address of `wSnapshotRequest` (look up symbol). Step the emulator for `timeout_frames` frames or until a snapshot event arrives. Parse and return.
9. **`demo.py`** — minimal script: load ROM, press Start a few times, print events. Exercises the whole pipeline.

## Out of scope

- HTTP server (Task 06).
- Session folder / persistence (Task 05).
- Narrative rendering — emit raw events with decoded text fields. Higher layers can prettify.
- Patch-based loading. For V1, load the already-instrumented ROM produced by `make LLM_TELEMETRY=1`. A vanilla-ROM + patches.json flow can be added later for distribution.

## Done when

- `python demo.py path/to/pokered.gbc` loads the ROM, presses Start, and prints at least one `text_display` event with decoded title-screen text.
- `request_snapshot()` returns a `Snapshot` with all fields populated for a freshly-loaded ROM.
- `test_emulator.py` and `test_telemetry.py` pass.
