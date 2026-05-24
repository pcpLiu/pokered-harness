"""Composite actions: server-side wrappers that compose multiple emulator
inputs into one route call.

Filled in fully in Task 08. The bodies are real (not stubs) so the server
exposes useful behavior, but the test coverage and edge handling lives in
the Task 08 deliverables.
"""
from __future__ import annotations

from typing import Iterable

from .sessions import Session
from .telemetry import Event


# Events that indicate a walk should abort early. The first one to appear
# during the walk stops it.
WALK_ABORT_EVENTS = frozenset({
    "battle_start",
    "npc_interaction_start",
    "menu_open",
    "map_loaded",
})


DIRECTION_TO_BUTTON = {
    "up": "up", "down": "down", "left": "left", "right": "right",
}


def abortable_walk(session: Session, direction: str, tiles: int) -> dict:
    """Walk `tiles` tiles in `direction`, aborting on interruption events.

    Returns a dict with `completed`, `tiles_traversed`, `events`, `abort_reason`.
    """
    button = DIRECTION_TO_BUTTON.get(direction)
    if button is None:
        raise ValueError(f"invalid direction: {direction}")
    all_events: list[Event] = []
    aborted: str | None = None
    traversed = 0
    for _ in range(tiles):
        evs = session.press_button(button, hold_frames=8, release_frames=8)
        all_events.extend(evs)
        # Check the events from this single press for an abort trigger.
        for ev in evs:
            if ev.id in WALK_ABORT_EVENTS:
                aborted = ev.id
                break
        if aborted:
            break
        # Count this tile as traversed if we observed a player_moved event.
        if any(ev.id == "player_moved" for ev in evs):
            traversed += 1
        else:
            # If no movement happened, assume a wall blocked us. Still count
            # the attempt but bail so we don't burn frames on a wall.
            if any(ev.id == "player_blocked" for ev in evs):
                aborted = "player_blocked"
                break
    return {
        "completed": aborted is None and traversed == tiles,
        "tiles_traversed": traversed,
        "events": [ev.to_dict() for ev in all_events],
        "abort_reason": aborted,
    }


def talk(session: Session, count: int = 1) -> list[Event]:
    """Press A `count` times. Used to engage or advance NPC dialogue."""
    return session.press_button("a", count=count)


# wCurrentMenuItem and wMaxMenuItem are at known WRAM addresses. Pulled from
# pokered.sym at build time:
WCURRENT_MENU_ITEM = 0xCC26
WMAX_MENU_ITEM = 0xCC28


def menu_select(
    session: Session,
    target: str | None = None,
    target_index: int | None = None,
) -> dict:
    """Navigate a menu cursor and press A.

    Currently supports `target_index` directly. The `target` (name lookup)
    path requires tilemap decoding and is left for the Task 08 follow-on —
    when provided we fall back to a NotImplementedError so the caller can
    use target_index.
    """
    if target_index is None and target is not None:
        # The current minimal implementation does not decode the on-screen
        # tilemap to find option strings; refusing here is more honest than
        # guessing. Task 08 expands this for the Start menu and battle menu.
        raise ValueError(
            "target-by-name lookup is not yet supported; pass target_index for now"
        )
    if target_index is None:
        raise ValueError("supply either target or target_index")

    # Read current cursor position from WRAM.
    cur = session.emulator.read_ram(WCURRENT_MENU_ITEM, 1)[0]
    events: list[Event] = []
    while cur != target_index:
        if cur < target_index:
            evs = session.press_button("down", hold_frames=4, release_frames=8)
        else:
            evs = session.press_button("up", hold_frames=4, release_frames=8)
        events.extend(evs)
        new = session.emulator.read_ram(WCURRENT_MENU_ITEM, 1)[0]
        if new == cur:
            # Couldn't move — bail so we don't loop forever.
            return {
                "completed": False,
                "events": [ev.to_dict() for ev in events],
                "cursor_index_final": cur,
                "reason": "cursor did not advance — menu may not be active",
            }
        cur = new
    confirm = session.press_button("a", hold_frames=5, release_frames=12)
    events.extend(confirm)
    return {
        "completed": True,
        "events": [ev.to_dict() for ev in events],
        "cursor_index_final": cur,
    }
