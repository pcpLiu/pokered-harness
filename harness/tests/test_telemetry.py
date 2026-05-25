"""Unit tests for the telemetry parser. These exercise the wire format without
spinning up PyBoy."""
from __future__ import annotations

from harness.charmap import decode_text
from harness.telemetry import (
    Event,
    TelemetryParser,
    parse_snapshot,
    SNAPSHOT_PAYLOAD_LEN,
    SNAPSHOT_ID,
    MENU_CURSOR_ID,
    MENU_CURSOR_PAYLOAD_LEN,
    MENU_CURSOR_TEXT_BYTES,
)


# ---------------------------------------------------------------------------
# Charmap
# ---------------------------------------------------------------------------

def test_decode_text_letters():
    # $80-$99 = A-Z, $A0-$B9 = a-z, $7F = space, $50 = terminator
    raw = bytes([0x80, 0x81, 0x82, 0x7F, 0xA3, 0xA4, 0xA5, 0x50])
    assert decode_text(raw) == "ABC def"


def test_decode_text_line_break():
    raw = bytes([0x87, 0xA4, 0xAB, 0xAB, 0xAE, 0x4E, 0x96, 0xAE, 0xB1, 0xAB, 0xA3, 0x50])
    # Hello<NEXT>World
    assert decode_text(raw) == "Hello\nWorld"


def test_decode_text_player_marker():
    raw = bytes([0x52, 0x50])  # <PLAYER>@
    assert decode_text(raw) == "<PLAYER>"


def test_decode_text_pokemon_expansion():
    raw = bytes([0x4A, 0x50])  # <PKMN>@
    assert decode_text(raw) == "POKéMON"


def test_decode_text_stops_at_terminator():
    raw = bytes([0x80, 0x50, 0x81])  # A@B
    assert decode_text(raw) == "A"


# ---------------------------------------------------------------------------
# Parser — fixed-length events
# ---------------------------------------------------------------------------

def test_parser_zero_payload_event():
    parser = TelemetryParser()
    events = parser.feed(bytes([0x49]), frame=10)  # title_screen_shown
    assert len(events) == 1
    assert events[0].id == "title_screen_shown"
    assert events[0].category == "meta"
    assert events[0].payload == {}
    assert events[0].frame == 10


def test_parser_fixed_payload_event():
    parser = TelemetryParser()
    # player_moved: id $08, payload (x, y, direction, map_id)
    events = parser.feed(bytes([0x08, 5, 6, 0x04, 38]), frame=100)
    assert len(events) == 1
    e = events[0]
    assert e.id == "player_moved"
    assert e.category == "overworld"
    assert e.payload == {"x": 5, "y": 6, "direction": 0x04, "map_id": 38}


def test_parser_multiple_events_in_one_feed():
    parser = TelemetryParser()
    raw = (
        bytes([0x49])              # title_screen_shown
        + bytes([0x4A])            # new_game_started
        + bytes([0x06, 38, 4, 4, 2])  # map_loaded
    )
    events = parser.feed(raw, frame=200)
    assert [e.id for e in events] == ["title_screen_shown", "new_game_started", "map_loaded"]
    assert events[2].payload == {"map_id": 38, "width": 4, "height": 4, "tileset": 2}


def test_parser_partial_event_waits_for_more_bytes():
    parser = TelemetryParser()
    # player_moved needs 4 payload bytes — feed only 2
    events = parser.feed(bytes([0x08, 5, 6]), frame=1)
    assert events == []
    # Now feed the rest
    events = parser.feed(bytes([0x04, 38]), frame=2)
    assert len(events) == 1
    assert events[0].id == "player_moved"
    assert events[0].payload == {"x": 5, "y": 6, "direction": 0x04, "map_id": 38}


# ---------------------------------------------------------------------------
# Parser — text_display
# ---------------------------------------------------------------------------

def test_parser_text_display_complete():
    parser = TelemetryParser()
    # $01 [H E L L O] $50
    raw = bytes([0x01, 0x87, 0xA4, 0xAB, 0xAB, 0xAE, 0x50])
    events = parser.feed(raw, frame=42)
    assert len(events) == 1
    e = events[0]
    assert e.id == "text_display"
    assert e.payload["string"] == "Hello"
    assert e.frame == 42


def test_parser_text_display_split_across_feeds():
    parser = TelemetryParser()
    parser.feed(bytes([0x01, 0x87, 0xA4]), frame=1)
    events = parser.feed(bytes([0xAB, 0xAB, 0xAE, 0x50]), frame=2)
    assert len(events) == 1
    assert events[0].payload["string"] == "Hello"
    assert events[0].frame == 2


def test_parser_text_display_followed_by_other_event():
    parser = TelemetryParser()
    raw = bytes([0x01, 0x80, 0x50, 0x49])  # text "A" + title_screen_shown
    events = parser.feed(raw, frame=5)
    assert [e.id for e in events] == ["text_display", "title_screen_shown"]


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _make_snapshot_payload(
    *,
    in_battle: int = 0,
    party_count: int = 0,
    cursor_index: int = 0,
    max_menu_item: int = 0,
) -> bytes:
    """Build a synthetic 202-byte snapshot payload for testing."""
    p = bytearray(SNAPSHOT_PAYLOAD_LEN)
    # World
    p[0] = 38   # map_id
    p[1] = 37   # last_map
    p[2] = 5    # x
    p[3] = 6    # y
    p[4] = 0x04  # direction = up
    p[5] = 0    # walking
    p[6] = in_battle
    p[7] = 0    # text_box_id
    # Party count
    p[8] = party_count
    # Slot 0 — Bulbasaur at lvl 5 with 19/19 HP if party_count > 0
    if party_count > 0:
        p[9] = 0x99   # species (Bulbasaur is $99 in internal id table; arbitrary here)
        p[10] = 5     # level
        p[11] = 0     # hp big-endian high byte
        p[12] = 19    # hp big-endian low byte
        p[13] = 0     # max_hp high
        p[14] = 19    # max_hp low
        p[15] = 0     # status
        p[16] = 22    # type1 (GRASS)
        p[17] = 3     # type2 (POISON)
    # active mon idx
    p[63] = 0
    # active mon moves
    p[64:68] = bytes([33, 45, 0, 0])  # tackle, growl, none, none
    # Money — BCD big-endian: 3000 = $03 $00 $00
    p[68] = 0x00
    p[69] = 0x30
    p[70] = 0x00
    # Badges
    p[71] = 0
    # Bag
    p[72] = 1            # bag_count
    p[73] = 4            # item id (Potion = 4 in pret)
    p[74] = 5            # quantity
    # Pokedex owned — Bulbasaur (species 1) set
    p[113] = 0x01
    # Pokedex seen — Bulbasaur + Pidgey (species 16) set
    p[132] = 0x01
    p[133] = 0x80  # bit 7 of byte 1 = species 16
    # Event flags — leave zeros
    # Enemy block — leave zeros unless in_battle
    if in_battle:
        p[191] = 0x70    # species
        p[192] = 5       # level
        p[193] = 0       # hp_hi
        p[194] = 12      # hp_lo
        p[195] = 0       # max_hp_hi
        p[196] = 20      # max_hp_lo
        p[197] = 0       # status
        p[198] = 4       # type1
        p[199] = 4       # type2
    # Menu cursor
    p[200] = cursor_index
    p[201] = max_menu_item
    return bytes(p)


def test_parse_snapshot_overworld():
    payload = _make_snapshot_payload(
        in_battle=0, party_count=1, cursor_index=2, max_menu_item=5
    )
    snap = parse_snapshot(payload, frame=1000)
    assert snap.map_id == 38
    assert snap.last_map == 37
    assert snap.x == 5
    assert snap.y == 6
    assert snap.direction == 0x04
    assert snap.party_count == 1
    assert snap.party[0]["species"] == 0x99
    assert snap.party[0]["level"] == 5
    assert snap.party[0]["hp_cur"] == 19
    assert snap.party[0]["hp_max"] == 19
    assert snap.party[0]["type1"] == 22
    assert snap.in_battle == 0
    assert snap.enemy is None
    assert snap.money == 3000
    assert snap.bag_count == 1
    assert snap.bag[0] == {"item_id": 4, "quantity": 5}
    assert snap.pokedex_owned == [1]
    assert snap.pokedex_seen == [1, 16]
    assert snap.event_flags == bytes(40)
    assert snap.cursor_index == 2
    assert snap.max_menu_item == 5


def test_parse_snapshot_battle_context():
    payload = _make_snapshot_payload(in_battle=1, party_count=1)
    snap = parse_snapshot(payload, frame=2000)
    assert snap.in_battle == 1
    assert snap.enemy is not None
    assert snap.enemy["species"] == 0x70
    assert snap.enemy["level"] == 5
    assert snap.enemy["hp_cur"] == 12
    assert snap.enemy["hp_max"] == 20


def test_parser_snapshot_via_feed():
    parser = TelemetryParser()
    payload = _make_snapshot_payload(party_count=0)
    raw = bytes([SNAPSHOT_ID, SNAPSHOT_PAYLOAD_LEN]) + payload
    events = parser.feed(raw, frame=500)
    assert len(events) == 1
    assert events[0].id == "snapshot"
    assert parser.last_snapshot is not None
    assert parser.last_snapshot.map_id == 38


def test_parser_snapshot_split_across_feeds():
    parser = TelemetryParser()
    payload = _make_snapshot_payload()
    raw = bytes([SNAPSHOT_ID, SNAPSHOT_PAYLOAD_LEN]) + payload
    # Split in the middle
    half = len(raw) // 2
    assert parser.feed(raw[:half], frame=1) == []
    events = parser.feed(raw[half:], frame=2)
    assert len(events) == 1
    assert events[0].id == "snapshot"


def test_parser_snapshot_length_mismatch_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_snapshot(b"\x00" * (SNAPSHOT_PAYLOAD_LEN - 1))


# ---------------------------------------------------------------------------
# Parser — menu_cursor
# ---------------------------------------------------------------------------

def _make_menu_cursor_payload(
    *,
    cursor_index: int = 0,
    max_menu_item: int = 0,
    text_box_id: int = 0,
    text_bytes: bytes = b"",
) -> bytes:
    """Build a synthetic 19-byte menu_cursor payload. `text_bytes` is padded
    with $7F (space) to fill the 16-byte text window."""
    padding = bytes([0x7F]) * (MENU_CURSOR_TEXT_BYTES - len(text_bytes))
    return bytes([cursor_index, max_menu_item, text_box_id]) + text_bytes + padding


def test_parser_menu_cursor_basic():
    parser = TelemetryParser()
    # cursor=1, max=4, box=2, option "ITEM"
    item_text = bytes([0x88, 0x93, 0x84, 0x8C])  # I T E M
    payload = _make_menu_cursor_payload(
        cursor_index=1, max_menu_item=4, text_box_id=2, text_bytes=item_text
    )
    raw = bytes([MENU_CURSOR_ID, MENU_CURSOR_PAYLOAD_LEN]) + payload
    events = parser.feed(raw, frame=77)
    assert len(events) == 1
    e = events[0]
    assert e.id == "menu_cursor"
    assert e.category == "menu"
    assert e.frame == 77
    assert e.payload["cursor_index"] == 1
    assert e.payload["max_menu_item"] == 4
    assert e.payload["text_box_id"] == 2
    assert e.payload["option_text"] == "ITEM"


def test_parser_menu_cursor_trims_trailing_spaces():
    parser = TelemetryParser()
    # 16-byte buffer with only "POKéDEX" (7 chars) and rest spaces
    # P O K é D E X — using internal charmap codes
    text = bytes([0x8F, 0x8E, 0x8A, 0xBA, 0x83, 0x84, 0x97])  # POKéDEX
    payload = _make_menu_cursor_payload(
        cursor_index=0, max_menu_item=6, text_box_id=8, text_bytes=text
    )
    raw = bytes([MENU_CURSOR_ID, MENU_CURSOR_PAYLOAD_LEN]) + payload
    events = parser.feed(raw, frame=0)
    assert events[0].payload["option_text"] == "POKéDEX"


def test_parser_menu_cursor_split_across_feeds():
    parser = TelemetryParser()
    item_text = bytes([0x88, 0x93, 0x84, 0x8C])
    payload = _make_menu_cursor_payload(
        cursor_index=2, max_menu_item=5, text_box_id=1, text_bytes=item_text
    )
    raw = bytes([MENU_CURSOR_ID, MENU_CURSOR_PAYLOAD_LEN]) + payload
    half = len(raw) // 2
    assert parser.feed(raw[:half], frame=1) == []
    events = parser.feed(raw[half:], frame=2)
    assert len(events) == 1
    assert events[0].id == "menu_cursor"
    assert events[0].payload["cursor_index"] == 2
    assert events[0].payload["option_text"] == "ITEM"


def test_parser_menu_cursor_followed_by_other_event():
    parser = TelemetryParser()
    text = bytes([0x80])  # "A"
    raw = (
        bytes([MENU_CURSOR_ID, MENU_CURSOR_PAYLOAD_LEN])
        + _make_menu_cursor_payload(text_bytes=text)
        + bytes([0x49])  # title_screen_shown
    )
    events = parser.feed(raw, frame=11)
    assert [e.id for e in events] == ["menu_cursor", "title_screen_shown"]


def test_parser_reset_clears_buffer():
    parser = TelemetryParser()
    parser.feed(bytes([0x08, 1, 2]), frame=1)  # partial event
    assert parser.buffer_bytes > 0
    parser.reset()
    assert parser.buffer_bytes == 0
    assert parser.last_snapshot is None
