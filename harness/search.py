"""Text + structured event search backed by the session's events.jsonl file.

Loads the full file on each call. For sessions under ~100k events this is fast
enough (tens of ms); index it later if profiling shows it matters.
"""
from __future__ import annotations

import json
from typing import Iterable

from .sessions import Session


def search_text(
    session: Session,
    query: str,
    case_sensitive: bool = False,
    limit: int = 20,
) -> dict:
    """Substring search over text_display payloads."""
    events_path = session.folder / "events.jsonl"
    if not events_path.exists():
        return {"matches": [], "count": 0}
    needle = query if case_sensitive else query.lower()
    matches: list[dict] = []
    with events_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") != "text_display":
                continue
            payload = obj.get("payload") or {}
            text = payload.get("string", "")
            haystack = text if case_sensitive else text.lower()
            if needle in haystack:
                matches.append({"frame": obj.get("frame", 0), "text": text})
                if len(matches) >= limit:
                    break
    return {"matches": matches, "count": len(matches)}


def search_events(
    session: Session,
    event_ids: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
    since_frame: int = 0,
    until_frame: int | None = None,
    limit: int = 50,
) -> dict:
    """Filter events by id, category, and frame range."""
    events_path = session.folder / "events.jsonl"
    if not events_path.exists():
        return {"matches": [], "count": 0}
    id_set = set(event_ids) if event_ids else None
    cat_set = set(categories) if categories else None
    until = until_frame if until_frame is not None else float("inf")
    matches: list[dict] = []
    with events_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            frame = obj.get("frame", 0)
            if frame < since_frame or frame > until:
                continue
            if id_set and obj.get("id") not in id_set:
                continue
            if cat_set and obj.get("category") not in cat_set:
                continue
            matches.append(obj)
            if len(matches) >= limit:
                break
    return {"matches": matches, "count": len(matches)}
