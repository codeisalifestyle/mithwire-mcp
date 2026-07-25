#!/usr/bin/env bash
# Dev entrypoint: Xvfb + x11vnc/noVNC + mithwire-mcp on TCP.
# Used only in the local dev container (docker compose -f docker-compose.dev.yml up).
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_RES="${SCREEN_RES:-1920x1080x24}"
MCP_PORT="${MCP_PORT:-7867}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
STATE_ROOT="${MCP_STATE_ROOT:-/data}"

export DISPLAY=":${DISPLAY_NUM}"

mkdir -p "$STATE_ROOT"

echo "[dev] starting Xvfb on :${DISPLAY_NUM} (${SCREEN_RES})"
Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_RES}" -ac +extension GLX &
XVFB_PID=$!
sleep 1

echo "[dev] starting x11vnc on port ${VNC_PORT}"
x11vnc -display ":${DISPLAY_NUM}" -rfbport "${VNC_PORT}" -nopw -forever -shared -quiet &
X11VNC_PID=$!

echo "[dev] starting noVNC on port ${NOVNC_PORT} -> VNC ${VNC_PORT}"
websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
NOVNC_PID=$!

echo "[dev] starting mithwire-mcp on TCP port ${MCP_PORT}"
echo "[dev]   state root: ${STATE_ROOT}"
echo "[dev]   noVNC UI:   http://localhost:${NOVNC_PORT}/vnc.html"
echo ""

cleanup() {
    echo "[dev] shutting down..."
    kill "$XVFB_PID" "$X11VNC_PID" "$NOVNC_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec mithwire-mcp \
    --transport streamable-http \
    --port "${MCP_PORT}" \
    --host 0.0.0.0 \
    --state-root "${STATE_ROOT}"
