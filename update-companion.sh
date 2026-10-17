#!/bin/sh
# Update the Companion Server to the newest release (or git pull for a clone).
# Stop the server first. Your .env, local config, data and personas are kept;
# the replaced files are backed up in data/updates/. See docs/updating.md.
#
# Everything is inside main(), so the shell has read the whole script before the
# update replaces this file.

main() {
  set -u
  cd "$(dirname "$0")" || exit 1

  if ! command -v uv >/dev/null 2>&1; then
    echo "uv was not found. Install it from https://docs.astral.sh/uv/ and run this again." >&2
    exit 1
  fi

  uvargs_file=$(mktemp)
  status=0
  uv run --no-sync python -m companion.updater --extras-file "$uvargs_file" || status=$?
  if [ "$status" -eq 3 ]; then
    rm -f "$uvargs_file"
    exit 0
  fi
  if [ "$status" -ne 0 ]; then
    rm -f "$uvargs_file"
    echo "The update did not finish; nothing more was changed." >&2
    exit 1
  fi

  uvargs=$(cat "$uvargs_file" 2>/dev/null || true)
  rm -f "$uvargs_file"
  [ -n "$uvargs" ] || uvargs="--frozen"
  echo "Installing dependencies: uv sync $uvargs"
  sync=0
  # shellcheck disable=SC2086 # the arguments are separate words
  uv sync $uvargs || sync=$?
  uv run --no-sync python -m companion.updater --synced "$sync"
  if [ "$sync" -ne 0 ]; then
    echo "uv sync failed. Run it again by hand: uv sync $uvargs" >&2
    exit 1
  fi

  echo
  echo "Updated. Start the server again with ./start-companion.sh."
  echo "Boards with an older edge client: Devices, Maintenance, Update (in the dashboard)."
  exit 0
}

main "$@"
