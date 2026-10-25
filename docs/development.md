# Development

```powershell
uv sync
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

Release packages are built from a tag: `uv run python scripts/package_release.py v2.0.0` writes the Windows and Linux server packages, the edge client package and `SHA256SUMS` to `build/release/`. They hold the committed files of that tag without CI settings and dashboard sources, and the same tag always gives the same files.

```text
companion/
  api/         FastAPI app, HTTP routes, WebSocket transport
  core/        config, runtime, sessions, state machine, cancel tokens, clock sync, metrics
  agent/       Hermes, OpenClaw and direct backends
  audio/       PCM helpers, VAD, utterance segmentation, echo guard and canceller
  vision/      image validation, dedup, change detection, observations, vision pipeline
  attention/   attention policy, scene tracking, proactive-speech guards
  perception/  people, face tracks, who said what, routing, timeline; replay harness
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

## Replaying a recorded session

`companion replay` feeds a recording through the perception steps (faces, voices, who said what, whether it was meant for the companion) offline, without a live server, and scores the result against hand labels. Use it to check a change, or the thresholds, on the same recording again and again:

```powershell
uv run companion replay my-room/labels.json --fps 10 --out report.json --repeat 2
```

The labels file names the video and audio, the people in it (`user` or `acquaintance`), every utterance with its speaker (a person, `stranger`, `media` for a TV or video, or `unknown`) and whether it was `addressed` to the companion, and optional spot checks of faces (`{"t": 15.0, "box": [x0, y0, x1, y1], "who": "kenta"}`). The format is described at the top of `companion/perception/recording.py`. Video and audio are decoded with PyAV, so no ffmpeg is needed.

The report lists every utterance with what each step found, a summary per step, and a `fingerprint`: the same recording, people and settings always give the same fingerprint, and `--repeat 2` says whether two runs agree.
