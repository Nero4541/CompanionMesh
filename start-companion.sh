#!/bin/sh
# Start the Companion Server and open its admin dashboard (Linux; Windows: start-companion.bat).
# The server runs in this terminal (Ctrl+C stops it). If it is already running,
# this only opens the dashboard.
set -eu
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv was not found. Install it from https://docs.astral.sh/uv/ and run this again." >&2
  exit 1
fi

# The address the server listens on, from configs/companion.yaml (+ .local.yaml).
if ! URL=$(uv run python -c "from companion.core.config import load_config; s = load_config().server; h = '127.0.0.1' if s.host in ('0.0.0.0', '::', '') else s.host; h = f'[{h}]' if ':' in h else h; print(f'http://{h}:{s.port}')"); then
  echo "Could not read the configuration. Run \"uv run companion check\" to see why." >&2
  exit 1
fi

answers() {
  command -v curl >/dev/null 2>&1 && curl -s -f -o /dev/null "$URL/health"
}

open_dashboard() {
  echo "Dashboard: $URL/admin/"
  # Only with a desktop session; on a headless server, open the URL from another machine.
  if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL/admin/" >/dev/null 2>&1 || true
  fi
}

if answers; then
  echo "Companion Server is already running at $URL"
  open_dashboard
  exit 0
fi

echo "Starting Companion Server at $URL ..."
# Open the dashboard once the server answers (the first start can take a while
# to load speech recognition), while the server itself runs in the foreground.
(
  i=0
  while [ "$i" -lt 120 ]; do
    if answers; then
      open_dashboard
      exit 0
    fi
    sleep 1
    i=$((i + 1))
  done
) &
watcher=$!
status=0
uv run companion || status=$?
kill "$watcher" 2>/dev/null || true
exit "$status"
