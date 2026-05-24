"""Out-of-process PyBoy worker.

Why: on macOS, an SDL window's event loop must run on the **main thread** of
its process. Uvicorn owns the main thread of the harness server, so a PyBoy
SDL window opened in the same process gets a bouncing Dock icon but never a
real responsive window. Putting PyBoy in a child process whose main thread
is dedicated to ticking the emulator + pumping SDL events fixes that.

The `EmulatorProcess` class exposes the same interface as `harness.emulator.Emulator`
so `Session` can use either interchangeably.

Wire protocol (over a `multiprocessing.Pipe`):

    request  = {"op": <str>, "args": <dict>}
    response = {"ok": True, ...result fields...}  or  {"ok": False, "error": <str>}

The worker's main loop polls the pipe with a short timeout, calls
`SDL_PumpEvents()` between polls so the OS keeps the window responsive even
when the game is paused waiting for the next command. The game itself does
NOT advance between commands — only `step()` and `press_button()` move frames.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import numpy as np

# Import emulator constants only — the actual Emulator class is imported
# inside the child process so PyBoy/SDL initialize there, not in the parent.
from .emulator import (
    EMIT_EVENT_BYTE_BANK,
    EMIT_EVENT_BYTE_ADDR,
    WSNAPSHOT_REQUEST_ADDR,
    VALID_BUTTONS,
)


log = logging.getLogger(__name__)


# Use spawn — fork on macOS is fragile (objc runtime can deadlock when forked
# from a multithreaded process, which is exactly what we'd hit if the parent
# is already running uvicorn).
_CTX = mp.get_context("spawn")


def _worker_main(conn: Connection, init_kwargs: dict) -> None:
    """Child process entry point.

    Creates a `harness.emulator.Emulator` and services commands until told to
    close. Between commands, pumps SDL events so the native window stays alive.
    """
    # Quiet PyBoy's noisy startup warnings in the child too.
    logging.getLogger("pyboy.pyboy").setLevel(logging.ERROR)

    try:
        from .emulator import Emulator
        emu = Emulator(**init_kwargs)
    except Exception as e:  # pragma: no cover - startup errors surface to parent
        import traceback
        conn.send({"ok": False, "error": str(e), "tb": traceback.format_exc()})
        return
    conn.send({"ok": True})

    # Best-effort SDL pump. If pysdl2 isn't importable (or window=null), this is a no-op.
    try:
        import sdl2  # type: ignore
        pump = sdl2.SDL_PumpEvents
    except Exception:
        def pump():  # type: ignore[misc]
            return None

    while True:
        # Poll with a small timeout so we get back to pumping SDL events
        # frequently even when no command is in flight.
        if conn.poll(timeout=0.016):
            try:
                msg = conn.recv()
            except EOFError:
                break
            op = msg.get("op")
            args = msg.get("args", {})
            try:
                if op == "step":
                    raw = emu.step(int(args.get("frames", 1)))
                    conn.send({"ok": True, "raw": bytes(raw)})
                elif op == "press_button":
                    raw = emu.press_button(
                        args["button"],
                        hold_frames=int(args.get("hold_frames", 5)),
                        release_frames=int(args.get("release_frames", 5)),
                    )
                    conn.send({"ok": True, "raw": bytes(raw)})
                elif op == "frame":
                    conn.send({"ok": True, "frame": emu.frame})
                elif op == "read_ram":
                    data = emu.read_ram(int(args["addr"]), int(args.get("length", 1)))
                    conn.send({"ok": True, "data": bytes(data)})
                elif op == "write_ram":
                    emu.write_ram(int(args["addr"]), args["data"])
                    conn.send({"ok": True})
                elif op == "get_screen":
                    arr = emu.get_screen()
                    conn.send({
                        "ok": True,
                        "data": arr.tobytes(),
                        "shape": tuple(arr.shape),
                        "dtype": str(arr.dtype),
                    })
                elif op == "save_state":
                    emu.save_state(args["path"])
                    conn.send({"ok": True})
                elif op == "load_state":
                    emu.load_state(args["path"])
                    conn.send({"ok": True})
                elif op == "save_state_bytes":
                    conn.send({"ok": True, "data": emu.save_state_bytes()})
                elif op == "load_state_bytes":
                    emu.load_state_bytes(args["data"])
                    conn.send({"ok": True})
                elif op == "close":
                    emu.close()
                    conn.send({"ok": True})
                    return
                else:
                    conn.send({"ok": False, "error": f"unknown op: {op}"})
            except Exception as e:
                import traceback
                conn.send({
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "tb": traceback.format_exc(),
                })
        else:
            # Idle — keep the window responsive on macOS.
            pump()


class EmulatorProcess:
    """Process-backed Emulator. Same interface as harness.emulator.Emulator.

    The child process owns PyBoy and (optionally) its SDL window. The parent
    holds only a pipe; calls are synchronous round-trips.
    """

    def __init__(
        self,
        rom_path: str | Path,
        expected_sha1: str | None = None,
        window: str = "null",
        sound: bool = False,
        log_level: str = "ERROR",
    ):
        self._parent_conn, child_conn = _CTX.Pipe(duplex=True)
        init_kwargs = dict(
            rom_path=str(rom_path),
            expected_sha1=expected_sha1,
            window=window,
            sound=sound,
            log_level=log_level,
        )
        self._proc = _CTX.Process(
            target=_worker_main,
            args=(child_conn, init_kwargs),
            daemon=True,
            name=f"emulator-{os.getpid()}-{int(time.monotonic()*1000)%10000}",
        )
        self._proc.start()
        child_conn.close()  # parent end only
        # Wait for the worker to confirm it's up.
        resp = self._parent_conn.recv()
        if not resp.get("ok"):
            tb = resp.get("tb", "")
            self.close()
            raise RuntimeError(
                f"emulator worker failed to start: {resp.get('error')}\n{tb}"
            )

    # --- request/reply ----------------------------------------------------

    def _call(self, op: str, **args) -> dict:
        if not self._proc.is_alive():
            raise RuntimeError("emulator worker process is no longer alive")
        self._parent_conn.send({"op": op, "args": args})
        resp = self._parent_conn.recv()
        if not resp.get("ok"):
            raise RuntimeError(
                f"emulator worker error during {op}: {resp.get('error')}"
                + (f"\n{resp.get('tb','')}" if resp.get('tb') else "")
            )
        return resp

    # --- lifecycle --------------------------------------------------------

    def close(self) -> None:
        if not hasattr(self, "_proc"):
            return
        try:
            if self._proc.is_alive():
                self._parent_conn.send({"op": "close", "args": {}})
                # Best-effort wait for ack
                try:
                    self._parent_conn.recv()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._proc.join(timeout=2.0)
        except Exception:
            pass
        if self._proc.is_alive():
            self._proc.terminate()
            try:
                self._proc.join(timeout=1.0)
            except Exception:
                pass
        try:
            self._parent_conn.close()
        except Exception:
            pass

    def __enter__(self) -> "EmulatorProcess":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # --- mirror Emulator API ---------------------------------------------

    @property
    def frame(self) -> int:
        return int(self._call("frame")["frame"])

    def step(self, frames: int = 1) -> bytes:
        if frames < 1:
            return b""
        return self._call("step", frames=frames)["raw"]

    def press_button(
        self,
        button: str,
        hold_frames: int = 5,
        release_frames: int = 5,
    ) -> bytes:
        button = button.lower()
        if button not in VALID_BUTTONS:
            raise ValueError(
                f"invalid button {button!r}; must be one of {sorted(VALID_BUTTONS)}"
            )
        return self._call(
            "press_button",
            button=button,
            hold_frames=hold_frames,
            release_frames=release_frames,
        )["raw"]

    def get_screen(self) -> np.ndarray:
        d = self._call("get_screen")
        arr = np.frombuffer(d["data"], dtype=np.dtype(d["dtype"])).reshape(d["shape"])
        return arr.copy()

    def read_ram(self, addr: int, length: int = 1) -> bytes:
        return self._call("read_ram", addr=addr, length=length)["data"]

    def write_ram(self, addr: int, data: bytes | int) -> None:
        if isinstance(data, int):
            data = bytes([data & 0xFF])
        self._call("write_ram", addr=addr, data=bytes(data))

    def save_state(self, path: str | Path) -> None:
        # Have the worker write the file directly so we don't ship bytes over
        # the pipe for what's usually a 168 KB blob.
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._call("save_state", path=str(path))

    def load_state(self, path: str | Path) -> None:
        self._call("load_state", path=str(Path(path)))

    def save_state_bytes(self) -> bytes:
        return self._call("save_state_bytes")["data"]

    def load_state_bytes(self, data: bytes) -> None:
        self._call("load_state_bytes", data=bytes(data))
