"""Static map data + live sprite reads.

`harness/data/maps.json` is produced by `scripts/extract_map_data.py` from
pret/pokered. It contains the static layout of every map in the game: name,
size, tileset, connections, warps, signs, and NPC sprite entries.

This module loads that file once and exposes a lookup API. It also reads the
WRAM sprite state (`wSpriteStateData1` / `wSpriteStateData2`) from a live
emulator so callers can get the current position of each sprite, which moves
around when NPCs walk.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


_MAPS_JSON = Path(__file__).resolve().parent / "data" / "maps.json"

# WRAM addresses (see pokered.sym / ram/wram.asm)
WSPRITE_STATE_DATA1 = 0xC100   # 16 entries × 16 bytes
WSPRITE_STATE_DATA2 = 0xC200   # 16 entries × 16 bytes
WNUMSPRITES = 0xD4E1           # number of active sprites (not including player)
WYCOORD = 0xD361               # player's map-y coord (tile units)
WXCOORD = 0xD362               # player's map-x coord
WPLAYERDIRECTION = 0xC109      # player's facing direction (same byte as sprite0 data1+9)

# Field offsets within a 16-byte sprite entry. Player is sprite 0.
SD1_PICTURE_ID = 0
SD1_FACING_DIR = 9
SD2_MAP_Y = 4   # "in 2x2 tile grid steps, topmost 2x2 tile has value 4"
SD2_MAP_X = 5
SD2_MOVEMENT = 6
SD2_PICTURE_ID = 0xD   # copy of picture id (data2 has it at offset $D)

# Facing-direction byte → readable name
FACING = {0x00: "down", 0x04: "up", 0x08: "left", 0x0C: "right"}


@lru_cache(maxsize=1)
def _load() -> dict[int, dict]:
    raw = json.loads(_MAPS_JSON.read_text())
    return {int(k): v for k, v in raw.items()}


def lookup(map_id: int) -> dict | None:
    """Return the static map record (name, size, warps, signs, objects, …) or None."""
    return _load().get(int(map_id))


def name(map_id: int) -> str:
    m = lookup(map_id)
    return m["display_name"] if m else f"map_{map_id}"


def read_live_sprites(emulator: Any, *, max_slots: int = 16) -> list[dict]:
    """Read sprite state from WRAM and return a list of active sprites.

    Each entry: {slot, picture_id, map_x, map_y, facing}.
    Slot 0 is the player. Inactive slots (picture_id == 0xFF) are skipped.

    The WRAM sprite-state coords for NPCs use a 2×2 tile grid where the
    topmost tile is y=4 — we subtract 4 so callers see natural map-tile
    coords matching the static `objects[].y` values from pret. For the player
    (slot 0) we read the authoritative `wXCoord`/`wYCoord` directly instead,
    which is always in real map-tile units.
    """
    data1 = emulator.read_ram(WSPRITE_STATE_DATA1, 16 * max_slots)
    data2 = emulator.read_ram(WSPRITE_STATE_DATA2, 16 * max_slots)
    player_x = int(emulator.read_ram(WXCOORD, 1)[0])
    player_y = int(emulator.read_ram(WYCOORD, 1)[0])
    out: list[dict] = []
    for n in range(max_slots):
        base = n * 16
        picture_id = data1[base + SD1_PICTURE_ID]
        # 0 == player (only slot 0), 0xFF == unused slot
        if n > 0 and picture_id in (0, 0xFF):
            continue
        if n == 0:
            map_x, map_y = player_x, player_y
        else:
            map_x = int(data2[base + SD2_MAP_X]) - 4
            map_y = int(data2[base + SD2_MAP_Y]) - 4
        out.append({
            "slot": n,
            "picture_id": picture_id,
            "map_y": map_y,
            "map_x": map_x,
            "facing": FACING.get(data1[base + SD1_FACING_DIR],
                                   f"0x{data1[base + SD1_FACING_DIR]:02x}"),
        })
    return out


def merge_static_and_live(map_id: int, live_sprites: list[dict]) -> dict:
    """Combine pret's static map record with the live sprite list.

    Live slot N (for N >= 1) corresponds to the Nth `object_event` entry in the
    static data (slot 0 is always the player). We pair them up so each NPC has
    both its static info (sprite name, text_id) and its live position.
    """
    static = lookup(map_id) or {"name": f"map_{map_id}", "display_name": f"map_{map_id}"}
    out = dict(static)
    # Build NPC list
    npcs = []
    statics = static.get("objects", [])
    for s in live_sprites:
        if s["slot"] == 0:
            out["player_live"] = {
                "map_x": s["map_x"], "map_y": s["map_y"], "facing": s["facing"],
            }
            continue
        npc = {
            "slot": s["slot"],
            "live_x": s["map_x"],
            "live_y": s["map_y"],
            "facing": s["facing"],
            "picture_id": s["picture_id"],
        }
        # Static slot index is slot-1 (1-indexed objects vs 0-indexed slots)
        idx = s["slot"] - 1
        if 0 <= idx < len(statics):
            stat = statics[idx]
            npc.update({
                "sprite": stat.get("sprite"),
                "text_id": stat.get("text_id"),
                "spawn_x": stat.get("x"),
                "spawn_y": stat.get("y"),
                "movement": stat.get("movement"),
            })
        npcs.append(npc)
    out["npcs_live"] = npcs
    return out
