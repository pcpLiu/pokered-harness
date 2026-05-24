"""Extract static map data from pret/pokered into a single JSON file.

Reads:
  pokered-fork/constants/map_constants.asm   — map IDs, sizes, display name
  pokered-fork/data/maps/objects/*.asm        — warps, signs, NPCs per map

Writes:
  harness/data/maps.json                      — {map_id: {name, ...}}

The JSON is consumed by harness/maps.py and surfaced via the /map HTTP route.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRET = ROOT / "pokered-fork"
OUT = ROOT / "harness" / "data" / "maps.json"


MAP_CONST_RE = re.compile(
    r"^\s*map_const\s+([A-Z_0-9]+)\s*,\s*(\d+)\s*,\s*(\d+)",
    re.MULTILINE,
)


def pascal_case(map_const: str) -> str:
    """PALLET_TOWN -> PalletTown.  ROUTE_1 -> Route1."""
    return "".join(part.capitalize() for part in map_const.split("_"))


def pretty_name(map_const: str) -> str:
    """PALLET_TOWN -> 'Pallet Town'.  ROUTE_1 -> 'Route 1'."""
    parts = map_const.split("_")
    return " ".join(p[:1] + p[1:].lower() if not p.isdigit() else p for p in parts).strip()


def parse_constants() -> list[tuple[str, int, int]]:
    """Return list of (map_const, width, height) in declaration order — index
    is the map id."""
    text = (PRET / "constants" / "map_constants.asm").read_text()
    return [
        (name, int(w), int(h))
        for name, w, h in MAP_CONST_RE.findall(text)
    ]


# Strip comments + trim
def _line_iter(text: str):
    for line in text.splitlines():
        line = line.split(";", 1)[0].rstrip()
        if line.strip():
            yield line


def parse_objects(map_const: str) -> dict:
    """Parse data/maps/objects/<PascalCase>.asm. Returns dict with:
        border_block, warps, signs, objects.
    Missing file → all empty."""
    fname = PRET / "data" / "maps" / "objects" / f"{pascal_case(map_const)}.asm"
    result = {"border_block": None, "warps": [], "signs": [], "objects": []}
    if not fname.exists():
        return result

    text = fname.read_text()
    section = None  # "warps" | "signs" | "objects" | None

    for raw in _line_iter(text):
        line = raw.strip()

        # Border block: `db $b`
        if result["border_block"] is None:
            m = re.match(r"db\s+\$([0-9a-fA-F]+)\s*$", line)
            if m:
                result["border_block"] = int(m.group(1), 16)
                continue

        if "def_warp_events" in line:
            section = "warps"; continue
        if "def_bg_events" in line:
            section = "signs"; continue
        if "def_object_events" in line:
            section = "objects"; continue
        if "def_warps_to" in line:
            section = None; continue

        if section == "warps":
            # warp_event  X,  Y, DEST_MAP, dest_warp_id
            m = re.match(r"warp_event\s+(-?\d+)\s*,\s*(-?\d+)\s*,\s*([A-Z_0-9]+)\s*,\s*(\d+)", line)
            if m:
                result["warps"].append({
                    "x": int(m.group(1)),
                    "y": int(m.group(2)),
                    "to_map": m.group(3),
                    "to_warp": int(m.group(4)),
                })
        elif section == "signs":
            # bg_event X, Y, TEXT_ID
            m = re.match(r"bg_event\s+(-?\d+)\s*,\s*(-?\d+)\s*,\s*([A-Z_0-9]+)", line)
            if m:
                result["signs"].append({
                    "x": int(m.group(1)),
                    "y": int(m.group(2)),
                    "text_id": m.group(3),
                })
        elif section == "objects":
            # object_event X, Y, SPRITE_ID, MOVEMENT, FACING, TEXT_ID  (+optional trainer args)
            m = re.match(
                r"object_event\s+(-?\d+)\s*,\s*(-?\d+)\s*,\s*([A-Z_0-9]+)"
                r"\s*,\s*([A-Z_0-9]+)\s*,\s*([A-Z_0-9]+)"
                r"\s*,\s*([A-Z_0-9]+)"
                r"(?:\s*,\s*(.+))?",
                line,
            )
            if m:
                obj = {
                    "x": int(m.group(1)),
                    "y": int(m.group(2)),
                    "sprite": m.group(3),
                    "movement": m.group(4),
                    "facing": m.group(5),
                    "text_id": m.group(6),
                }
                if m.group(7):
                    extra = [x.strip() for x in m.group(7).split(",")]
                    obj["extra"] = extra
                result["objects"].append(obj)
    return result


def parse_header(map_const: str) -> dict:
    """Parse data/maps/headers/<PascalCase>.asm to extract connections + tileset.

    Header format:
        map_header MapName, MAP_CONST, TILESET, DIRS
        connection north, NeighborMap, NEIGHBOR_CONST, offset
        end_map_header
    """
    fname = PRET / "data" / "maps" / "headers" / f"{pascal_case(map_const)}.asm"
    result: dict = {"tileset": None, "connections": {}}
    if not fname.exists():
        return result

    for line in _line_iter(fname.read_text()):
        line = line.strip()
        if line.startswith("map_header"):
            # map_header MapName, MAP_CONST, TILESET, FLAGS
            parts = [p.strip() for p in line[len("map_header"):].split(",")]
            if len(parts) >= 3:
                result["tileset"] = parts[2]
        elif line.startswith("connection"):
            # connection direction, MapName, MAP_CONST, offset
            parts = [p.strip() for p in line[len("connection"):].split(",")]
            if len(parts) >= 3:
                direction = parts[0]
                neighbor_const = parts[2]
                result["connections"][direction] = neighbor_const
    return result


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    maps: dict[int, dict] = {}
    consts = parse_constants()
    print(f"Found {len(consts)} maps in constants/map_constants.asm")
    for idx, (map_const, w, h) in enumerate(consts):
        entry = {
            "id": idx,
            "name": map_const,
            "display_name": pretty_name(map_const),
            "width_blocks": w,
            "height_blocks": h,
        }
        entry.update(parse_header(map_const))
        entry.update(parse_objects(map_const))
        maps[idx] = entry

    # Sort keys for stable output. Use string keys (JSON object keys must be strings).
    out = {str(k): maps[k] for k in sorted(maps)}
    OUT.write_text(json.dumps(out, indent=2))
    print(f"Wrote {len(out)} maps to {OUT.relative_to(ROOT)}")

    # Quick sanity print for the first few + a busy one
    for i in [0, 1, 37, 38]:
        e = maps.get(i)
        if e:
            print(f"  [{i}] {e['display_name']:<25} "
                  f"({e['width_blocks']}×{e['height_blocks']} blocks, tileset {e.get('tileset')}) "
                  f"warps={len(e.get('warps', []))} signs={len(e.get('signs', []))} npcs={len(e.get('objects', []))}")


if __name__ == "__main__":
    main()
