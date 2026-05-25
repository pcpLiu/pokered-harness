"""Telemetry parser for Pokemon Red instrumented ROM.

Reads the byte stream captured at the EmitEventByte hook and turns it into
structured Event objects per harness/events.yaml. Also decodes the 202-byte
snapshot dump emitted on demand via wSnapshotRequest.

The wire format is:
  - Each event begins with a 1-byte event ID.
  - Most events are followed by a fixed number of payload bytes (table below,
    derived from engine/telemetry/wrappers.asm).
  - $01 (text_display) carries a variable-length string terminated by $50.
  - $1C (menu_cursor) carries a 1-byte length followed by 3 fixed bytes
    (cursor_index, max_menu_item, text_box_id) and a 16-byte tilemap window.
  - $FF (snapshot) carries a 1-byte length (always 202) followed by the payload.

If a wrapper emits fewer bytes than the YAML schema suggests, we follow the
wrapper — bytes on the wire are authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .charmap import decode_text, TERMINATOR


# ---------------------------------------------------------------------------
# Event spec — (event_id_byte) -> (id_str, category, payload_field_names)
#
# Field names match wrappers.asm emission order. Empty tuple = no payload.
# Variable-length events (text_display, snapshot) are handled specially.
# ---------------------------------------------------------------------------

# fmt: off
EVENT_SPEC: dict[int, tuple[str, str, tuple[str, ...]]] = {
    # display
    0x01: ("text_display",            "display",   ("string",)),  # variable
    0x02: ("text_box_open",           "display",   ("width", "height")),
    0x03: ("text_box_close",          "display",   ()),
    0x04: ("text_paused",             "display",   ()),
    0x05: ("yes_no_prompt",           "display",   ("menu_id",)),
    # overworld
    0x06: ("map_loaded",              "overworld", ("map_id", "width", "height", "tileset")),
    0x07: ("enter_map_anim",          "overworld", ()),
    0x08: ("player_moved",            "overworld", ("x", "y", "direction", "map_id")),
    0x09: ("player_turned",           "overworld", ("direction",)),
    0x0A: ("player_blocked",          "overworld", ("direction",)),
    0x0B: ("warp_used",               "overworld", ("src_map", "dst_map", "warp_id")),
    0x0C: ("door_entered",            "overworld", ()),
    0x0D: ("ledge_jump",              "overworld", ("direction",)),
    0x0E: ("npc_interaction_start",   "overworld", ("text_id", "map_id")),
    0x0F: ("trainer_sighted",         "overworld", ("trainer_class",)),
    0x10: ("item_picked_up",          "overworld", ("item_id", "quantity")),
    0x11: ("hidden_item_found",       "overworld", ("item_id",)),
    0x12: ("bicycle_toggled",         "overworld", ("now_active",)),
    0x13: ("surf_started",            "overworld", ()),
    0x14: ("fly_used",                "overworld", ("destination_map",)),
    0x15: ("escape_rope_used",        "overworld", ()),
    0x16: ("field_move_used",         "overworld", ()),
    0x17: ("whiteout",                "overworld", ("money_low", "money_mid", "money_high")),
    0x18: ("safari_step",             "overworld", ("steps_remaining",)),
    0x19: ("repel_expired",           "overworld", ()),
    # menu
    0x1A: ("menu_open",               "menu",      ()),
    0x1B: ("menu_close",              "menu",      ()),
    # 0x1C menu_cursor is length-prefixed — handled out-of-band, not via this table.
    0x1D: ("bag_opened",              "menu",      ()),
    0x1E: ("pokedex_opened",          "menu",      ()),
    0x1F: ("party_menu_opened",       "menu",      ("party_count",)),
    0x20: ("pc_accessed",             "menu",      ()),
    0x21: ("bills_pc_opened",         "menu",      ("current_box",)),
    0x22: ("pokemart_opened",         "menu",      ()),
    0x23: ("pokemon_center_used",     "menu",      ()),
    0x24: ("naming_screen_opened",    "menu",      ("naming_type",)),
    # progress
    0x25: ("badge_obtained",          "progress",  ("badges_bitmask",)),
    0x26: ("party_added",             "progress",  ("species", "new_party_count")),
    0x27: ("party_removed",           "progress",  ("slot", "new_party_count")),
    0x28: ("pokemon_received",        "progress",  ("species",)),
    0x29: ("item_obtained",           "progress",  ("item_id", "quantity")),
    0x2A: ("item_lost",               "progress",  ("item_id", "quantity")),
    0x2B: ("tm_taught",               "progress",  ("tm_id", "target_slot")),
    0x2C: ("money_changed",           "progress",  ("money_low", "money_mid", "money_high")),
    0x2D: ("pokedex_owned_flag_set",  "progress",  ("species",)),
    0x2E: ("pokedex_seen_flag_set",   "progress",  ("species",)),
    0x2F: ("event_flag_set",          "progress",  ()),
    0x30: ("pokemon_traded",          "progress",  ()),
    0x31: ("nickname_set",            "progress",  ()),
    # battle
    0x32: ("battle_start",            "battle",    ("battle_type", "is_trainer", "enemy_party_count", "enemy_first_species")),
    0x33: ("battle_end",              "battle",    ("result",)),
    0x34: ("move_used",               "battle",    ("side", "move_id")),
    0x35: ("move_missed",             "battle",    ()),
    0x36: ("damage_dealt",            "battle",    ("target", "damage_low", "damage_high", "new_hp_low", "new_hp_high")),
    0x37: ("critical_hit",            "battle",    ()),
    0x38: ("type_effectiveness",      "battle",    ("effectiveness",)),
    0x39: ("status_applied",          "battle",    ()),
    0x3A: ("confusion_applied",       "battle",    ()),
    0x3B: ("stat_change",             "battle",    ()),
    0x3C: ("pokemon_fainted",         "battle",    ("side", "species")),
    0x3D: ("pokemon_switched",        "battle",    ("side", "new_species")),
    0x3E: ("xp_gained",               "battle",    ("target_slot",)),
    0x3F: ("level_up",                "battle",    ("target_slot", "new_level")),
    0x40: ("move_learned",            "progress",  ("target_slot", "move_id")),
    0x41: ("move_forgotten",          "progress",  ("target_slot",)),
    0x42: ("evolution_started",       "progress",  ("target_slot",)),
    0x43: ("evolution_completed",     "progress",  ("target_slot", "new_species")),
    0x44: ("evolution_cancelled",     "progress",  ()),
    0x45: ("run_attempted",           "battle",    ()),
    0x46: ("pokeball_thrown",         "battle",    ("ball_id", "target_species")),
    0x47: ("pokemon_caught",          "battle",    ("species", "level", "ball_id")),
    0x48: ("safari_action",           "battle",    ()),
    # meta
    0x49: ("title_screen_shown",      "meta",      ()),
    0x4A: ("new_game_started",        "meta",      ()),
    0x4B: ("continue_game",           "meta",      ()),
    0x4C: ("save_written",            "meta",      ("play_time_hours", "play_time_minutes")),
    0x4D: ("oak_speech_done",         "meta",      ()),
    0x4E: ("hall_of_fame_entered",    "meta",      ()),
    0x4F: ("credits_shown",           "meta",      ()),
}
# fmt: on

SNAPSHOT_ID = 0xFF
TEXT_DISPLAY_ID = 0x01
MENU_CURSOR_ID = 0x1C
SNAPSHOT_PAYLOAD_LEN = 202
MENU_CURSOR_PAYLOAD_LEN = 19  # 3 fixed bytes + 16 tilemap bytes
MENU_CURSOR_TEXT_BYTES = 16
SPACE_TILE = 0x7F  # charmap space — used to trim option_text padding


@dataclass
class Event:
    """A decoded telemetry event."""
    id: str
    category: str
    payload: dict
    frame: int

    def to_dict(self) -> dict:
        return {"id": self.id, "category": self.category, "payload": self.payload, "frame": self.frame}


@dataclass
class Snapshot:
    """Decoded 202-byte snapshot payload (layout per engine/telemetry/wrappers.asm)."""
    frame: int
    map_id: int
    last_map: int
    x: int
    y: int
    direction: int
    player_state: int
    in_battle: int       # 0 none, 1 wild, 0xFF trainer
    text_box_id: int
    party_count: int
    party: list[dict] = field(default_factory=list)
    active_mon_idx: int = 0
    active_mon_moves: list[int] = field(default_factory=list)
    money: int = 0
    badges: int = 0
    bag_count: int = 0
    bag: list[dict] = field(default_factory=list)
    pokedex_owned: list[int] = field(default_factory=list)
    pokedex_seen: list[int] = field(default_factory=list)
    event_flags: bytes = b""
    enemy: dict | None = None
    # Menu cursor — meaningful when text_box_id != 0 (a menu is open). When no
    # menu is open these hold whatever the most recent menu left behind.
    cursor_index: int = 0
    max_menu_item: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        # bytes is not JSON-serializable; hex-encode event_flags
        d["event_flags"] = self.event_flags.hex()
        return d


def _bcd_to_int(b0: int, b1: int, b2: int) -> int:
    """Decode a 3-byte BCD value (low to high). Pret stores money as packed BCD."""
    def nyb(byte: int) -> int:
        return (byte >> 4) * 10 + (byte & 0x0F)
    # low byte holds highest digits per pret's ones-place-on-right convention?
    # Actually pret's wPlayerMoney is stored high-to-low (big-endian BCD).
    # See home/print_num.asm. Money is read as wPlayerMoney, wPlayerMoney+1,
    # wPlayerMoney+2 in big-endian order — the LSB-named bytes from our wrapper
    # are actually the upper two digits.
    # Treat b0 as the most-significant byte.
    return nyb(b0) * 10000 + nyb(b1) * 100 + nyb(b2)


def _bitfield_to_species_ids(buf: bytes) -> list[int]:
    """Convert a Pokedex bitfield (LSB = species id 1) to a sorted list of ids."""
    result = []
    for byte_idx, byte in enumerate(buf):
        for bit in range(8):
            if byte & (1 << bit):
                species = byte_idx * 8 + bit + 1
                if species <= 151:
                    result.append(species)
    return result


def _parse_menu_cursor_payload(payload: bytes) -> dict:
    """Decode the 19-byte menu_cursor payload (3 fixed bytes + 16 tilemap bytes).

    The 16 tilemap bytes are charmap-encoded; trailing $7F (space) tiles are
    padding and get trimmed before decoding. The raw bytes are also returned
    (hex-encoded) for diagnostics.
    """
    if len(payload) < 3:
        return {
            "cursor_index": 0,
            "max_menu_item": 0,
            "text_box_id": 0,
            "option_text": "",
            "raw": payload.hex(),
        }
    cursor_index = payload[0]
    max_menu_item = payload[1]
    text_box_id = payload[2]
    text_bytes = payload[3:3 + MENU_CURSOR_TEXT_BYTES]
    # Trim trailing space tiles; option text is the leading non-padding run.
    trimmed = text_bytes.rstrip(bytes([SPACE_TILE]))
    option_text = decode_text(trimmed).rstrip()
    return {
        "cursor_index": cursor_index,
        "max_menu_item": max_menu_item,
        "text_box_id": text_box_id,
        "option_text": option_text,
        "raw": text_bytes.hex(),
    }


def parse_snapshot(payload: bytes, frame: int = 0) -> Snapshot:
    """Decode a 202-byte snapshot payload. The header bytes ($FF $CA) are not
    expected here — pass just the 202 payload bytes."""
    if len(payload) != SNAPSHOT_PAYLOAD_LEN:
        raise ValueError(
            f"snapshot payload must be {SNAPSHOT_PAYLOAD_LEN} bytes, got {len(payload)}"
        )

    p = payload  # alias

    # World (8 bytes)
    map_id        = p[0]
    last_map      = p[1]
    x             = p[2]
    y             = p[3]
    direction     = p[4]
    player_state  = p[5]
    in_battle     = p[6]
    text_box_id   = p[7]

    # Party count (1 byte)
    party_count = p[8]

    # Party slots × 6 (9 bytes each, 54 bytes total)
    party: list[dict] = []
    for i in range(6):
        base = 9 + i * 9
        slot = {
            "species":  p[base + 0],
            "level":    p[base + 1],
            # HP / MaxHP are big-endian per pret convention. The wrapper emits
            # hp_lo, hp_hi in WRAM order which is high-byte-first.
            "hp_cur":   (p[base + 2] << 8) | p[base + 3],
            "hp_max":   (p[base + 4] << 8) | p[base + 5],
            "status":   p[base + 6],
            "type1":    p[base + 7],
            "type2":    p[base + 8],
        }
        party.append(slot)

    # Active mon (5 bytes)
    active_mon_idx = p[63]
    active_mon_moves = list(p[64:68])

    # Money + badges (4 bytes)
    money = _bcd_to_int(p[68], p[69], p[70])
    badges = p[71]

    # Inventory (1 + 40 bytes)
    bag_count = p[72]
    bag: list[dict] = []
    for i in range(20):
        item_id = p[73 + i * 2]
        qty = p[73 + i * 2 + 1]
        bag.append({"item_id": item_id, "quantity": qty})

    # Pokedex owned (19 bytes)
    dex_owned_bytes = p[113:132]
    pokedex_owned = _bitfield_to_species_ids(dex_owned_bytes)

    # Pokedex seen (19 bytes)
    dex_seen_bytes = p[132:151]
    pokedex_seen = _bitfield_to_species_ids(dex_seen_bytes)

    # Event flags (40 bytes)
    event_flags = bytes(p[151:191])

    # Battle context (9 bytes, meaningful only when in_battle != 0)
    enemy: dict | None = None
    if in_battle != 0:
        enemy = {
            "species":   p[191],
            "level":     p[192],
            "hp_cur":    (p[193] << 8) | p[194],
            "hp_max":    (p[195] << 8) | p[196],
            "status":    p[197],
            "type1":     p[198],
            "type2":     p[199],
        }

    # Menu cursor (2 bytes)
    cursor_index   = p[200]
    max_menu_item  = p[201]

    return Snapshot(
        frame=frame,
        map_id=map_id,
        last_map=last_map,
        x=x,
        y=y,
        direction=direction,
        player_state=player_state,
        in_battle=in_battle,
        text_box_id=text_box_id,
        party_count=party_count,
        party=party,
        active_mon_idx=active_mon_idx,
        active_mon_moves=active_mon_moves,
        money=money,
        badges=badges,
        bag_count=bag_count,
        bag=bag,
        pokedex_owned=pokedex_owned,
        pokedex_seen=pokedex_seen,
        event_flags=event_flags,
        enemy=enemy,
        cursor_index=cursor_index,
        max_menu_item=max_menu_item,
    )


class TelemetryParser:
    """Stateful byte-stream parser. Buffers between calls so partial events
    crossing call boundaries are handled correctly."""

    def __init__(self, events_yaml_path: str | Path | None = None):
        # YAML is informational — we use EVENT_SPEC for the wire format. The
        # path is accepted for API compatibility / future schema validation.
        self._yaml_path = Path(events_yaml_path) if events_yaml_path else None
        self._buf = bytearray()
        # Most recent snapshot payload we successfully parsed — request_snapshot()
        # uses this to return a Snapshot back to the caller.
        self._last_snapshot: Snapshot | None = None

    @property
    def buffer_bytes(self) -> int:
        """Number of unparsed bytes in the buffer (for debugging)."""
        return len(self._buf)

    @property
    def last_snapshot(self) -> Snapshot | None:
        return self._last_snapshot

    def feed(self, raw_bytes: bytes, frame: int) -> list[Event]:
        """Append bytes and parse out any complete events.

        `frame` is stamped onto every event produced by this call. If multiple
        events fall inside the same call, they all share the frame number —
        that's correct for the typical pattern of "step N frames, drain bytes,
        feed once."
        """
        if raw_bytes:
            self._buf.extend(raw_bytes)
        events: list[Event] = []
        while True:
            ev = self._try_parse_one(frame)
            if ev is None:
                break
            if isinstance(ev, Event):
                events.append(ev)
            # Snapshots arrive as Event objects too (id="snapshot") — caller may
            # also pull the parsed Snapshot from self.last_snapshot.
        return events

    def reset(self) -> None:
        """Clear the buffer. Call when loading state or starting a new session."""
        self._buf.clear()
        self._last_snapshot = None

    # --- internal -------------------------------------------------------

    def _try_parse_one(self, frame: int) -> Event | None:
        if not self._buf:
            return None
        first = self._buf[0]

        # Snapshot — $FF then 1-byte length, then payload.
        if first == SNAPSHOT_ID:
            if len(self._buf) < 2:
                return None
            length = self._buf[1]
            if len(self._buf) < 2 + length:
                return None
            payload = bytes(self._buf[2:2 + length])
            del self._buf[:2 + length]
            snap = parse_snapshot(payload, frame=frame)
            self._last_snapshot = snap
            return Event(id="snapshot", category="meta", payload=snap.to_dict(), frame=frame)

        # menu_cursor — $1C then 1-byte length, then payload (3 fixed + 16 text).
        if first == MENU_CURSOR_ID:
            if len(self._buf) < 2:
                return None
            length = self._buf[1]
            if len(self._buf) < 2 + length:
                return None
            payload = bytes(self._buf[2:2 + length])
            del self._buf[:2 + length]
            return Event(
                id="menu_cursor",
                category="menu",
                payload=_parse_menu_cursor_payload(payload),
                frame=frame,
            )

        # text_display — $01 then bytes up to and including $50 terminator.
        if first == TEXT_DISPLAY_ID:
            term_pos = -1
            for i in range(1, len(self._buf)):
                if self._buf[i] == TERMINATOR:
                    term_pos = i
                    break
            if term_pos == -1:
                # safety bound: wrapper emits at most 256 bytes (1 id + 255 payload)
                # before bailing; if buffer is shorter than that, wait for more.
                if len(self._buf) < 256:
                    return None
                # Defensive: drop the leading byte if we've seen more than the
                # safety bound with no terminator — corrupt stream.
                del self._buf[:1]
                return self._try_parse_one(frame)
            payload_bytes = bytes(self._buf[1:term_pos])
            del self._buf[:term_pos + 1]
            decoded = decode_text(payload_bytes)
            return Event(
                id="text_display",
                category="display",
                payload={"string": decoded, "raw": payload_bytes.hex()},
                frame=frame,
            )

        # Fixed-length event lookup
        spec = EVENT_SPEC.get(first)
        if spec is None:
            # Unknown byte — could be desync or a future event. Skip it.
            del self._buf[:1]
            return None
        event_id, category, fields = spec
        needed = 1 + len(fields)
        if len(self._buf) < needed:
            return None
        payload = {name: self._buf[1 + i] for i, name in enumerate(fields)}
        del self._buf[:needed]
        return Event(id=event_id, category=category, payload=payload, frame=frame)


def request_snapshot(
    emulator,
    parser: TelemetryParser,
    timeout_frames: int = 30,
    wsnapshot_request_addr: int = 0xDEE2,
) -> Snapshot:
    """Trigger an on-demand snapshot.

    Writes 1 to `wSnapshotRequest`. PollSnapshot is farcalled every iteration of
    OverworldLoop; on its next visit it sees the flag, emits a snapshot event,
    and clears the flag. We poll the emulator for up to `timeout_frames` frames
    waiting for the snapshot event to arrive in the parser.

    Requires both an Emulator-like object exposing step() and write_ram(), and
    a TelemetryParser to decode the stream.
    """
    # Reset last_snapshot so we know when a fresh one arrives.
    parser._last_snapshot = None
    emulator.write_ram(wsnapshot_request_addr, b"\x01")

    for _ in range(timeout_frames):
        raw = emulator.step(1)
        if raw:
            parser.feed(raw, frame=emulator.frame)
        if parser.last_snapshot is not None:
            return parser.last_snapshot

    raise TimeoutError(
        f"Snapshot did not arrive within {timeout_frames} frames. PollSnapshot is "
        "only called inside OverworldLoop — if the game is in a battle, menu, or "
        "intro animation, it may need more frames or a different game state."
    )
