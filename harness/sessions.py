"""Session lifecycle and folder-based persistence.

Each session lives in a folder under `sessions/<id>/`. The folder is the
source of truth: an in-memory `Session` instance is a cache of state derived
from disk. The server can be killed mid-action and at most one action is
lost — everything else is durable on disk.

Layout:

    sessions/<session_id>/
      meta.json           # session metadata (id, rom, polling interval, frame)
      current.state       # latest PyBoy state, overwritten after every action
      events.jsonl        # append-only event log (one JSON object per line)
      actions.jsonl       # append-only action log
      journal.md          # agent's persistent notes
      snapshots/<frame>.json   # polled snapshot history (zero-padded frame)
      saves/<name>.state       # named save slots
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .emulator import Emulator, rom_sha1
from .telemetry import Event, Snapshot, TelemetryParser, request_snapshot


def _new_emulator(rom_path, expected_sha1, window, use_process):
    """Construct an in-process Emulator or a process-backed EmulatorProcess.

    They share an interface, so the rest of the Session code doesn't care which
    one it has. Process mode is required when window='SDL2' on macOS so the
    native window's event loop has a dedicated main thread.
    """
    if use_process:
        # Lazy import — process_emulator pulls in multiprocessing machinery.
        from .process_emulator import EmulatorProcess
        return EmulatorProcess(rom_path, expected_sha1=expected_sha1, window=window)
    return Emulator(rom_path, expected_sha1=expected_sha1, window=window)


log = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_FRAMES = 60  # one game-second at ~60 fps
SESSION_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRAME_PAD = 10  # 10 digits ≈ 27 years of frames at 60fps — plenty of room


# A small vocabulary of words used to assemble the default
# "adjective-noun-hex" identifier. Kept short and game-themed.
_ADJECTIVES = (
    "brave", "calm", "clever", "curious", "daring", "eager", "happy",
    "lucky", "mighty", "quiet", "quick", "shy", "sly", "swift", "wild",
)
_NOUNS = (
    "ember", "flame", "leaf", "pebble", "ridge", "river", "stone", "thunder",
    "tide", "trail", "valley", "wave", "wing", "wisp", "moon",
)


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@dataclass
class SessionMeta:
    session_id: str
    created_at: str            # ISO-8601 UTC
    rom_path: str
    rom_sha1: str
    snapshot_interval_frames: int = DEFAULT_POLL_INTERVAL_FRAMES
    current_frame: int = 0
    status: Literal["active", "ended"] = "active"
    last_activity: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionMeta":
        # Drop unknown keys for forward-compat.
        fields = {f for f in cls.__annotations__}
        return cls(**{k: v for k, v in d.items() if k in fields})


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class SessionEnded(RuntimeError):
    """Raised when an action is attempted on an ended session."""


class Session:
    """One game session backed by a folder.

    Owns one Emulator + TelemetryParser. Methods that change game state
    (`press_button`, `wait`, `save_named_state`, `load_named_state`) persist
    automatically.
    """

    def __init__(self, folder: Path, meta: SessionMeta, emulator: Emulator, parser: TelemetryParser):
        self.folder = Path(folder)
        self.meta = meta
        self.emulator = emulator
        self.parser = parser
        self._last_poll_frame = (emulator.frame // max(meta.snapshot_interval_frames, 1)) * max(meta.snapshot_interval_frames, 1) if meta.snapshot_interval_frames else 0
        self._last_touched = time.monotonic()
        # SSE-style async subscribers (Task 07). Each entry is a
        # (loop, queue) pair so the publisher (which may run in a worker
        # thread) can hand events to the asyncio side safely.
        self._subscribers: list[tuple["asyncio.AbstractEventLoop", "asyncio.Queue[Event]"]] = []
        # Inferred dialogue context. There's no "speaker" field in Pokemon
        # Red's text data, so we attach the most likely source based on
        # surrounding events (NPC interaction, battle, etc.). Each
        # `text_display` event gets the context at its frame.
        self._dialogue_context: dict = {"scene": "boot"}
        # PyBoy is not thread-safe — pyboy.tick() releases the GIL during
        # its C-level work, so two threads calling into the same instance
        # concurrently can corrupt internal state. Every emulator-touching
        # route runs via asyncio.to_thread (different worker threads), so
        # we need a per-session lock to serialize them. Without this, an
        # auto-ticking viewer + a /press from another connection can race
        # through transitions (turning the LCD off, scrambling palettes,
        # leaving the CPU in a wait loop) and the game appears stuck.
        self._emu_lock = threading.RLock()

    # ----- construction / restore --------------------------------------

    @classmethod
    def create(
        cls,
        base_dir: Path | str,
        rom_path: str | Path,
        name: str | None = None,
        snapshot_interval_frames: int = DEFAULT_POLL_INTERVAL_FRAMES,
        window: str = "null",
        use_process: bool = False,
    ) -> "Session":
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        session_id = _pick_session_id(base_dir, name)
        folder = base_dir / session_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "snapshots").mkdir()
        (folder / "saves").mkdir()
        (folder / "events.jsonl").touch()
        (folder / "actions.jsonl").touch()
        (folder / "journal.md").touch()

        rom_path = str(Path(rom_path).resolve())
        sha = rom_sha1(rom_path)
        now = _now_iso()
        meta = SessionMeta(
            session_id=session_id,
            created_at=now,
            rom_path=rom_path,
            rom_sha1=sha,
            snapshot_interval_frames=snapshot_interval_frames,
            current_frame=0,
            status="active",
            last_activity=now,
        )
        emu = _new_emulator(rom_path, sha, window, use_process)
        parser = TelemetryParser()
        session = cls(folder, meta, emu, parser)
        session._write_meta()
        # Initial state file so an immediate reopen works.
        emu.save_state(folder / "current.state")
        return session

    @classmethod
    def open(
        cls,
        folder: Path | str,
        window: str = "null",
        use_process: bool = False,
    ) -> "Session":
        folder = Path(folder)
        meta_path = folder / "meta.json"
        if not meta_path.exists():
            raise FileNotFoundError(f"no meta.json under {folder}")
        meta = SessionMeta.from_dict(json.loads(meta_path.read_text()))
        emu = _new_emulator(meta.rom_path, meta.rom_sha1, window, use_process)
        state_path = folder / "current.state"
        if state_path.exists():
            emu.load_state(state_path)
        parser = TelemetryParser()
        return cls(folder, meta, emu, parser)

    # ----- properties --------------------------------------------------

    @property
    def session_id(self) -> str:
        return self.meta.session_id

    @property
    def ended(self) -> bool:
        return self.meta.status == "ended"

    def _require_active(self) -> None:
        if self.ended:
            raise SessionEnded(f"session {self.session_id} has ended")

    # ----- noise filtering ------------------------------------------------
    #
    # The instrumented ROM has a few hooks that fire in places they shouldn't,
    # plus we have `snapshot` events that are pull-mode and shouldn't pollute
    # the event log. We drop these here, before publishing to subscribers and
    # before appending to events.jsonl. The raw stream is still parsed
    # faithfully — we just decide not to surface noise.
    #
    # Known issues being filtered:
    #
    # * `snapshot` events arrive as part of the wire stream, but conceptually
    #   they are pull-mode state dumps. They get written to snapshots/<frame>.json
    #   on their own; including them in events.jsonl + the SSE stream would
    #   spam the log with 200-byte JSON every poll cycle.
    #
    # * `battle_start` is hooked at `InitBattle:` (engine/battle/core.asm), but
    #   InitBattle is called as the FIRST step of every wild-encounter dice
    #   roll — most of which fail. When it fails, the enemy fields are all
    #   zero. We drop those obviously-spurious events. (The right fix is to
    #   move the hook to `InitBattleCommon:` in the ROM; this is a stopgap.)
    #
    # * `credits_shown` is hooked at `Credits:` but the Oak-intro Pokemon
    #   pic-display routines pull from the same compilation unit and end up
    #   firing the hook. We only let credits_shown through after the player
    #   has actually entered the Hall of Fame.

    def _filter_noise(self, events: list[Event]) -> list[Event]:
        out: list[Event] = []
        for ev in events:
            if ev.id == "snapshot":
                continue
            if ev.id == "battle_start":
                p = ev.payload or {}
                if (
                    p.get("battle_type", 0) == 0
                    and p.get("is_trainer", 0) == 0
                    and p.get("enemy_party_count", 0) == 0
                    and p.get("enemy_first_species", 0) == 0
                ):
                    continue
            if ev.id == "credits_shown" and not getattr(self, "_post_game", False):
                continue
            out.append(ev)
        return out

    # ----- dialogue context ------------------------------------------------
    #
    # Pokemon Red doesn't tag text with a speaker. We infer one by tracking
    # the most recent "what kind of scene are we in" event:
    #
    #   scene = "boot"        — before any game has started
    #   scene = "oak_intro"   — after new_game_started, before player can move
    #   scene = "overworld"   — walking around freely
    #   scene = "npc"         — A-button text from NPC / sign (carries text_id+map_id)
    #   scene = "battle"      — battle in progress (carries trainer flag + species)
    #   scene = "menu"        — start menu / sub-menu open
    #
    # Each text_display event we publish gets a `context` field with the
    # current scene snapshot. The state machine is purely advisory — for
    # ambiguous cases (e.g. a battle dialog from a wild encounter the player
    # never "started"), the most recent scene wins.

    def _annotate_and_update_context(self, events: list[Event]) -> None:
        """Tag each text_display with the current context, then advance the
        context state machine in event order."""
        for ev in events:
            if ev.id == "text_display":
                # Snapshot the context AT THIS POINT — context-changing events
                # earlier in the same batch have already taken effect.
                ev.payload["context"] = dict(self._dialogue_context)
            self._step_context(ev)

    def _step_context(self, ev: Event) -> None:
        scene = self._dialogue_context.get("scene")
        if ev.id == "new_game_started":
            self._dialogue_context = {"scene": "oak_intro"}
        elif ev.id == "oak_speech_done":
            # Oak's monologue ends; the next thing is the naming screen / fade
            # into Pallet Town. Stay in oak_intro until we see a map_loaded
            # that moves us to the overworld.
            pass
        elif ev.id == "npc_interaction_start":
            self._dialogue_context = {
                "scene": "npc",
                "text_id": ev.payload.get("text_id"),
                "map_id": ev.payload.get("map_id"),
            }
        elif ev.id == "text_box_close":
            if scene == "npc":
                self._dialogue_context = {"scene": "overworld"}
        elif ev.id == "battle_start":
            self._dialogue_context = {
                "scene": "battle",
                "is_trainer": ev.payload.get("is_trainer"),
                "enemy_first_species": ev.payload.get("enemy_first_species"),
            }
        elif ev.id == "battle_end":
            self._dialogue_context = {"scene": "overworld"}
        elif ev.id == "menu_open":
            # Remember where to return to.
            self._dialogue_context = {"scene": "menu", "from": scene}
        elif ev.id == "menu_close":
            prev = self._dialogue_context.get("from", "overworld")
            self._dialogue_context = {"scene": prev}
        elif ev.id == "map_loaded":
            # First time we load a real map after intro / boot, we're in the
            # overworld. Otherwise just bookkeep the map id.
            if scene in ("boot", "oak_intro"):
                self._dialogue_context = {
                    "scene": "overworld",
                    "map_id": ev.payload.get("map_id"),
                }
            elif scene == "overworld":
                self._dialogue_context["map_id"] = ev.payload.get("map_id")
        elif ev.id == "title_screen_shown":
            self._dialogue_context = {"scene": "title"}
        elif ev.id == "hall_of_fame_entered":
            # Once we've actually entered the Hall of Fame, the legitimate
            # credit-roll is allowed to fire credits_shown.
            self._post_game = True

    # ----- actions -----------------------------------------------------

    def press_button(
        self,
        button: str,
        count: int = 1,
        hold_frames: int = 5,
        release_frames: int = 5,
    ) -> list[Event]:
        self._require_active()
        with self._emu_lock:
            all_events: list[Event] = []
            for _ in range(count):
                raw = self.emulator.press_button(button, hold_frames=hold_frames, release_frames=release_frames)
                evs = self.parser.feed(raw, frame=self.emulator.frame)
                evs = self._filter_noise(evs)
                self._annotate_and_update_context(evs)
                self._publish_events(evs)
                all_events.extend(evs)
                poll_evs = self._maybe_poll_snapshot()
                poll_evs = self._filter_noise(poll_evs)
                self._annotate_and_update_context(poll_evs)
                self._publish_events(poll_evs)
                all_events.extend(poll_evs)
            self._record_action({"op": "press", "button": button, "count": count, "hold_frames": hold_frames, "release_frames": release_frames})
            self._append_events(all_events)
            self._persist_after_action()
            return all_events

    def wait(self, frames: int) -> list[Event]:
        self._require_active()
        if frames <= 0:
            return []
        # Run in chunks of poll-interval so polling fires on schedule even on
        # very long waits.
        with self._emu_lock:
            all_events: list[Event] = []
            remaining = frames
            chunk = max(self.meta.snapshot_interval_frames, 1) if self.meta.snapshot_interval_frames else frames
            while remaining > 0:
                take = min(remaining, chunk)
                raw = self.emulator.step(take)
                evs = self.parser.feed(raw, frame=self.emulator.frame)
                evs = self._filter_noise(evs)
                self._annotate_and_update_context(evs)
                self._publish_events(evs)
                all_events.extend(evs)
                poll_evs = self._maybe_poll_snapshot()
                poll_evs = self._filter_noise(poll_evs)
                self._annotate_and_update_context(poll_evs)
                self._publish_events(poll_evs)
                all_events.extend(poll_evs)
                remaining -= take
            self._record_action({"op": "wait", "frames": frames})
            self._append_events(all_events)
            self._persist_after_action()
            return all_events

    # The viewer drives /tick at ~30 Hz to make the game animate without
    # waiting for the user to click. Persisting current.state on every call
    # would churn the disk, so we throttle to once per second.
    _TICK_PERSIST_INTERVAL_S = 1.0

    def tick_observation(self, frames: int, target_fps: float = 60.0) -> list[Event]:
        """Advance the game without recording an action.

        Same effect on game state as `wait`, but:
          * no entry in actions.jsonl (avoids cluttering the agent's record
            with 30 Hz viewer ticks)
          * current.state is persisted at most once per second
          * snapshot polling still fires
          * events are still appended to events.jsonl + published to subscribers
          * wall-clock paced to `target_fps` game-frames per real second
            (so the game runs at consistent speed regardless of how often the
            client polls /tick or how fast the host CPU is)
        """
        self._require_active()
        if frames <= 0:
            return []
        # Pace: each call should take at least frames/target_fps seconds of
        # wall time. If the client calls us faster than that, sleep the rest.
        # If they call slower (e.g. after a pause), we don't try to catch up —
        # just resume real-time from now.
        if target_fps > 0:
            target_interval = frames / target_fps
            now = time.monotonic()
            last = getattr(self, "_last_tick_wallclock", 0.0)
            elapsed = now - last
            if elapsed < target_interval:
                time.sleep(target_interval - elapsed)
            self._last_tick_wallclock = time.monotonic()
        with self._emu_lock:
            raw = self.emulator.step(frames)
            evs = self.parser.feed(raw, frame=self.emulator.frame)
            evs = self._filter_noise(evs)
            self._annotate_and_update_context(evs)
            self._publish_events(evs)
            poll_evs = self._maybe_poll_snapshot()
            poll_evs = self._filter_noise(poll_evs)
            self._annotate_and_update_context(poll_evs)
            self._publish_events(poll_evs)
            all_events = list(evs) + list(poll_evs)
            if all_events:
                self._append_events(all_events)
            # Throttled persist
            now = time.monotonic()
            last = getattr(self, "_last_tick_persist", 0.0)
            if now - last >= self._TICK_PERSIST_INTERVAL_S:
                self._persist_after_action()
                self._last_tick_persist = now
            else:
                self._last_touched = now
            return all_events

    def read_screen(self) -> "tuple[Any, int]":
        """Read the current framebuffer under the emulator lock.

        Returns (ndarray, frame). Callers (the /screen route, the MJPEG
        stream) should use this rather than reaching into self.emulator
        directly so that screen reads are serialized with ticks.
        """
        self._require_active()
        with self._emu_lock:
            return self.emulator.get_screen(), self.emulator.frame

    def snapshot_now(self) -> Snapshot:
        """Force a fresh snapshot pull. Does not affect polling cadence."""
        self._require_active()
        with self._emu_lock:
            snap = request_snapshot(self.emulator, self.parser, timeout_frames=120)
            # The snapshot Event was already emitted by the parser; capture it too
            # in the persistent log via the next persist.
            self._write_snapshot(snap)
            # request_snapshot may have generated other events too — drain & log them.
            return snap

    def get_snapshot_history(
        self,
        since_frame: int = 0,
        until_frame: int | None = None,
        limit: int = 100,
        fields: Iterable[str] | None = None,
    ) -> list[dict]:
        """Read polled snapshots from disk. Filenames are zero-padded by frame
        so a lexicographic listing is already in frame order."""
        snaps_dir = self.folder / "snapshots"
        if not snaps_dir.exists():
            return []
        until_frame = until_frame if until_frame is not None else float("inf")
        results: list[dict] = []
        for path in sorted(snaps_dir.glob("*.json")):
            try:
                frame = int(path.stem)
            except ValueError:
                continue
            if frame < since_frame or frame > until_frame:
                continue
            data = json.loads(path.read_text())
            if fields:
                projected = {"frame": data.get("frame", frame)}
                for f in fields:
                    if f in data:
                        projected[f] = data[f]
                data = projected
            results.append(data)
            if len(results) >= limit:
                break
        return results

    def set_polling_interval(self, frames: int) -> int:
        """Update polling interval. Returns the previous value. Persists to meta.json."""
        if frames < 0:
            raise ValueError("interval_frames must be >= 0")
        prev = self.meta.snapshot_interval_frames
        self.meta.snapshot_interval_frames = frames
        self._write_meta()
        # If we increase the interval, push the next-poll-frame forward.
        if frames:
            self._last_poll_frame = (self.emulator.frame // frames) * frames
        return prev

    # ----- save slots --------------------------------------------------

    def save_named_state(self, name: str) -> Path:
        self._require_active()
        _validate_name(name)
        with self._emu_lock:
            path = self.folder / "saves" / f"{name}.state"
            self.emulator.save_state(path)
            self._record_action({"op": "save_named", "name": name})
            self._persist_after_action()
            return path

    def load_named_state(self, name: str) -> None:
        self._require_active()
        _validate_name(name)
        path = self.folder / "saves" / f"{name}.state"
        if not path.exists():
            raise FileNotFoundError(f"no such save slot: {name}")
        with self._emu_lock:
            self.emulator.load_state(path)
            self.parser.reset()
            self._record_action({"op": "load_named", "name": name})
            self._persist_after_action()

    def list_named_states(self) -> list[str]:
        saves_dir = self.folder / "saves"
        if not saves_dir.exists():
            return []
        return sorted(p.stem for p in saves_dir.glob("*.state"))

    # ----- text / event search ----------------------------------------

    def search_text(self, query: str, case_sensitive: bool = False, limit: int = 100) -> list[Event]:
        """Search `text_display` payloads in events.jsonl."""
        events_path = self.folder / "events.jsonl"
        if not events_path.exists():
            return []
        needle = query if case_sensitive else query.lower()
        results: list[Event] = []
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
                    results.append(Event(
                        id=obj["id"],
                        category=obj.get("category", "display"),
                        payload=payload,
                        frame=obj.get("frame", 0),
                    ))
                    if len(results) >= limit:
                        break
        return results

    # ----- journal -----------------------------------------------------

    def append_journal(self, text: str) -> None:
        self._require_active()
        with (self.folder / "journal.md").open("a") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")

    def read_journal(self) -> str:
        return (self.folder / "journal.md").read_text()

    # ----- lifecycle ---------------------------------------------------

    def end(self) -> None:
        if self.ended:
            return
        self.meta.status = "ended"
        self.meta.last_activity = _now_iso()
        self.meta.current_frame = self.emulator.frame
        self._write_meta()
        self.emulator.save_state(self.folder / "current.state")
        self.emulator.close()

    def close(self) -> None:
        """Drop in-memory resources without ending the session."""
        try:
            self.emulator.close()
        except Exception:
            pass

    # ----- subscription (used by Task 07 SSE) --------------------------

    def subscribe_events(self) -> "asyncio.Queue[Event]":
        """Returns a queue. Must be called from inside an asyncio loop —
        captures the running loop so the publisher (which may run in a
        worker thread) can hand events back across the thread boundary."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.append((loop, q))
        return q

    def unsubscribe_events(self, q: "asyncio.Queue[Event]") -> None:
        self._subscribers = [(l, qq) for (l, qq) in self._subscribers if qq is not q]

    def _publish_events(self, events: Iterable[Event]) -> None:
        if not self._subscribers:
            return
        events_list = list(events)
        if not events_list:
            return
        dead: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        for loop, q in list(self._subscribers):
            try:
                for ev in events_list:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:
                # Loop is closed — drop this subscriber.
                dead.append((loop, q))
        if dead:
            self._subscribers = [(l, q) for (l, q) in self._subscribers if (l, q) not in dead]

    # ----- persistence -------------------------------------------------

    def _persist_after_action(self) -> None:
        self.meta.current_frame = self.emulator.frame
        self.meta.last_activity = _now_iso()
        self._write_meta()
        self.emulator.save_state(self.folder / "current.state")
        self._last_touched = time.monotonic()

    def _write_meta(self) -> None:
        meta_path = self.folder / "meta.json"
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.meta.to_dict(), indent=2, sort_keys=True))
        tmp.replace(meta_path)

    def _append_events(self, events: Iterable[Event]) -> None:
        events = list(events)
        if not events:
            return
        with (self.folder / "events.jsonl").open("a") as f:
            for ev in events:
                f.write(json.dumps(ev.to_dict()) + "\n")

    def _record_action(self, action: dict) -> None:
        record = {"ts": _now_iso(), "frame": self.emulator.frame, **action}
        with (self.folder / "actions.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")

    # Polling-driven snapshot requests should not significantly perturb frame
    # pacing. `PollSnapshot` runs every iteration of `OverworldLoop` — when
    # we're actually in the overworld, the snapshot comes back in 1-2 frames.
    # Anywhere else (intro, battles, menus) the engine never visits the poll
    # site, so we just bail after a few frames rather than burning a big budget.
    _POLL_SNAPSHOT_TIMEOUT_FRAMES = 8

    def _maybe_poll_snapshot(self) -> list[Event]:
        """Pull a snapshot if we've crossed the polling boundary since last time."""
        interval = self.meta.snapshot_interval_frames
        if interval <= 0:
            return []
        cur = self.emulator.frame
        if cur - self._last_poll_frame < interval:
            return []
        # Crossed the boundary — request a snapshot. If we're in a state where
        # PollSnapshot doesn't run (intro, battle, menu), request_snapshot will
        # time out quickly; treat that as a soft failure and skip polling for now.
        try:
            snap = request_snapshot(
                self.emulator, self.parser,
                timeout_frames=self._POLL_SNAPSHOT_TIMEOUT_FRAMES,
            )
        except TimeoutError:
            # Don't try again immediately — defer to next interval boundary.
            self._last_poll_frame = cur
            return []
        # The snapshot event is already buffered inside the parser; pull it back
        # out so we can write it.
        self._write_snapshot(snap)
        self._last_poll_frame = cur
        # Produce a synthetic Event so callers see polling happened.
        return [Event(
            id="snapshot",
            category="meta",
            payload={"map_id": snap.map_id, "frame": snap.frame, "polled": True},
            frame=snap.frame,
        )]

    def _write_snapshot(self, snap: Snapshot) -> None:
        path = self.folder / "snapshots" / f"{snap.frame:0{_FRAME_PAD}d}.json"
        path.write_text(json.dumps(snap.to_dict(), indent=2))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SessionRegistry:
    """In-memory cache of open Sessions plus a TTL-based eviction policy."""

    def __init__(
        self,
        base_dir: Path | str,
        ttl_seconds: int = 600,
        rom_path: str | None = None,
        window: str = "null",
        use_process: bool = False,
    ):
        self.base_dir = Path(base_dir)
        self.ttl_seconds = ttl_seconds
        self.default_rom_path = rom_path  # may be None; passed explicitly to create()
        self.window = window
        self.use_process = use_process
        self._cache: dict[str, Session] = {}
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        rom_path: str | None = None,
        name: str | None = None,
        snapshot_interval_frames: int = DEFAULT_POLL_INTERVAL_FRAMES,
    ) -> Session:
        rom = rom_path or self.default_rom_path
        if not rom:
            raise ValueError("rom_path required (registry has no default)")
        session = Session.create(
            self.base_dir,
            rom,
            name=name,
            snapshot_interval_frames=snapshot_interval_frames,
            window=self.window,
            use_process=self.use_process,
        )
        self._cache[session.session_id] = session
        return session

    def get_or_load(self, session_id: str) -> Session:
        self.evict_idle()
        if session_id in self._cache:
            session = self._cache[session_id]
            session._last_touched = time.monotonic()
            return session
        folder = self.base_dir / session_id
        if not folder.exists():
            raise KeyError(session_id)
        session = Session.open(folder, window=self.window, use_process=self.use_process)
        self._cache[session_id] = session
        return session

    def list(self) -> list[SessionMeta]:
        results: list[SessionMeta] = []
        if not self.base_dir.exists():
            return results
        for sub in sorted(self.base_dir.iterdir()):
            meta_path = sub / "meta.json"
            if not meta_path.exists():
                continue
            try:
                results.append(SessionMeta.from_dict(json.loads(meta_path.read_text())))
            except Exception as e:
                log.warning("failed to read meta for %s: %s", sub, e)
        return results

    def end_session(self, session_id: str) -> None:
        session = self.get_or_load(session_id)
        session.end()
        # Drop from cache so a future call surfaces the ended status from disk.
        self._cache.pop(session_id, None)

    def delete_session(self, session_id: str) -> None:
        # Drop from cache first so file handles release.
        if session_id in self._cache:
            self._cache[session_id].close()
            del self._cache[session_id]
        folder = self.base_dir / session_id
        if folder.exists():
            _rmtree(folder)

    def evict_idle(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for sid in [k for k, v in self._cache.items() if v._last_touched < cutoff]:
            try:
                self._cache[sid].close()
            except Exception:
                pass
            del self._cache[sid]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_name(name: str) -> None:
    if not SESSION_NAME_RE.match(name):
        raise ValueError(
            f"invalid name {name!r}: must be lowercase kebab-case (a-z, 0-9, hyphens)"
        )


def _pick_session_id(base_dir: Path, requested: str | None) -> str:
    """Pick a unique session_id, taking caller's `requested` first if given."""
    if requested:
        _validate_name(requested)
        candidate = requested
        i = 2
        while (base_dir / candidate).exists():
            candidate = f"{requested}-{i}"
            i += 1
        return candidate
    # adjective-noun-hex4
    while True:
        adj = secrets.choice(_ADJECTIVES)
        noun = secrets.choice(_NOUNS)
        suffix = secrets.token_hex(2)
        candidate = f"{adj}-{noun}-{suffix}"
        if not (base_dir / candidate).exists():
            return candidate


def _rmtree(path: Path) -> None:
    """Minimal rmtree that handles read-only files."""
    if path.is_file() or path.is_symlink():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    for child in path.iterdir():
        _rmtree(child)
    try:
        path.rmdir()
    except OSError:
        # If something else is using the directory (unlikely on POSIX), let it surface.
        raise
