"""Hand-crafted route documentation for the Pokemon Red AI Harness.

`GET /<path>` returns the corresponding entry from `ROUTE_DOCS`. We write the
descriptions for human (and LLM) consumption rather than relying on FastAPI's
automatic OpenAPI generator — LLMs respond much better to prose than to
JSON schema dumps.
"""

VERSION = "0.1"
SESSION_HEADER = "X-Session-Id"

VALID_BUTTONS = ["a", "b", "up", "down", "left", "right", "start", "select"]


INDEX_DOC = {
    "version": VERSION,
    "session_header": SESSION_HEADER,
    "session_lifecycle": [
        "POST /sessions/start",
        "POST /sessions/list",
        "POST /sessions/end",
        "POST /sessions/delete",
    ],
    "routes_requiring_session": [
        "/press", "/wait", "/state", "/map", "/events", "/save", "/load", "/journal", "/screen",
        "/snapshots/history", "/snapshots/poll", "/events/stream",
        "/walk", "/talk", "/menu/select", "/search/text", "/search/events",
    ],
    "notes": (
        "Every route is self-documenting via GET. POST to execute. Include the "
        f"{SESSION_HEADER} header on every call except /sessions/start and "
        "/sessions/list. Errors are JSON objects with at least an 'error' key. "
        "The emulator only advances during action POSTs — there is no background "
        "game loop, so an LLM can think between calls without burning game time."
    ),
}


ROUTE_DOCS: dict[str, dict] = {
    # --- Lifecycle -------------------------------------------------------
    "/sessions/start": {
        "method": "POST",
        "path": "/sessions/start",
        "description": (
            "Create a new game session. The server allocates a folder for it on disk, "
            "loads the instrumented ROM into a fresh emulator, and returns the session "
            f"id. Pass it back as the '{SESSION_HEADER}' header on every subsequent call."
        ),
        "requires_header": None,
        "params": {
            "rom_path": {
                "type": "string",
                "required": False,
                "description": "Path to the instrumented .gbc ROM. If omitted, the server's default ROM is used.",
            },
            "name": {
                "type": "string",
                "required": False,
                "description": "Optional kebab-case name. Collisions append -2, -3, etc.",
            },
            "snapshot_interval_frames": {
                "type": "integer",
                "default": 60,
                "description": "How often the session pulls a full snapshot during press/wait calls. 0 disables auto-polling.",
            },
        },
        "returns": {
            "session_id": "Unique id for this session.",
            "header_to_use": f"Pre-formatted '{SESSION_HEADER}: <id>' for convenience.",
            "folder": "Filesystem path of the session folder.",
            "rom_sha1": "SHA-1 of the ROM in use.",
        },
        "example": {
            "request": {"name": "kanto-run"},
            "response": {
                "session_id": "kanto-run",
                "header_to_use": f"{SESSION_HEADER}: kanto-run",
                "folder": "/path/to/sessions/kanto-run",
                "rom_sha1": "8605d3842a4cd0f2c50cf76da97587ac2771a20c",
            },
        },
    },

    "/sessions/list": {
        "method": "POST",
        "path": "/sessions/list",
        "description": "List every session folder on disk, with metadata.",
        "requires_header": None,
        "params": {},
        "returns": {
            "sessions": "List of session metadata objects (session_id, created_at, rom_path, rom_sha1, current_frame, status).",
        },
        "example": {
            "request": {},
            "response": {
                "sessions": [
                    {"session_id": "kanto-run", "created_at": "2025-01-01T00:00:00Z",
                     "current_frame": 4200, "status": "active"},
                ]
            },
        },
    },

    "/sessions/end": {
        "method": "POST",
        "path": "/sessions/end",
        "description": "Gracefully end the session. The state file is persisted, emulator resources freed. The folder is kept; reopen with /sessions/start? No — once ended, the session is read-only. Use /sessions/delete to remove.",
        "requires_header": SESSION_HEADER,
        "params": {},
        "returns": {"session_id": "echoed back", "status": "ended"},
        "example": {
            "request": {},
            "response": {"session_id": "kanto-run", "status": "ended"},
        },
    },

    "/sessions/delete": {
        "method": "POST",
        "path": "/sessions/delete",
        "description": "Delete the session's folder entirely. Use sparingly — this throws away everything (events, snapshots, journal). For finishing a session use /sessions/end instead.",
        "requires_header": SESSION_HEADER,
        "params": {},
        "returns": {"deleted": "the session id that was removed"},
        "example": {
            "request": {},
            "response": {"deleted": "kanto-run"},
        },
    },

    # --- Session-scoped action routes -----------------------------------
    "/press": {
        "method": "POST",
        "path": "/press",
        "description": (
            "Press a Game Boy button. The button is held briefly, then released. "
            "Game frames advance during both phases. Returns events that fired."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "button": {
                "type": "string",
                "required": True,
                "enum": VALID_BUTTONS,
            },
            "count": {
                "type": "integer",
                "default": 1,
                "description": "Press this many times in a row (useful for advancing dialogue).",
            },
            "hold_frames": {"type": "integer", "default": 5,
                            "description": "Frames the button is held down."},
            "release_frames": {"type": "integer", "default": 5,
                               "description": "Frames to run after releasing."},
        },
        "returns": {
            "events": "List of structured events fired during the press.",
            "frame": "Current frame after the press.",
        },
        "example": {
            "request": {"button": "a"},
            "response": {
                "events": [{"id": "text_display", "payload": {"string": "HELLO!"}, "frame": 1252}],
                "frame": 1252,
            },
        },
    },

    "/wait": {
        "method": "POST",
        "path": "/wait",
        "description": "Advance N frames without sending input. Useful for letting animations finish or trainer scripts run.",
        "requires_header": SESSION_HEADER,
        "params": {
            "frames": {"type": "integer", "required": True,
                       "description": "How many frames to step."},
        },
        "returns": {"events": "events fired", "frame": "current frame"},
        "example": {
            "request": {"frames": 60},
            "response": {"events": [], "frame": 1312},
        },
    },

    "/tick": {
        "method": "POST",
        "path": "/tick",
        "description": (
            "Advance N frames WITHOUT recording an action. Used by the browser "
            "viewer to animate the game in real time without polluting "
            "actions.jsonl. Persists current.state at most once per second. "
            "An LLM agent should use /wait (which records) instead."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "frames": {"type": "integer", "default": 1,
                       "description": "How many frames to step. Range 1..600."},
            "target_fps": {"type": "number", "default": 60,
                            "description": "Wall-clock pacing target. 60 = real-time, 30 = half speed, 120 = 2×, 0 = no pacing (run as fast as the host can)."},
        },
        "returns": {"events": "events fired", "frame": "current frame"},
        "example": {
            "request": {"frames": 2, "target_fps": 60},
            "response": {"events": [], "frame": 1314},
        },
    },

    "/map": {
        "method": "POST",
        "path": "/map",
        "description": (
            "Return map layout. With an empty body, returns the player's "
            "current map merged with live NPC positions read from WRAM "
            "(works even mid-dialogue, no snapshot emit needed). With "
            "`{\"map_id\": N}` or `{\"name\": \"PALLET_TOWN\"}`, returns the "
            "static layout for any map in the game (248 maps, no live "
            "sprites since the player isn't there). Useful for enriching a "
            "`map_loaded` event you saw in the log, or planning a route."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "map_id": {"type": "integer", "required": False,
                       "description": "Look up a specific map by id (0-247)."},
            "name": {"type": "string", "required": False,
                     "description": "Look up by pret constant name, e.g. PALLET_TOWN."},
        },
        "returns": {
            "map_id": "numeric map id (echoed back, or wCurMap when no arg given)",
            "map": (
                "{display_name, name, width_blocks, height_blocks, tileset, "
                "connections, warps[], signs[], objects[] (static spawns). "
                "Current-map calls also include npcs_live[] and "
                "player_live{map_x, map_y, facing}}"
            ),
            "live": "true if the response includes WRAM-read sprite positions",
        },
        "example": {
            "request": {"map_id": 0},
            "response": {
                "map_id": 0,
                "map": {
                    "display_name": "Pallet Town",
                    "warps": [{"x": 5, "y": 5, "to_map": "REDS_HOUSE_1F", "to_warp": 1}],
                    "signs": [{"x": 13, "y": 13, "text_id": "TEXT_PALLETTOWN_OAKSLAB_SIGN"}],
                    "objects": [
                        {"x": 8, "y": 5, "sprite": "SPRITE_OAK", "text_id": "TEXT_PALLETTOWN_OAK"},
                    ],
                },
                "live": False,
            },
        },
    },

    "/state": {
        "method": "POST",
        "path": "/state",
        "description": (
            "Pull a fresh full-state snapshot from the running ROM. Triggers a "
            "wSnapshotRequest write; the engine emits a 200-byte payload on its "
            "next PollSnapshot tick (next OverworldLoop iteration). Returns the "
            "decoded Snapshot object. Note: during intros, battles, or menus, "
            "PollSnapshot isn't running and this can time out — wait until the "
            "player is on the overworld."
        ),
        "requires_header": SESSION_HEADER,
        "params": {},
        "returns": {
            "snapshot": "Decoded snapshot fields. See engine/telemetry/wrappers.asm for layout."
        },
        "example": {
            "request": {},
            "response": {
                "snapshot": {
                    "frame": 4242,
                    "map_id": 38, "x": 5, "y": 6, "direction": 4,
                    "party_count": 1, "money": 3000, "badges": 0,
                    "in_battle": 0,
                }
            },
        },
    },

    "/events": {
        "method": "POST",
        "path": "/events",
        "description": (
            "Query the persisted event log. By default returns the most recent events. "
            "Use 'since_frame' to filter by time and 'categories' to filter by category."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "since_frame": {"type": "integer", "default": 0},
            "until_frame": {"type": "integer", "default": None},
            "categories": {"type": "array<string>", "default": None,
                           "description": "Filter to these categories only (display, overworld, menu, progress, battle, meta)."},
            "limit": {"type": "integer", "default": 200},
        },
        "returns": {"events": "list of matching events", "count": "number returned"},
        "example": {
            "request": {"categories": ["display"], "limit": 5},
            "response": {
                "events": [
                    {"id": "text_display", "category": "display",
                     "payload": {"string": "NEW GAME\nOPTION"}, "frame": 1210},
                ],
                "count": 1,
            },
        },
    },

    "/save": {
        "method": "POST",
        "path": "/save",
        "description": "Save the current emulator state to a named slot under the session's saves/ folder.",
        "requires_header": SESSION_HEADER,
        "params": {
            "name": {"type": "string", "required": True,
                     "description": "Slot name. Kebab-case, alphanumeric + hyphens."},
        },
        "returns": {"saved": "the slot name", "path": "absolute path to the saved state file"},
        "example": {
            "request": {"name": "pre-brock"},
            "response": {"saved": "pre-brock", "path": "/path/to/sessions/kanto-run/saves/pre-brock.state"},
        },
    },

    "/load": {
        "method": "POST",
        "path": "/load",
        "description": "Restore the emulator from a previously saved named slot.",
        "requires_header": SESSION_HEADER,
        "params": {
            "name": {"type": "string", "required": True},
        },
        "returns": {"loaded": "the slot name", "frame": "current frame after load"},
        "example": {
            "request": {"name": "pre-brock"},
            "response": {"loaded": "pre-brock", "frame": 4000},
        },
    },

    "/journal": {
        "method": "POST",
        "path": "/journal",
        "description": (
            "Read or append to the agent's persistent journal (journal.md). Use this "
            "for notes you want to recall later — strategies, NPC observations, etc."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "op": {"type": "string", "required": True, "enum": ["read", "append"]},
            "text": {"type": "string", "required": False,
                     "description": "Required when op='append'."},
        },
        "returns": {"journal": "full journal text after the operation"},
        "example": {
            "request": {"op": "append", "text": "Mom says I should level up before facing Brock."},
            "response": {"journal": "Mom says I should level up before facing Brock.\n"},
        },
    },

    "/screen": {
        "method": "POST",
        "path": "/screen",
        "description": "Capture the current 160x144 Game Boy screen as a base64-encoded PNG.",
        "requires_header": SESSION_HEADER,
        "params": {},
        "returns": {
            "image_base64": "base64-encoded PNG bytes",
            "frame": "current frame",
            "width": 160,
            "height": 144,
        },
        "example": {
            "request": {},
            "response": {"image_base64": "iVBORw0KGgo...", "frame": 1234, "width": 160, "height": 144},
        },
    },

    # --- Streaming + history (Task 07) ---------------------------------
    "/events/stream": {
        "method": "POST",
        "path": "/events/stream",
        "description": (
            "Open a Server-Sent Events stream. The server pushes events as they fire "
            "during action POSTs. Connection stays open until the client disconnects. "
            "Since the emulator only advances during action POSTs, events flow in "
            "bursts — between actions the stream is idle."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "since_frame": {"type": "integer", "default": 0},
            "categories": {"type": "array<string>", "default": None},
        },
        "returns": {
            "stream": "SSE chunks of the form 'event: <id>\\ndata: <json>\\n\\n'",
        },
        "example": {
            "request": {"categories": ["battle"]},
            "response": "event: battle_start\\ndata: {\"id\":\"battle_start\",...}\\n\\n",
        },
    },

    "/snapshots/history": {
        "method": "POST",
        "path": "/snapshots/history",
        "description": (
            "Query polled snapshot history from disk. Use 'fields' to project only "
            "specific snapshot fields (saves bandwidth on long queries)."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "since_frame": {"type": "integer", "default": 0},
            "until_frame": {"type": "integer", "default": None},
            "limit": {"type": "integer", "default": 100},
            "fields": {
                "type": "array<string>",
                "default": None,
                "description": "If present, return only these top-level fields of each snapshot.",
            },
        },
        "returns": {"snapshots": "list of (possibly projected) snapshots", "count": "number returned"},
        "example": {
            "request": {"fields": ["map_id", "x", "y"], "limit": 3},
            "response": {
                "snapshots": [
                    {"frame": 60, "map_id": 38, "x": 5, "y": 6},
                    {"frame": 120, "map_id": 38, "x": 5, "y": 7},
                    {"frame": 180, "map_id": 38, "x": 6, "y": 7},
                ],
                "count": 3,
            },
        },
    },

    "/snapshots/poll": {
        "method": "POST",
        "path": "/snapshots/poll",
        "description": (
            "Get or set the snapshot polling interval. Set 0 to disable automatic "
            "polling. The setting is persisted to the session's meta.json."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "interval_frames": {
                "type": "integer",
                "required": False,
                "description": "If omitted, returns the current value without changing it.",
            },
        },
        "returns": {
            "interval_frames": "the new (or current) interval",
            "previous": "the prior value if it was changed",
        },
        "example": {
            "request": {"interval_frames": 30},
            "response": {"interval_frames": 30, "previous": 60},
        },
    },

    # --- Composite + search (Task 08) -----------------------------------
    "/walk": {
        "method": "POST",
        "path": "/walk",
        "description": (
            "Walk N tiles in a direction. Aborts early on battle_start, "
            "npc_interaction_start, menu_open, or map_loaded."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "direction": {"type": "string", "required": True, "enum": ["up", "down", "left", "right"]},
            "tiles": {"type": "integer", "required": True},
        },
        "returns": {
            "completed": "bool — true if all tiles traversed",
            "tiles_traversed": "how many tiles we actually moved",
            "events": "events that fired during the walk",
            "abort_reason": "event id that caused the abort, or null",
        },
        "example": {
            "request": {"direction": "up", "tiles": 3},
            "response": {"completed": True, "tiles_traversed": 3, "events": [], "abort_reason": None},
        },
    },

    "/talk": {
        "method": "POST",
        "path": "/talk",
        "description": (
            "Press A. In Pokemon Red, talking to an NPC is just pressing A while "
            "facing them. Position with /walk first."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "count": {"type": "integer", "default": 1,
                      "description": "Press A this many times — useful for paging through dialogue."},
        },
        "returns": {"events": "events fired"},
        "example": {
            "request": {"count": 2},
            "response": {"events": [{"id": "text_display", "payload": {"string": "Hello!"}, "frame": 1500}]},
        },
    },

    "/menu/select": {
        "method": "POST",
        "path": "/menu/select",
        "description": (
            "Navigate a menu cursor to a target and press A. Supply either "
            "'target_index' (numeric) or 'target' (string match against visible options "
            "in supported menus). Returns 400 with the visible options if no match."
        ),
        "requires_header": SESSION_HEADER,
        "params": {
            "target": {"type": "string", "required": False},
            "target_index": {"type": "integer", "required": False},
        },
        "returns": {
            "completed": "bool",
            "events": "events fired",
            "cursor_index_final": "the cursor position after navigation",
        },
        "example": {
            "request": {"target_index": 0},
            "response": {"completed": True, "events": [], "cursor_index_final": 0},
        },
    },

    "/search/text": {
        "method": "POST",
        "path": "/search/text",
        "description": "Full-text substring search over text_display events in this session's history.",
        "requires_header": SESSION_HEADER,
        "params": {
            "query": {"type": "string", "required": True},
            "case_sensitive": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 20},
        },
        "returns": {"matches": "list of {frame, text}", "count": "number matched"},
        "example": {
            "request": {"query": "professor"},
            "response": {"matches": [{"frame": 142, "text": "..."}], "count": 1},
        },
    },

    "/search/events": {
        "method": "POST",
        "path": "/search/events",
        "description": "Filter the event log by event ids, categories, and frame range.",
        "requires_header": SESSION_HEADER,
        "params": {
            "event_ids": {"type": "array<string>", "default": None},
            "categories": {"type": "array<string>", "default": None},
            "since_frame": {"type": "integer", "default": 0},
            "until_frame": {"type": "integer", "default": None},
            "limit": {"type": "integer", "default": 50},
        },
        "returns": {"matches": "list of events", "count": "number matched"},
        "example": {
            "request": {"event_ids": ["battle_start", "battle_end"]},
            "response": {"matches": [], "count": 0},
        },
    },
}
