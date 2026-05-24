"""PyBoy wrapper for the Pokemon Red instrumented ROM.

Captures the telemetry byte stream by hooking the entry of EmitEventByte in
ROM bank 0. Exposes a small synchronous API for the higher layers (sessions,
HTTP server) to drive the game and observe state.

The capture point is a PyBoy execution hook — PyBoy 2.x's serial stub does not
actually intercept rSB writes (it pins SB to 0xFF), so we read register A at
EmitEventByte's first instruction instead. That's the authoritative point.
"""
from __future__ import annotations

import hashlib
import io
import logging
import struct
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    from pyboy import PyBoy
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "PyBoy is required. Install with `pip install -r requirements.txt`."
    ) from e


# Built from `pokered.sym` after `make LLM_TELEMETRY=1`. These are the
# instrumented build's symbols; the unmodified ROM does not have them.
EMIT_EVENT_BYTE_BANK = 0x00
EMIT_EVENT_BYTE_ADDR = 0x00BE
WSNAPSHOT_REQUEST_ADDR = 0xDEE2

# Buttons accepted by PyBoy's `button()` / `button_press()` methods.
VALID_BUTTONS = frozenset({"a", "b", "up", "down", "left", "right", "start", "select"})


log = logging.getLogger(__name__)


class Emulator:
    """Wraps a PyBoy instance with telemetry capture and a button-input helper.

    Telemetry is captured into an internal buffer; calls to `step()` and
    `press_button()` return the bytes captured during their execution.
    """

    def __init__(
        self,
        rom_path: str | Path,
        expected_sha1: str | None = None,
        window: str = "null",
        sound: bool = False,
        log_level: str = "ERROR",
    ):
        rom_path = str(rom_path)
        if expected_sha1:
            actual = _sha1(rom_path)
            if actual.lower() != expected_sha1.lower():
                raise ValueError(
                    f"ROM SHA-1 mismatch for {rom_path}: expected {expected_sha1}, "
                    f"got {actual}"
                )

        # `sound_emulated=False` prevents PyBoy from emulating the sound chip
        # at all, which (a) skips work we don't need and (b) avoids the
        # `Buffer overrun!` CRITICAL spam that fires when the sound chip
        # generates samples that nothing reads. Crucially, this leaves the
        # `tick(..., sound=...)` parameter alone, so audio-clock interrupts
        # the game relies on for timing still fire correctly.
        pyboy_kwargs = dict(
            window=window,
            sound=sound,
            log_level=log_level,
        )
        try:
            self._pyboy = PyBoy(rom_path, sound_emulated=False, **pyboy_kwargs)
        except TypeError:
            # Older PyBoy versions don't accept sound_emulated; fall back.
            self._pyboy = PyBoy(rom_path, **pyboy_kwargs)
        # We always pass render=True to tick() — even in headless mode, so the
        # framebuffer reflects the current game state and `/screen` returns
        # something useful. The CPU cost is small (just pixel composition into
        # an in-memory buffer; no GPU).
        self._render = True
        # Buffer of bytes captured since the last drain.
        self._buffer = bytearray()
        # Track current button state so press_button can press/release symmetrically.
        self._pressed: set[str] = set()
        # PyBoy's frame_count is monotonic wall-clock and is NOT part of the
        # save state. We track a logical frame separately so save/load can
        # round-trip a session's notion of "current frame." `_frame_offset` is
        # added to pyboy.frame_count to produce the logical frame.
        self._frame_offset = 0
        self._hook_handle = self._install_hook()

    # --- lifecycle ------------------------------------------------------

    def close(self) -> None:
        try:
            self._pyboy.stop(save=False)
        except Exception:
            pass

    def __enter__(self) -> "Emulator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- properties -----------------------------------------------------

    @property
    def frame(self) -> int:
        """Logical frame counter. Persisted across save/load."""
        return int(self._pyboy.frame_count) + self._frame_offset

    @property
    def pyboy(self) -> PyBoy:
        """Underlying PyBoy instance. Use sparingly — prefer the wrapper methods."""
        return self._pyboy

    # --- emulation ------------------------------------------------------

    def step(self, frames: int = 1) -> bytes:
        """Advance N frames, return any telemetry bytes captured during them.

        The capture hook fires synchronously during tick() execution, so by the
        time this call returns the buffer reflects everything emitted.
        """
        if frames < 1:
            return b""
        self._pyboy.tick(frames, True)
        return self._drain_buffer()

    def press_button(
        self,
        button: str,
        hold_frames: int = 5,
        release_frames: int = 5,
    ) -> bytes:
        """Press, hold for `hold_frames`, release, then run `release_frames` more.

        Returns telemetry bytes captured throughout the whole sequence.
        """
        button = button.lower()
        if button not in VALID_BUTTONS:
            raise ValueError(
                f"invalid button {button!r}; must be one of {sorted(VALID_BUTTONS)}"
            )
        # Drain anything stale so the returned bytes are press-scoped only.
        out = bytearray()
        out += self._drain_buffer()
        self._pyboy.button_press(button)
        self._pressed.add(button)
        try:
            self._pyboy.tick(max(1, hold_frames), True)
            out += self._drain_buffer()
        finally:
            self._pyboy.button_release(button)
            self._pressed.discard(button)
        self._pyboy.tick(max(1, release_frames), True)
        out += self._drain_buffer()
        return bytes(out)

    def press_buttons(self, buttons: Iterable[str], hold_frames: int = 5, release_frames: int = 5) -> bytes:
        """Press a sequence of buttons one after another."""
        out = bytearray()
        for b in buttons:
            out += self.press_button(b, hold_frames=hold_frames, release_frames=release_frames)
        return bytes(out)

    # --- screen ---------------------------------------------------------

    def get_screen(self) -> np.ndarray:
        """Current screen as an (H, W, 3) uint8 RGB numpy array."""
        rgba = self._pyboy.screen.ndarray  # (144, 160, 4) RGBA
        return rgba[:, :, :3].copy()

    # --- RAM ------------------------------------------------------------

    def read_ram(self, addr: int, length: int = 1) -> bytes:
        """Read `length` bytes from memory starting at `addr`.

        Works for WRAM/HRAM. PyBoy's memory view spans the full 64KB address
        space, so high RAM (0xFF80+) and WRAM (0xC000+) are both reachable.
        """
        end = addr + length
        return bytes(self._pyboy.memory[addr:end])

    def write_ram(self, addr: int, data: bytes | int) -> None:
        """Write bytes (or a single int) to memory."""
        if isinstance(data, int):
            self._pyboy.memory[addr] = data & 0xFF
            return
        for i, b in enumerate(data):
            self._pyboy.memory[addr + i] = b

    # --- state ----------------------------------------------------------
    #
    # Save format: 16-byte header + raw PyBoy state.
    #   bytes 0-3: magic "POK1"
    #   bytes 4-11: logical frame (uint64 little-endian)
    #   bytes 12-15: reserved (zeroed)
    # The header lets us restore the logical frame counter on load; PyBoy
    # itself doesn't include frame_count in its serialization.

    _STATE_MAGIC = b"POK1"
    _STATE_HEADER_LEN = 16

    def save_state(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.save_state_bytes()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def load_state(self, path: str | Path) -> None:
        data = Path(path).read_bytes()
        self.load_state_bytes(data)

    def save_state_bytes(self) -> bytes:
        buf = io.BytesIO()
        self._pyboy.save_state(buf)
        body = buf.getvalue()
        header = self._STATE_MAGIC + struct.pack("<Q", self.frame) + b"\x00" * 4
        return header + body

    def load_state_bytes(self, data: bytes) -> None:
        if data[: len(self._STATE_MAGIC)] == self._STATE_MAGIC:
            saved_frame = struct.unpack("<Q", data[4:12])[0]
            body = data[self._STATE_HEADER_LEN :]
        else:
            # Older / raw PyBoy state without our header — accept it but the
            # logical frame counter just continues from current pyboy.frame_count.
            saved_frame = int(self._pyboy.frame_count)
            body = data
        self._pyboy.load_state(io.BytesIO(body))
        # PyBoy's save state does not include the framebuffer, so the screen
        # we see after load is whatever PyBoy had in memory (often all-white
        # garbage). Tick one frame with render=True so the framebuffer
        # reflects the loaded game state, then re-anchor the logical frame
        # counter so it still equals saved_frame.
        self._pyboy.tick(1, True)
        self._frame_offset = saved_frame - int(self._pyboy.frame_count)
        # Drop telemetry bytes from the repaint tick AND from anything before
        # the saved state; the next action starts with a clean buffer.
        self._buffer.clear()

    # --- internal -------------------------------------------------------

    def _drain_buffer(self) -> bytes:
        if not self._buffer:
            return b""
        out = bytes(self._buffer)
        self._buffer.clear()
        return out

    def _install_hook(self):
        # Capture register A on entry to EmitEventByte. PyBoy hook_register
        # signature: (bank, addr, callback, context). The callback receives
        # `context` so we don't have to use a closure, but a closure is
        # simpler here and the overhead is fine for this use case.
        pyboy = self._pyboy
        buf = self._buffer

        def _on_emit(_ctx):
            buf.append(int(pyboy.register_file.A))

        return pyboy.hook_register(
            EMIT_EVENT_BYTE_BANK, EMIT_EVENT_BYTE_ADDR, _on_emit, None
        )


def _sha1(path: str | Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rom_sha1(path: str | Path) -> str:
    """Convenience: return the SHA-1 of `path` as a hex string."""
    return _sha1(path)
