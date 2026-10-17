# Updating

Stop the server (close its window, or Ctrl+C), then double-click **`update-companion.bat`** (on Linux, run `./update-companion.sh`) in the server's folder. It asks GitHub for the newest release and, if it is newer:

- downloads this system's package and checks it against the release's `SHA256SUMS` (nothing is changed if it does not match);
- keeps the files it is about to replace in `data/updates/backup-<version>-<time>.zip` (the last three are kept), writes the new ones and removes the files the new release no longer has;
- never touches what is yours: `.env`, `configs/*.local.yaml`, `data/`, `.venv/` and your own personas;
- installs the new dependencies with `uv sync`, keeping the optional ones you installed (speech recognition, CUDA, Opus).

Every update is logged in `data/updates.jsonl`. The dashboard's **Updates** page shows the version running, whether a newer release exists (with its notes), that log and the [changelog](../CHANGELOG.md). Afterwards, update your boards from the dashboard: Devices, a board, Maintenance, **Update**.

In a git clone, the script runs `git pull --ff-only` and `uv sync` instead. To go back, unpack a backup zip over the folder and run `uv sync --frozen` (with your `--extra` options). `COMPANION_UPDATE_API` points the update at another "latest release" document in GitHub's format, for a fork or a mirror.
