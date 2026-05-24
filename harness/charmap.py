"""Pokemon Red text charmap decoder.

Mirrors pret/pokered's `constants/charmap.asm`. The Game Boy stores text in a
custom encoding; PlaceString reads from that encoding and writes tiles to VRAM.
The telemetry text_display hook captures the source bytes before the typewriter
animation, so we need to decode the source encoding back to readable text.

Control codes that have no readable equivalent (e.g. <SCROLL>, <PROMPT>) are
preserved as bracketed markers — useful both for the LLM (which can recognize
"prompt waiting") and for narrative renderers downstream.
"""

# Byte -> printable representation.
# Pure-graphic characters render directly; control codes render as <NAME>
# markers; line/paragraph breaks render as actual whitespace.
CHARMAP: dict[int, str] = {}

# Control / formatting codes
CHARMAP[0x00] = ""  # NULL (filler in some payloads)
CHARMAP[0x49] = "\n\n"  # <PAGE>
CHARMAP[0x4A] = "POKéMON"  # <PKMN>
# Typewriter pause / continuation codes. The engine uses these to mean
# "wait for the player to press A, then keep drawing the same dialog box on
# the next line." Visually they appear as a line break — render them that
# way so the decoded text reads naturally. The raw bytes are still preserved
# in payload["raw"] for anyone who needs to recover the original markers.
CHARMAP[0x4B] = "\n"  # <_CONT>
CHARMAP[0x4C] = "\n"  # <SCROLL>
CHARMAP[0x4E] = "\n"  # <NEXT>
CHARMAP[0x4F] = "\n"  # <LINE>
# 0x50 = '@' string terminator — handled by parser, not part of decoded text.
CHARMAP[0x51] = "\n\n"  # <PARA>
CHARMAP[0x52] = "<PLAYER>"
CHARMAP[0x53] = "<RIVAL>"
CHARMAP[0x54] = "POKé"  # '#' character
CHARMAP[0x55] = "\n"  # <CONT>
CHARMAP[0x56] = "……"  # <……>
CHARMAP[0x57] = ""  # <DONE> — terminator-ish, no glyph
CHARMAP[0x58] = "\n\n"  # <PROMPT> — full pause; treat as paragraph break
CHARMAP[0x59] = "<TARGET>"
CHARMAP[0x5A] = "<USER>"
CHARMAP[0x5B] = "PC"
CHARMAP[0x5C] = "TM"
CHARMAP[0x5D] = "TRAINER"
CHARMAP[0x5E] = "ROCKET"
CHARMAP[0x5F] = ""  # <DEXEND>, no glyph

# Bold-font letters (graphics-only)
for byte, ch in zip(range(0x60, 0x69), "ABCDEFGHI"):
    CHARMAP[byte] = ch
CHARMAP[0x69] = "V"  # bold V
CHARMAP[0x6A] = "S"  # bold S
CHARMAP[0x6B] = "L"
CHARMAP[0x6C] = "M"
CHARMAP[0x6D] = ":"  # tinier colon
CHARMAP[0x6E] = "<LV>"  # battle-extra "LV"
CHARMAP[0x6F] = "ぅ"

# Quotation marks, ellipsis, box drawing
CHARMAP[0x70] = "‘"
CHARMAP[0x71] = "’"
CHARMAP[0x72] = "“"
CHARMAP[0x73] = "”"
CHARMAP[0x74] = "·"
CHARMAP[0x75] = "…"
CHARMAP[0x76] = "ぁ"
CHARMAP[0x77] = "ぇ"
CHARMAP[0x78] = "ぉ"
CHARMAP[0x79] = "┌"
CHARMAP[0x7A] = "─"
CHARMAP[0x7B] = "┐"
CHARMAP[0x7C] = "│"
CHARMAP[0x7D] = "└"
CHARMAP[0x7E] = "┘"
CHARMAP[0x7F] = " "  # space

# A-Z
for byte, ch in zip(range(0x80, 0x9A), "ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    CHARMAP[byte] = ch

CHARMAP[0x9A] = "("
CHARMAP[0x9B] = ")"
CHARMAP[0x9C] = ":"
CHARMAP[0x9D] = ";"
CHARMAP[0x9E] = "["
CHARMAP[0x9F] = "]"

# a-z
for byte, ch in zip(range(0xA0, 0xBA), "abcdefghijklmnopqrstuvwxyz"):
    CHARMAP[byte] = ch

CHARMAP[0xBA] = "é"
CHARMAP[0xBB] = "'d"
CHARMAP[0xBC] = "'l"
CHARMAP[0xBD] = "'s"
CHARMAP[0xBE] = "'t"
CHARMAP[0xBF] = "'v"

CHARMAP[0xE0] = "'"
CHARMAP[0xE1] = "PK"  # <PK>
CHARMAP[0xE2] = "MN"  # <MN>
CHARMAP[0xE3] = "-"

CHARMAP[0xE4] = "'r"
CHARMAP[0xE5] = "'m"

CHARMAP[0xE6] = "?"
CHARMAP[0xE7] = "!"
CHARMAP[0xE8] = "."

CHARMAP[0xE9] = "ァ"
CHARMAP[0xEA] = "ゥ"
CHARMAP[0xEB] = "ェ"

CHARMAP[0xEC] = "▷"
CHARMAP[0xED] = "▶"
CHARMAP[0xEE] = "▼"
CHARMAP[0xEF] = "♂"
CHARMAP[0xF0] = "¥"
CHARMAP[0xF1] = "×"
CHARMAP[0xF2] = "."  # decimal point
CHARMAP[0xF3] = "/"
CHARMAP[0xF4] = ","
CHARMAP[0xF5] = "♀"

# Digits 0-9
for byte, ch in zip(range(0xF6, 0x100), "0123456789"):
    CHARMAP[byte] = ch


# Text-script control codes ($00-$17) — rarely seen at PlaceString level (those
# are pre-resolved by the text-script interpreter) but emit safe placeholders
# just in case.
for b in range(0x01, 0x18):
    CHARMAP.setdefault(b, f"<TX_{b:02x}>")


TERMINATOR = 0x50  # '@'


def decode_text(payload: bytes) -> str:
    """Decode a bytes payload (without the terminating $50) to readable text."""
    out: list[str] = []
    for b in payload:
        if b == TERMINATOR:
            break
        out.append(CHARMAP.get(b, f"<${b:02x}>"))
    return "".join(out)
