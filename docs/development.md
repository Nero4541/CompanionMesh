# Development

```powershell
uv sync --extra stt
uv run ruff check .
uv run ruff format .
uv run companion check      # validate config and reach every provider
```

CI runs lint and an import check on Windows and Linux, and rebuilds the dashboard to check that the committed build matches its source.

The dashboard is a Svelte app in `client/dashboard/` (Node 22 or newer). After changing it, rebuild and commit `dist/` along with the source:

```powershell
cd client/dashboard
npm ci
npm run dev      # http://localhost:5173/admin/, API proxied to a server on :8765
npm run build    # writes dist/
```

Release packages are built from a tag: `uv run python scripts/package_release.py v1.3.0` writes the Windows and Linux server packages, the edge client package and `SHA256SUMS` to `build/release/`. They hold the committed files of that tag without CI settings and dashboard sources, and the same tag always gives the same files.

```text
companion/
  api/         FastAPI app, HTTP routes, WebSocket transport
  core/        config, runtime, sessions, state machine, cancel tokens, clock sync, metrics
  agent/       Hermes, OpenClaw and direct backends
  audio/       PCM helpers, VAD, utterance segmentation, echo guard and canceller
  vision/      image validation, dedup, change detection, observations, vision pipeline
  attention/   attention policy, scene tracking, proactive-speech guards
  devices/     device processes: IP camera bridge and frame sources
  memory/      transcript stores (in-memory, SQLite)
  protocol/    envelope and binary frame codec, event names
  providers/   llm / stt / tts / vlm implementations
client/web/    browser development client
client/dashboard/  admin dashboard (Svelte; built output in dist/)
client/edge/   edge device client (separate uv project, websockets only); profiles/ per hardware
client/template/   a minimal device to port to other hardware or languages
configs/       default configuration
personas/      persona definitions
```
