#!/usr/bin/env bash
# Start/stop the Receipt OCR API (plus the email watcher, if IMAP_* is set in .env).
#
#   ./run.sh              start in the background (same as ./run.sh start)
#   ./run.sh stop         stop it
#   ./run.sh restart      stop, then start
#   ./run.sh status       is it running?
#   ./run.sh logs         follow process.log
#   ./run.sh fg           run in the foreground (Ctrl+C to stop)
#   ./run.sh setup        only create .venv / install requirements, do not start
#
# Once started it keeps running after the terminal/SSH session is closed, and
# if the server crashes or exits for any reason it is started again - it only
# stays down after "./run.sh stop". To also come back after a reboot, add this
# line to "crontab -e":
#   @reboot /full/path/to/run.sh start
#
# First run creates .venv and installs requirements.txt into it; after that
# requirements are only reinstalled when requirements.txt changes.
#
# Env overrides: HOST (default 0.0.0.0), PORT (default 8000).
#
# Always runs a single uvicorn worker: the job queue, ticket store and email
# poller all live in process memory, so a second worker would poll the same
# mailbox again and not see the first worker's tickets.
set -uo pipefail

# cache/, pending/, process.log and mailbox_state.json are relative paths -
# run from the project folder no matter where the script is called from
cd "$(dirname "$0")"
SCRIPT="$(pwd)/$(basename "$0")"

PID_FILE=run.pid
RUN_LOG=run.log                      # restarts + anything printed outside logging (crash tracebacks)
RUN_LOG_MAX_BYTES=$((10 * 1024 * 1024))
MAX_RESTART_DELAY=60                 # seconds; crash-loop backoff tops out here
VENV=.venv
REQ_STAMP=$VENV/.requirements.cksum  # checksum of the requirements.txt last installed

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [run.sh] $*" >> "$RUN_LOG"; }

is_running() {
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

venv_python() {
    if [ -x "$VENV/bin/python" ]; then
        echo "$VENV/bin/python"            # Linux / macOS
    elif [ -x "$VENV/Scripts/python.exe" ]; then
        echo "$VENV/Scripts/python.exe"    # Windows (Git Bash)
    else
        return 1
    fi
}

find_base_python() {
    local cmd
    for cmd in python3 python "py -3"; do
        # 3.10+: model.py uses "X | Y" type hints
        if $cmd -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            echo "$cmd"
            return 0
        fi
    done
    return 1
}

# Creates .venv if it's missing (or broken), and installs requirements.txt
# into it on first run and again whenever requirements.txt changes.
setup() {
    if [ ! -f .env ]; then
        echo "No .env found - copy .env.example to .env and fill it in first." >&2
        exit 1
    fi

    local py base want have=""
    if ! py=$(venv_python) || ! "$py" -c '' >/dev/null 2>&1; then
        if [ -d "$VENV" ]; then
            echo "$VENV is broken (or was made on another OS) - recreating it."
            rm -rf "$VENV"
        fi
        base=$(find_base_python) || {
            echo "Python 3.10 or newer not found - install it first." >&2
            exit 1
        }
        echo "Creating $VENV with $($base --version 2>&1)..."
        if ! $base -m venv "$VENV" || ! py=$(venv_python); then
            rm -rf "$VENV"
            echo "Could not create $VENV (on Ubuntu/Debian: sudo apt install python3-venv)." >&2
            exit 1
        fi
    fi

    want=$(cksum < requirements.txt)
    [ -f "$REQ_STAMP" ] && have=$(cat "$REQ_STAMP")
    if [ "$want" != "$have" ]; then
        echo "Installing requirements.txt into $VENV..."
        "$py" -m pip install --upgrade pip >/dev/null 2>&1 || true
        if ! "$py" -m pip install -r requirements.txt; then
            echo "pip install failed - fix the error above and run ./run.sh again." >&2
            exit 1
        fi
        echo "$want" > "$REQ_STAMP"
        echo "Requirements installed."
    fi
}

# Runs uvicorn and starts it again whenever it exits, until told to stop.
supervise() {
    PYTHON=$(venv_python) || {
        log "no $VENV found - start with ./run.sh so it gets created"
        exit 1
    }
    echo $$ > "$PID_FILE"
    export LOG_TO_CONSOLE=false            # everything already goes to process.log

    local child="" stopping=0 delay=5 started code
    trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT
    trap '' HUP                            # closing the terminal must not stop it

    while [ "$stopping" = 0 ]; do
        if [ -f "$RUN_LOG" ] && [ "$(wc -c < "$RUN_LOG")" -gt "$RUN_LOG_MAX_BYTES" ]; then
            : > "$RUN_LOG"
        fi
        log "starting server on ${HOST:-0.0.0.0}:${PORT:-8000}"
        started=$(date +%s)
        "$PYTHON" -m uvicorn main:app \
            --host "${HOST:-0.0.0.0}" \
            --port "${PORT:-8000}" \
            --workers 1 \
            --no-access-log \
            >> "$RUN_LOG" 2>&1 &
        child=$!
        # a trapped signal interrupts wait - keep waiting until uvicorn has
        # actually finished its graceful shutdown
        while kill -0 "$child" 2>/dev/null; do
            wait "$child"
            code=$?
        done
        child=""
        [ "$stopping" = 1 ] && break

        # ran for a while -> quick restart; died straight away (bad .env,
        # port in use...) -> back off so it doesn't spin
        if [ $(( $(date +%s) - started )) -gt 300 ]; then
            delay=5
        fi
        log "server exited (code $code), restarting in ${delay}s"
        sleep "$delay" &
        child=$!                           # so a stop also kills the wait
        wait "$child"
        child=""
        delay=$(( delay * 2 > MAX_RESTART_DELAY ? MAX_RESTART_DELAY : delay * 2 ))
    done

    log "stopped"
    rm -f "$PID_FILE"
}

start() {
    if is_running; then
        echo "Already running (pid $(cat "$PID_FILE"))."
        return 0
    fi
    rm -f "$PID_FILE"
    setup
    if command -v setsid >/dev/null; then
        nohup setsid bash "$SCRIPT" _supervise > /dev/null 2>&1 < /dev/null &
    else
        nohup bash "$SCRIPT" _supervise > /dev/null 2>&1 < /dev/null &
    fi
    for _ in 1 2 3 4 5; do
        sleep 1
        if is_running; then
            echo "Started (pid $(cat "$PID_FILE")) on port ${PORT:-8000}."
            echo "Logs: ./run.sh logs   (restarts/crashes: $RUN_LOG)"
            return 0
        fi
    done
    echo "Failed to start - check $RUN_LOG" >&2
    return 1
}

stop() {
    if ! is_running; then
        echo "Not running."
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    kill -TERM "$pid"
    for _ in $(seq 1 30); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "Stopped."
            return 0
        fi
        sleep 1
    done
    echo "Did not stop within 30s - force killing."
    pkill -KILL -P "$pid" 2>/dev/null
    kill -KILL "$pid" 2>/dev/null
    rm -f "$PID_FILE"
}

status() {
    if is_running; then
        echo "Running (pid $(cat "$PID_FILE"))."
    else
        echo "Not running."
        return 1
    fi
}

case "${1:-start}" in
    start)       start ;;
    stop)        stop ;;
    restart)     stop; start ;;
    status)      status ;;
    logs)        tail -n 100 -f process.log ;;
    fg)          setup && supervise ;;
    setup)       setup && echo "Setup done." ;;
    _supervise)  supervise ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|fg|setup}" >&2
        exit 1
        ;;
esac
