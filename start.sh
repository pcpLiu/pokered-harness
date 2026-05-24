#!/usr/bin/env bash
#
# One-shot bootstrap for the Pokemon Red AI Harness.
#
# Builds the instrumented ROM (if needed), creates a Python venv (if needed),
# installs deps, and launches the HTTP server with a visible PyBoy SDL window.
#
# Usage:
#     ./start.sh                       # headless + browser viewer at /viewer
#     ./start.sh --fresh               # wipe the auto-session folder first
#     ./start.sh --sdl                 # also open the PyBoy native SDL window
#     ./start.sh --no-browser          # don't auto-open the viewer
#     ./start.sh --port 8001           # custom port
#     ./start.sh --no-build            # skip the ROM rebuild step
#     ./start.sh --session-name foo    # override auto-created session name
#     ./start.sh --help                # show options
#
# Tip: if the viewer shows a blank screen after upgrading, hard-refresh the
# page (Cmd-Shift-R on macOS) to drop the cached HTML, or pass --fresh to
# discard the previously persisted session state.
#
# After it boots, point a browser at http://localhost:8000/viewer to watch the
# game and drive it with on-screen buttons (or arrow keys + z/x/Enter).
# Anything after `--` is forwarded to run_server.py.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# --- defaults ---------------------------------------------------------------
PORT=8000
HOST=127.0.0.1
# 'null' (headless) by default — the browser viewer at /viewer is the
# canonical UI. Pass --sdl to also pop the native PyBoy window; that mode
# automatically enables --process so the SDL event loop has a dedicated main
# thread (required on macOS).
WINDOW="null"
USE_PROCESS=0
SESSION_NAME="default"
SESSION_INTERVAL=60
DO_BUILD=1
OPEN_BROWSER=1
FRESH=0
EXTRA_ARGS=()

# --- arg parsing ------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            grep '^#' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        --headless)
            # alias kept for backward compatibility — current default
            WINDOW="null"
            shift
            ;;
        --sdl)
            WINDOW="SDL2"
            USE_PROCESS=1
            shift
            ;;
        --process)
            USE_PROCESS=1
            shift
            ;;
        --no-browser)
            OPEN_BROWSER=0
            shift
            ;;
        --fresh)
            FRESH=1
            shift
            ;;
        --port)
            PORT="$2"; shift 2
            ;;
        --host)
            HOST="$2"; shift 2
            ;;
        --no-build)
            DO_BUILD=0
            shift
            ;;
        --session-name)
            SESSION_NAME="$2"; shift 2
            ;;
        --session-interval)
            SESSION_INTERVAL="$2"; shift 2
            ;;
        --)
            shift
            EXTRA_ARGS+=("$@")
            break
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

echo "==> Pokemon Red AI Harness bootstrap"
echo "    root:      $ROOT"
echo "    window:    $WINDOW    (process mode: $( [[ $USE_PROCESS -eq 1 ]] && echo on || echo off ))"
echo "    address:   http://$HOST:$PORT"
echo "    session:   $SESSION_NAME (poll every $SESSION_INTERVAL frames)"

# --- step 1: build the ROM --------------------------------------------------
ROM="$ROOT/pokered-fork/pokered.gbc"
if [[ $DO_BUILD -eq 1 ]]; then
    echo ""
    echo "==> Building instrumented ROM (make LLM_TELEMETRY=1 pokered.gbc)..."
    if ! command -v rgbasm >/dev/null 2>&1; then
        echo "    error: rgbasm not found. Install rgbds (https://rgbds.gbdev.io/)."
        echo "    on macOS: brew install rgbds"
        exit 1
    fi
    pushd "$ROOT/pokered-fork" >/dev/null
    make LLM_TELEMETRY=1 pokered.gbc
    popd >/dev/null
fi

if [[ ! -f "$ROM" ]]; then
    echo "    error: $ROM not found. Re-run without --no-build."
    exit 1
fi
echo "    ROM ready: $ROM ($(shasum -a 1 "$ROM" | awk '{print $1}'))"

# Optionally wipe the auto-session folder for a clean boot. Useful when the
# server code changed and the persisted state has stale invariants.
if [[ $FRESH -eq 1 ]]; then
    if [[ -d "$ROOT/sessions/$SESSION_NAME" ]]; then
        echo "    --fresh: removing sessions/$SESSION_NAME"
        rm -rf "$ROOT/sessions/$SESSION_NAME"
    fi
fi

# --- step 2: Python venv ----------------------------------------------------
VENV="$ROOT/venv"
if [[ ! -d "$VENV" ]]; then
    echo ""
    echo "==> Creating Python venv at $VENV..."
    python3 -m venv "$VENV"
fi
PY="$VENV/bin/python3"

# Always check requirements are satisfied. Cheap if already installed.
if ! "$PY" -c "import pyboy, fastapi, uvicorn, sse_starlette" >/dev/null 2>&1; then
    echo ""
    echo "==> Installing Python dependencies..."
    "$VENV/bin/pip" install --quiet --upgrade pip
    "$VENV/bin/pip" install -r "$ROOT/requirements.txt"
fi

# --- step 3: start the server -----------------------------------------------
VIEWER_URL="http://$HOST:$PORT/viewer"
echo ""
echo "==> Starting harness server..."
echo ""
echo "    🎮  Open this in your browser to watch the game:"
echo "          $VIEWER_URL"
echo ""
echo "    Server endpoints:"
echo "      GET  http://$HOST:$PORT/                     — index"
echo "      GET  http://$HOST:$PORT/press                — route docs"
echo "      POST http://$HOST:$PORT/press                — press a button"
echo ""
echo "    Drive from the shell:"
echo "      curl -X POST http://$HOST:$PORT/press \\"
echo "        -H 'X-Session-Id: $SESSION_NAME' \\"
echo "        -H 'Content-Type: application/json' \\"
echo "        -d '{\"button\":\"start\"}'"
echo ""
echo "    Ctrl-C to stop."
echo ""

# Auto-open the viewer in the default browser after a short delay so it loads
# once the server is listening. Disable with --no-browser.
if [[ $OPEN_BROWSER -eq 1 ]]; then
    (
        sleep 1.5
        if command -v open >/dev/null 2>&1; then
            open "$VIEWER_URL"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$VIEWER_URL"
        fi
    ) &
fi

# The ${arr[@]+"${arr[@]}"} idiom is the bash-with-`set -u` safe way to expand
# an array that may be empty (a bare ${arr[@]} would trigger "unbound variable").
CMD=(
    "$PY" "$ROOT/scripts/run_server.py"
    --host "$HOST"
    --port "$PORT"
    --rom "$ROM"
    --base-dir "$ROOT/sessions"
    --window "$WINDOW"
    --auto-session "$SESSION_NAME"
    --auto-session-interval "$SESSION_INTERVAL"
)
if [[ $USE_PROCESS -eq 1 ]]; then
    CMD+=(--process)
fi

# Start the server as a child so we can clean up reliably on Ctrl-C.
# Ctrl-C delivers SIGINT to the foreground process group, so uvicorn gets it
# directly. The trap below is belt-and-suspenders: forwards the signal to the
# child, waits briefly, then escalates to SIGKILL if anything is still alive.
"${CMD[@]}" ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} &
SERVER_PID=$!

cleanup() {
    trap '' INT TERM            # disarm so we don't recurse
    echo ""
    echo "==> shutting down…"
    # Term the server process tree (server + any worker children).
    if kill -0 $SERVER_PID 2>/dev/null; then
        kill -TERM $SERVER_PID 2>/dev/null || true
    fi
    # Wait up to 3s for graceful exit, then force-kill anything still alive.
    for _ in 1 2 3 4 5 6; do
        if ! kill -0 $SERVER_PID 2>/dev/null; then break; fi
        sleep 0.5
    done
    if kill -0 $SERVER_PID 2>/dev/null; then
        echo "    forcing kill…"
        pkill -KILL -P $SERVER_PID 2>/dev/null || true
        kill -KILL $SERVER_PID 2>/dev/null || true
    fi
    exit 130
}
trap cleanup INT TERM

wait $SERVER_PID
