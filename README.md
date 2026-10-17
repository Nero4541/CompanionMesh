# Companion Server

An open-source, self-hosted AI companion server. Talk to it by voice or text from a browser or from a small Linux board with a camera, microphone and speaker; it listens, can look through your camera when you allow it, thinks through the agent framework of your choice, and answers with streaming text and speech.

**Status:** v1.3.0. The realtime protocol is version 1.2: 1.x releases only add events and optional fields, so a client written for 1.0 keeps working as long as it ignores what it does not recognize.

- **Windows first, Linux supported.** Runs natively on Windows (PowerShell, no WSL required for the server itself) and on Linux.
- **Bring your own agent.** [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [OpenClaw](https://openclaw.ai) are supported through their OpenAI-compatible APIs. Without a framework, the `direct` backend talks to any OpenAI-compatible LLM.
- **Conversational, not walkie-talkie.** Talk over the companion and it stops mid-sentence to listen (barge-in), without mistaking its own voice for yours.
- **Speaks up, within limits.** When the camera sees something worth mentioning, the companion can start the conversation, at most once every 15 minutes, never during quiet hours, and only if the agent thinks it is worth saying.
- **A body for the companion.** A small Linux board (camera, microphone, speaker) can be the companion's endpoint over Wi-Fi or a phone hotspot, with Opus-compressed audio and automatic reconnection.
- **Several devices, one conversation.** A browser can keep the mic and speaker while an IP camera (or a phone running a camera app) joins the same session as the companion's eyes.
- **Agents can run elsewhere.** Point the companion at an agent on another machine (LAN or Tailscale). Memory and tools stay on that machine.
- **Opt-in vision.** A client can share camera frames with the agent, either as images for a model that can see or as short descriptions from a separate vision model. Cameras are watched locally and only frames where something changed are sent: no continuous video upstream. Off until a session enables it.
- **Replaceable providers.** STT, TTS, LLM and VLM are interfaces. The defaults are local: faster-whisper for STT and Irodori-TTS for Japanese TTS.
- **Private by default.** With an external agent, the companion writes no conversation data to disk. Audio and camera images are never stored unless you enable it.

## How it works

```text
Browser / edge client                     Companion Server                       Agent host (same or other machine)
┌───────────────────┐   WebSocket    ┌──────────────────────────────┐   HTTP   ┌──────────────────────────┐
│ mic ──PCM 16 kHz──┼───────────────►│ VAD (Silero) → STT (Whisper) │          │ Hermes / OpenClaw        │
│                   │                │          │                   │─────────►│  memory, tools, model    │
│ speaker ◄─audio───┼◄───────────────│ TTS ◄── sentence splitter ◄──│◄─stream──│                          │
│ text ◄──deltas────┼◄───────────────│                              │          └──────────────────────────┘
│ camera ──JPEG─────┼───────────────►│ filter → image (or VLM text) │
└───────────────────┘                └──────────────────────────────┘
```

The server streams the agent's reply text to the client as it arrives. Each finished sentence is synthesized and played while the rest of the reply is still being generated. The microphone stays open while the companion speaks, so you can interrupt it; see [Realtime conversation](#realtime-conversation).

## Requirements

| | Required | Notes |
|---|---|---|
| OS | Windows 10/11 or Linux | |
| [uv](https://docs.astral.sh/uv/) | yes | Installs Python 3.13 and all dependencies. No Conda, no manual `pip`. |
| Agent backend | yes | Hermes, OpenClaw, or any OpenAI-compatible LLM (`direct`) |
| NVIDIA GPU | recommended | For Whisper and the built-in voice. Falls back to CPU automatically. |
| STT/TTS servers | optional | Any OpenAI-compatible server, or none: the server can install and run [built-in speech](#built-in-speech) itself. Without any TTS the companion replies in text only. |
| Vision | optional | For the camera: the LLM behind your agent must be multimodal. Otherwise set `vision.mode: vlm` and configure a vision model. |
| Browser | for the dev client | Chrome or Edge recommended. The microphone needs `localhost` or HTTPS. |

## Quick start (Windows, PowerShell)

```powershell
git clone https://github.com/Nero4541/CompanionMesh.git
cd CompanionMesh

# Python 3.13 + dependencies (+ local speech recognition, + NVIDIA libraries)
uv python install 3.13
uv sync --extra stt --extra cuda

# Secrets
Copy-Item .env.example .env
notepad .env

# Validate the configuration and reach every provider
uv run companion check

# Start the server
uv run companion
```

Instead of editing `.env` and the config by hand, run `uv run companion setup`: it asks for the agent's address and key (and tests them), speech recognition and synthesis, and whether boards connect over the network or from away from home, then writes `.env` (with new device and admin tokens) and `configs/companion.local.yaml`. Run it again any time; Enter keeps each current value. `start-companion.bat` / `.sh` runs it by itself on the first start, when there is no `.env` yet. Without a speech-to-text or text-to-speech server, choose **Built-in** for both: setup then installs them for the server to run itself (see [Built-in speech](#built-in-speech)).

Open **http://127.0.0.1:8765/dev/**, press **Start mic** and speak, or type a message. **Start camera** lets the companion see. This needs a multimodal LLM behind the agent, or `vision.mode: vlm` with a vision model configured (see [Vision](#vision)).

**http://127.0.0.1:8765/admin/** is the admin dashboard: server status, connected devices, latency per pipeline stage and every setting (see [Admin dashboard](#admin-dashboard)).

After the first setup, double-click **`start-companion.bat`** in the project folder (on Linux, run `./start-companion.sh`): it starts the server (or finds it already running) and opens the dashboard once the server answers.

Instead of `git clone`, you can download a release from the [Releases](https://github.com/Nero4541/CompanionMesh/releases) page: `companion-mesh-<version>-windows.zip` or `companion-mesh-<version>-linux.tar.gz`. Unpack it (to a `companion-mesh` folder) and continue with `uv python install 3.13` above; `SHA256SUMS` lists the checksums.

On Linux, use `cp .env.example .env` instead of `Copy-Item` and `./start-companion.sh` instead of the `.bat`; every other command is the same. Without an NVIDIA GPU, drop `--extra cuda`; speech recognition then runs on the CPU. That is slower, so consider a smaller model (`stt.model: small`).

`uv run companion check` prints one line per component (config, persona, agent, STT, VAD, TTS) and exits non-zero if anything is unreachable. Run it first whenever something does not work.

## Updating

Stop the server (close its window, or Ctrl+C), then double-click **`update-companion.bat`** (on Linux, run `./update-companion.sh`) in the server's folder. It asks GitHub for the newest release and, if it is newer:

- downloads this system's package and checks it against the release's `SHA256SUMS` (nothing is changed if it does not match);
- keeps the files it is about to replace in `data/updates/backup-<version>-<time>.zip` (the last three are kept), writes the new ones and removes the files the new release no longer has;
- never touches what is yours: `.env`, `configs/*.local.yaml`, `data/`, `.venv/` and your own personas;
- installs the new dependencies with `uv sync`, keeping the optional ones you installed (speech recognition, CUDA, Opus).

Every update is logged in `data/updates.jsonl`. The dashboard's **Updates** page shows the version running, whether a newer release exists (with its notes), that log and the [changelog](CHANGELOG.md). Afterwards, update your boards from the dashboard: Devices, a board, Maintenance, **Update**.

In a git clone, the script runs `git pull --ff-only` and `uv sync` instead. To go back, unpack a backup zip over the folder and run `uv sync --frozen` (with your `--extra` options). `COMPANION_UPDATE_API` points the update at another "latest release" document in GitHub's format, for a fork or a mirror.

## Choosing an agent backend

Set `agent.provider` in the config (see [Configuration](#configuration)).

### Hermes Agent (`hermes`, default)

Hermes owns memory: each companion session maps to a Hermes session, so Hermes keeps the transcript, tool calls and long-term memory, and they survive restarts of either side.

1. Install and configure Hermes, including a model provider (`hermes model`). On Windows, Hermes runs inside WSL2; WSL forwards `localhost`, so the companion can reach it at `127.0.0.1`.
2. Enable its API server in `~/.hermes/.env`:

   ```bash
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<a long random string>
   ```

3. Put the same key in the companion's `.env` as `HERMES_API_KEY`. Without a key, Hermes still answers, but it ignores session continuity and memory scoping.
4. Start Hermes with `hermes gateway`. Its API listens on `http://127.0.0.1:8642`.

### OpenClaw (`openclaw`)

OpenClaw likewise keeps each session's transcript and its agent memory on the gateway host.

1. Enable the Chat Completions endpoint in `openclaw.json` (it is off by default):

   ```json5
   {
     gateway: {
       http: { endpoints: { chatCompletions: { enabled: true } } },
     },
   }
   ```

2. Put the gateway token (`gateway.auth.token`) in the companion's `.env` as `OPENCLAW_GATEWAY_TOKEN`.
3. Choose the agent with `agent.openclaw.agent_id` (default `main`), then start the gateway. It listens on `http://127.0.0.1:18789`.

### No agent framework (`direct`)

The companion talks to an OpenAI-compatible LLM itself, such as LM Studio, Ollama, llama.cpp, vLLM or a cloud API. Configure `llm.base_url`, `llm.model` and, if needed, `LLM_API_KEY`. This backend has no tools. Its memory is the stored conversation history plus a small full-text recall over earlier conversations.

### Running the agent on another machine

Point `base_url` at the other host, e.g. `http://100.x.y.z:8642/v1`. On the agent host:

- **Hermes:** set `API_SERVER_HOST=0.0.0.0` (or the Tailscale IP). `API_SERVER_KEY` is mandatory when binding beyond loopback.
- **OpenClaw:** set `gateway.bind` to `lan` or `tailnet`, and keep token auth enabled.

Alternatively, leave the agent (and a TTS server) listening on loopback and forward the ports over SSH. The traffic is then encrypted, and nothing changes on the server:

```powershell
ssh -N -L 127.0.0.1:28789:127.0.0.1:18789 -L 127.0.0.1:28088:127.0.0.1:8088 user@agent-host
```

Then set `base_url: http://127.0.0.1:28789/v1` for the agent and `http://127.0.0.1:28088/v1` for TTS.

> **Security:** these endpoints give full control of the agent, including its terminal and file tools, and the traffic is plain HTTP. Use them only over a private network such as [Tailscale](https://tailscale.com), or through an SSH tunnel. Never expose them to the internet.

## Where data lives

| Backend | Conversation text on the companion server | Long-term memory |
|---|---|---|
| `hermes`, `openclaw` | In memory only, dropped when the connection closes; nothing is written to disk | On the agent host |
| `direct` | SQLite at `data/companion.db` | Same SQLite file (full-text recall) |

Visual observations (VLM mode descriptions and `vision.event`s) follow the same rule: in memory only with an external agent, which receives them as part of the conversation; in SQLite with `direct`, deleted after `vision.retention_days`.

Microphone audio and synthesized speech are not written to disk, except utterances kept for checking speech recognition when `stt.keep_audio: true` (and microphone tests from the dashboard), under `data/recordings/` for `stt.keep_audio_days`. Camera images are never written to disk unless `vision.store_images: true`; they are then kept under `data/vision/` for `vision.retention_days`. `data/` also holds the boards this server set up (`devices.json`: token hashes, the root passwords it gave them, their last settings), their logs (`devices/<id>/edge.log`), this server's SSH key for its boards (`ssh/`) and the public listener's certificate (`tls/`); keep it private. Logs contain transcripts at `INFO` level; raise `logging.level` to `WARNING` if you do not want that.

With an external agent, reconnecting with the same session keeps the conversation going (the agent remembers), but the dev client will not replay earlier messages.

## Speech

### Built-in speech

Without a speech-to-text or text-to-speech server of your own, the server installs and runs both itself: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for recognition and [Irodori-TTS](https://github.com/Aratako/Irodori-TTS-Server), a Japanese voice. Choose **Built-in** in `uv run companion setup`, or open **Speech** in the dashboard, press **Install** and turn the engine on.

- **Installing.** Each engine gets an environment of its own under `data/builtin/stt` and `data/builtin/tts`, made by uv from the locked projects in `builtin/` (the voice runs on Python 3.10, which uv fetches by itself), so the server's own installation stays small. The first install downloads several GB: PyTorch for the voice and the NVIDIA libraries, or CPU builds without an NVIDIA GPU (`builtin.libraries`). The models download from Hugging Face on the first start: about 1.5 GB for Whisper `large-v3-turbo`, 3.4 GB for Irodori `v4.1-Small-MF`.
- **On and off.** The switch on the Speech page, or `builtin.stt.enabled` / `builtin.tts.enabled`, takes effect at once, without a restart. Off, the engine's process ends and all of its GPU memory is free at once (the page shows the GPU's total); on, it starts again, in seconds once its model is downloaded. An engine also ends with the server, however the server ends.
- **What it replaces.** While on, an engine is used instead of `stt.provider` / `tts.provider`; off, those apply again. Setup sets them to `none` when you choose built-in, so off means no speech recognition or voice.
- **Settings.** `builtin.stt` (model, device) and `builtin.tts` (checkpoint, device, `precision: bf16` to halve the memory on CUDA); changing them restarts only that engine. `stt.language`, `stt.prompt`, `tts.voice` and `tts.extra_body` apply as with any provider. Reference clips for the voice go in `data/builtin/tts/voices/`.
- **Updates.** A newer server may bring newer engines; an installed engine is brought up to date when it next starts.

Each engine serves the OpenAI audio API on `127.0.0.1` only, on a port of its own; its log is on the Speech page and in `data/builtin/<name>/worker.log`.

### Speech-to-text

The default is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with `large-v3-turbo`. The model is downloaded on first start (about 1.6 GB).

```yaml
stt:
  model: large-v3-turbo   # small / medium / large-v3 / …
  language: ja            # null = auto-detect
  device: auto            # tries CUDA first, falls back to CPU
```

The `cuda` extra installs cuBLAS and cuDNN from PyPI, so no separate CUDA toolkit is needed. Voice activity detection uses Silero VAD, whose model comes with the server, so it also works with speech recognition on another machine. An utterance ends after `vad.min_silence_ms` (default 1000 ms) of silence; lower it for snappier turns, raise it if you are cut off mid-sentence.

To run speech recognition on another machine, point the companion at any server with the OpenAI `POST /v1/audio/transcriptions` API ([speaches](https://github.com/speaches-ai/speaches), the whisper.cpp server, OpenAI, Groq):

```yaml
stt:
  provider: openai_compatible
  base_url: http://gpu-box:8000/v1
  model: Systran/faster-whisper-large-v3-turbo   # the server's model name
  language: yue                                   # Cantonese
  api_key_env: STT_API_KEY                        # optional
```

Voice activity detection still runs in the companion; only finished utterances are sent, as 16 kHz WAV.

**Steering the writing.** Whisper often writes Cantonese in simplified characters, or as written Mandarin. `stt.prompt` shows it the style you want with a short example in that style, for instance:

```yaml
stt:
  language: yue
  prompt: 以下係廣東話對話，用香港繁體中文書寫。
```

It goes with every utterance (Whisper's initial prompt; `prompt` in the OpenAI API, which speaches, OpenAI and the built-in engine take) and applies at once. There is none by default; add one only if you need it. Keep it to one short sentence: a long or repetitive prompt can make Whisper repeat itself. It changes how words are written, not which words are heard; for better Cantonese recognition, use a model fine-tuned for it.

### Text-to-speech

Any server implementing OpenAI's `POST /v1/audio/speech` works. Each sentence is synthesized as it completes. Vendor-specific options go in `tts.extra_body` and are merged into every request.

The default configuration targets [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server), a Japanese TTS server (MIT; check the model cards for the weights' licenses). The [built-in voice](#built-in-speech) is the same server, installed and run for you. To run it yourself, for example on another machine:

```powershell
git clone https://github.com/Aratako/Irodori-TTS-Server.git
cd Irodori-TTS-Server
uv sync --extra cu128
Copy-Item .env.example .env   # e.g. IRODORI_HF_CHECKPOINT=Aratako/Irodori-TTS-v4.1-Small-MF
uv run --no-sync python -m irodori_openai_tts --host 127.0.0.1 --port 8088
```

`tts.voice: none` synthesizes without reference audio. For a stable character voice, add a reference clip to the server's `voices/` directory and set `tts.voice` to its id. Set `tts.provider: none` for text-only replies.

## Realtime conversation

Conversation is full duplex with clients that support it (the dev client and the edge client do): the microphone keeps streaming while the companion speaks.

**Barge-in.** When you start talking over a reply, the server cancels the agent's reply and any speech still being synthesized (`TTS cancellation`), tells every speaker device to stop at once (`audio.output.stop`), and treats what you say as your next turn. The interrupted reply is remembered only up to what you actually heard, and the next turn sees it ending in "…". Speaking while the companion is still *thinking* restarts the turn with both of your messages. Older clients stay half duplex: their audio is ignored while the companion thinks or speaks. Set `realtime.barge_in: false` to make every client half duplex.

**Not hearing itself.** A speaker next to a microphone means the companion's voice reaches its own ears. Three layers keep that from turning into a conversation with itself:

1. **Devices with echo cancellation** say so (`aec: "device"` in `audio.input.start`). Browsers do (`echoCancellation`), and so do most USB speakerphones. Nothing else is needed.
2. **Gating.** While reply audio plays on a device without echo cancellation, speech has to be longer (`barge_in_min_speech_ms`, 600 ms) and clearer (`barge_in_threshold`) to count as you cutting in.
3. **Echo guard.** A transcript that mostly repeats what the companion said in the last `guard_window_s` seconds is its own voice coming back. It is dropped (counted as `echo_dropped` in `/v1/metrics`).

`realtime.echo.canceller: nlms` adds a reference acoustic echo canceller on the server. Devices report when each reply clip starts playing as a position in their microphone stream. The server lines up the audio it sent with the microphone signal, finds the remaining speaker and room delay (up to `max_delay_ms`), and subtracts the echo with a frequency-domain NLMS filter. It is a reference implementation for boards without hardware AEC: it helps with a fixed speaker and microphone, while a speakerphone with built-in AEC is better.

**States and recovery.** Every session moves through `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `interrupting` and `recovering`, and only along defined transitions. Each busy state has a deadline. If a transcription hangs, a device never confirms playback, or the only speaker disconnects mid-reply, the session moves to `recovering` and back to listening instead of staying stuck.

**Latency.** `GET /v1/metrics` shows where the time goes, per stage, with p50/p95 over recent turns. It records numbers only: no audio, images or text.

```json
{"stages": {"mic_to_vad": {...}, "vad_to_stt": {"p50": 0.41, "p95": 0.8, ...},
            "stt_to_first_token": {"p50": 9.8, ...}, "first_token_to_audio": {...},
            "speech_to_audio": {...}, "network_rtt": {...}, "barge_in_to_stop": {...},
            "frame_to_observation": {...}},
 "counters": {"barge_ins": 3, "echo_dropped": 1, "recoveries": 0},
 "bottleneck": "stt_to_first_token"}
```

The server pings each device every `realtime.ping_interval_s` (10 s) to measure the round trip and the offset between the device's clock and its own. Device timestamps (when audio was captured, when a frame was taken) can then be compared with server time. `/v1/status` shows each device's `rtt_ms` and `clock_offset_ms`. Set `metrics.emit_turn: true` to also send each turn's timings to clients as `metrics.turn`.

```yaml
realtime:
  barge_in: true
  barge_in_min_speech_ms: 600
  echo:
    canceller: none      # or nlms
    guard: true
```

## Vision

> **The LLM that powers your agent must be multimodal (accept images) for the camera to work in the default mode.** If it is not, set `vision.mode: vlm` and configure a vision model in the `vlm` section ([VLM mode](#vlm-mode)); the agent then receives text descriptions instead of images.

Vision is opt-in per session: the dev client's **Start camera** sends `vision.enable`, and **Stop camera** sends `vision.disable`, which stops all visual processing immediately, including an analysis in progress.

```text
camera frame (JPEG/PNG/WebP)
 -> validate type, size and pixel count
 -> rate limit (vision.min_interval_s; "manual" frames skip it)
 -> skip near-duplicates of the last accepted frame (difference hash)
 -> downscale to vlm.max_image_side
 -> vision.mode: agent  newest frame is attached as an image to your next message
    vision.mode: vlm    a vision model describes the newest frame -> observation text
```

Cameras send a frame every few seconds (configurable, or only on request). In addition, when you start speaking or send a message, the server asks every camera device for a fresh frame (`vision.capture.request`, controlled by `vision.capture_on_turn`) and waits up to `vision.wait_for_pending_s` for it, so a question like 「これ何？」 is answered with what the camera sees now.

### Agent mode (default)

`vision.mode: agent` hands the image straight to the agent: your next message is sent as text plus the newest frame (an OpenAI `image_url` part). The agent's own model does the seeing, and its memory records what it saw. With `vision.max_turn_images: N` (default 1) the message carries up to N frames that arrived since the last turn, oldest first; when more arrive, routine frames are dropped before ones a detector or the user asked for. Each frame is sent at most once and only if it is younger than `vision.observation_max_age_s`; the companion keeps no image after the turn.

This needs an agent backend and model that accept images:

- **Hermes / OpenClaw** pass `image_url` parts through. In OpenClaw, the model definition must list image input, e.g. `"input": ["text", "image"]`, or the image is not forwarded.
- **llama.cpp** must be started with the model's multimodal projector (`--mmproj …`).
- **`direct`**: `llm` must point at a model that accepts images.

Images add to the prompt the model has to process; on a slow GPU, a smaller `vlm.max_image_side` (e.g. 512) keeps turns faster.

### VLM mode

`vision.mode: vlm` uses a separate vision model (the `vlm` section) to describe each accepted frame. Only the newest waiting frame is analyzed; descriptions arrive as `vision.observation` events, are kept as episodic memory, and are passed to the agent as text. Use it when the agent's model cannot see, or to keep images away from the agent entirely.

New observations are passed with your next message (`vision.inject: user`), which keeps the agent's prompt cache intact; each is included once. `vision.inject: system` instead lists recent observations in the system prompt on every turn. A turn waits up to `vision.wait_for_pending_s` for a frame that is still being analyzed.

### IP cameras and phones

Any camera reachable over HTTP or RTSP can be the companion's eyes. `companion camera` runs a small device process that joins the active conversation with the `camera` role, switches vision on, and sends frames when the view changes and whenever the server asks:

```powershell
# A phone running an "IP camera" app on the same network (or on Tailscale)
uv run companion camera http://192.168.1.20:8080/shot.jpg      # HTTP snapshot
uv run companion camera http://192.168.1.20:8080/video         # MJPEG stream
uv run companion camera rtsp://192.168.1.20:8554/live          # RTSP, needs ffmpeg on PATH
```

The source type is detected from the URL and the response. By default (`--mode change`) the bridge looks at the camera itself about once a second, on a tiny greyscale copy, and sends a frame only when something moves or the scene changes, plus a routine frame every `--heartbeat 300` seconds. `--mode periodic --interval 5` sends a frame every 5 seconds instead (`0` = only on request). Other options: `--session <id>` to join a specific session instead of the most recently started one, `--device-id`, and `--no-enable` to leave vision off until someone enables it. The bridge reconnects automatically. If vision is switched off in the session (for example with the **vision on** button in the dev client), the camera pauses and stays paused until vision is switched on again.

If the camera asks for a login (Basic or Digest, e.g. the "IP Webcam" Android app with a password set), put it in `.env` rather than on the command line:

```bash
CAMERA_USERNAME=…
CAMERA_PASSWORD=…
```

`http://user:pass@host:8080/shot.jpg` also works. Credentials never appear in logs.

The bridge is meant to run for a long time. Start it whenever you like: it waits for the camera to come up (retrying every 5 s), waits for a conversation to join, and when a conversation ends it joins the next one.

**Cameras the server runs itself.** Instead of a separate `companion camera` process per camera, list them under `cameras:` (or add them on the dashboard's **Cameras** page, which can also test a camera and show its current view). The server runs the same bridge for each enabled camera, and starts, stops or replaces them as soon as the list changes:

```yaml
cameras:
  - id: livingroom                      # also its device id
    url: http://192.168.1.20:8080/shot.jpg
    username_env: CAMERA_USERNAME       # login from .env (the default names)
    password_env: CAMERA_PASSWORD
    mode: change                        # or periodic, with interval_s
    heartbeat_s: 300
    enabled: true
```

Each camera names its own login variables, so cameras with different logins work side by side. Credentials in a camera's `url` are refused here, because the URL is shown on the dashboard and in `/v1/config`.

**Visual events.** Frames sent because something changed carry what the camera's detector saw (`event: {kind, score, area}`). They are prioritized: frames you asked for come first, then big changes, then routine frames. Vetted change frames skip the duplicate check, and important ones skip the rate limit. In VLM mode, frames wait in a small priority queue, and stale routine frames are dropped when the model is busy. Changes become events for the [attention engine](#proactive-behavior): in VLM mode with the model's description of the frame, in agent mode as "something moved", with the frame itself handed to the agent if it comes to a proactive turn. A clear change can lead to one; small movements stay context.

In both modes, clients that detect things themselves, such as an edge device with an accelerator, can send `vision.event` with a description; it becomes an observation without any model call.

#### Vision model settings

```yaml
vlm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1   # llama.cpp with --mmproj, vLLM, LM Studio, Ollama, cloud…
  model: default
  max_image_side: 768                  # frames are downscaled before sending
```

Any chat endpoint that accepts `image_url` content parts works. The companion asks for a short JSON description (`description`, `tags`, `confidence`) in the persona's language and falls back to plain text if the model ignores the format. It instructs the model not to identify people. `uv run companion check` sends a small test image to verify the model.

A small VLM is enough: descriptions are short, and one frame is analyzed at a time. The VLM shares the GPU budget with everything else (see below); on a busy GPU, raise `vision.min_interval_s`.

## Edge devices

A small Linux board with a camera, microphone and speaker can be the companion's body, while all inference stays on the server. The edge client lives in `client/edge`: a separate, deliberately tiny uv project whose only Python dependency is `websockets`. Audio and video go through standard tools (ALSA `arecord`/`aplay`, `ffmpeg`, and `libopus` via ctypes), so nothing heavy has to be compiled on boards such as RISC-V SBCs with 256 MB of RAM.

```text
board: arecord -> Opus (libopus) ----\                     /-> STT -> agent -> TTS --\
       ffmpeg (V4L2) -> JPEG frames ---> WebSocket (token) --> camera frames            |
       aplay <-------------------------------------------------- speech audio <--------/
```

### Setting up a board from the dashboard

For a LicheeRV Nano (or another board whose image logs in as `root` over SSH and starts services from `/etc/init.d` or systemd, with Python 3.11+ and `websockets`), nothing has to be typed on the board:

1. Flash the SD card with the maker's image (Sipeed's Buildroot image for the LicheeRV Nano) and let the board boot once (about 30 s).
2. Plug the board into the computer running the server by USB (the LicheeRV Nano's USB-C port). Its image offers a USB network; the server finds the board on it.
3. In the dashboard, open **Boards**, give the board a name, pick the hardware profile, the address it should reach this server at, the camera, and the Wi-Fi networks it may use (home and your phone's hotspot), and press **Set up**. `server.host` must not be `127.0.0.1` (`uv run companion setup` takes care of that).
4. Unplug it and power it from anywhere with Wi-Fi; it connects by itself.

**Set up** logs in over SSH (the stock password, or one you give), puts this server's own SSH key on the board and replaces the stock root password with a random one (shown under **Boards**: the stock one is public and the board goes outdoors). It gives the board a **token of its own**, copies this server's edge client to it (the board needs no Internet), writes `edge.toml` from the profile (a board set up before keeps its settings; a reflashed one gets back the settings it last reported), adds the Wi-Fi networks with their keys hashed (also on the boot partition, where Sipeed's image reads them from), installs a service that starts at boot and restarts the client if it stops, checks the tools and starts it. The same from a terminal: `uv run companion edge install --name walker --wifi "Home" --wifi "Phone"` (`uv run companion edge discover` lists boards on USB).

To change a board that was set up before but cannot connect (a new Wi-Fi network, another server address) or to bring its software up to date without the network, plug it in again and press **Update over USB** under **Boards** (or `uv run companion edge update --name walker --wifi "New network"`): it keeps the board's token and password and only updates its software, settings and Wi-Fi networks.

A board's own token works only for its name. **Lock out** under **Boards** makes it stop working at once and closes the board's connection, for example when a board is lost; set the board up again to let it back in.

### Setting up a board by hand

**On the server:** listen on an address the board can reach and set a token:

```yaml
# configs/companion.local.yaml
server:
  host: 0.0.0.0          # or the server's Tailscale IP
```

```bash
# .env
COMPANION_TOKEN=<a long random string>
```

The server refuses to listen beyond localhost without a token. With a token set, every client must present it: the dev client as `/dev/?token=…` (opened once; the browser remembers it), `companion camera` and the edge client from `COMPANION_TOKEN` in their environment.

**On the board** (Debian/Ubuntu-based images). Without git, use `companion-edge-<version>.tar.gz` from the [Releases](https://github.com/Nero4541/CompanionMesh/releases) page instead of the clone; it unpacks to the same files as `client/edge`, profiles included.

```bash
sudo apt install alsa-utils ffmpeg libopus0
git clone https://github.com/Nero4541/CompanionMesh.git && cd CompanionMesh/client/edge
cp edge.example.toml edge.toml      # set server = "ws://<server>:8765/v1/realtime"
export COMPANION_TOKEN=…
uv run companion-edge --config edge.toml --check   # tools, libopus, camera
uv run companion-edge --config edge.toml
```

`client/edge/systemd/companion-edge.service` is an example unit for running it at boot.

**MIPI CSI cameras on CVITEK boards.** The LicheeRV Nano's camera module (GC4653) is not a V4L2 device on Sipeed's image, so ffmpeg cannot open it. `[camera] driver = "cvi"` keeps Sipeed's `sensor_test` running and asks it for a frame when needed: change detection reads a few rows of each 2560×1440 frame (about 0.3 s per look), and a JPEG is made only for a frame that is sent (under a second with ffmpeg). `rotate` turns pictures from a module mounted sideways. Do not run Sipeed's `sample_venc` or `sample_vio` against this camera: when they crash, the camera sends no frames again until the board is switched off and on.

**Hardware profiles.** `client/edge/profiles/` has ready-made configs to copy instead of `edge.example.toml`; each explains the hardware it assumes and what to check:

| Profile | For |
|---|---|
| `licheerv-nano.toml` | Sipeed LicheeRV Nano with Sipeed's Buildroot image: onboard microphone and speaker, Opus through ffmpeg (no libopus), half duplex, the MIPI CSI camera (`driver = "cvi"`) |
| `raspberry-pi.toml` | Raspberry Pi with a USB speakerphone (hardware echo cancellation, so you can interrupt) and a USB webcam |
| `bluetooth-speaker.toml` | Any board playing through a Bluetooth speaker with bluez-alsa (lead-in and a longer hold for the speaker's wake-up) |
| `speaker-only.toml` | A speaker without microphone or camera, e.g. for proactive remarks while a microphone is too noisy |
| `camera-only.toml` | A board with only a camera: it joins whichever conversation was started last and waits for the next one when that ends |

Network cameras and phones need no board: list them under `cameras:` on the server instead (see [IP cameras and phones](#ip-cameras-and-phones)).

**Your own device.** `client/template/device.py` is a minimal device in one file (only `websockets`) with the whole protocol a device needs and its hardware in four methods to replace. [`client/template/README.md`](client/template/README.md) explains porting it, for example to a microcontroller or another language.

What the edge client does:

- **Microphone:** 16 kHz audio, Opus-encoded at ~24 kbps (about a tenth of raw PCM) and sent in batches of 20 ms packets. Without `libopus` it uses ffmpeg's Opus encoder, or falls back to PCM. Each batch carries its position in the microphone stream and its capture time. The mic keeps streaming while the companion speaks, so you can interrupt it (`full_duplex = true`). Set `aec = "device"` if your microphone cancels its own speaker's echo.
- **Speaker:** sentences of a reply are played back to back through one output stream, so there are no gaps between them. The client reports when each clip starts (for echo handling) and when the reply finished, and stops immediately when the server says so.
- **Camera:** ffmpeg reads the V4L2 camera once and produces both JPEG frames and a 64x48 greyscale copy. In `change` mode (default), the client checks the greyscale copy `analysis_fps` times a second for motion or a scene change, in plain Python, and sends a frame only when something changed, plus one every `heartbeat_s`. Frames are also sent on the server's capture requests (when you speak). `mode = "periodic"` uses adaptive sampling instead: every frame the server reports as a duplicate doubles the interval (up to `max_interval_s`), and a changed view resets it.
- **Network loss:** reconnects with exponential backoff, resumes the same session (the id is stored on disk), and keeps at most `buffer_s` seconds of microphone audio while offline. The server keeps the session for `server.session_linger_s` (60 s) after the last device drops and replays what the device missed, e.g. a reply that finished meanwhile.
- **Heartbeat:** `device.status` every `heartbeat_s` with CPU temperature, free memory, Wi-Fi signal, uptime and buffer level; `/v1/status` lists every connected device with its latest status.

**Bluetooth speakers** work through [bluez-alsa](https://github.com/arkq/bluez-alsa): pair the speaker with `bluetoothctl`, run `bluealsa -p a2dp-source`, and point `output_device` at an ALSA PCM for it, e.g. in `~/.asoundrc`:

```text
pcm.speaker {
    type plug
    slave.pcm "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"
}
```

Then set `output_device = "speaker"` and `output_lead_in_ms = 300`: Bluetooth speakers chop the first fraction of a second after their stream starts.

The protocol has nothing board-specific: any client that speaks it (another SBC, a phone app) works the same way.

### Reaching the server from outside

A board carried outdoors (on your phone's hotspot or mobile data) is outside the server's network. The board only ever connects out to the server, so all it needs is one address of the server it can reach from anywhere; settings, logs, diagnostics and updates then travel over that same connection.

**Recommended: [Tailscale](https://tailscale.com) on the server and the board.** Both connect out to Tailscale, so it works behind carrier-grade NAT, on any hotspot or public Wi-Fi, with a VPN on the server and without touching the router or the firewall; the server's Tailscale address stays the same wherever the board is, and traffic between them is encrypted.

1. **Server:** install Tailscale and sign in. `tailscale ip -4` shows its address (`100.x.y.z`).
2. **Board:** install Tailscale and sign in to the same account (tailnet). On Sipeed's LicheeRV Nano image (no package manager), copy the static `riscv64` binaries from [pkgs.tailscale.com](https://pkgs.tailscale.com/stable/#static) to `/usr/sbin/` and start `tailscaled` at boot with its state on the SD card, since `/run` is cleared at every start, for example as `/etc/init.d/S45tailscale`:

   ```sh
   #!/bin/sh
   case "$1" in
     start)
       mkdir -p /root/.tailscale /var/run/tailscale
       start-stop-daemon -S -b -m -p /run/tailscaled.pid -x /usr/sbin/tailscaled -- \
         --state=/root/.tailscale/tailscaled.state --port=41641 ;;
     stop)
       start-stop-daemon -K -p /run/tailscaled.pid && rm -f /run/tailscaled.pid ;;
   esac
   ```

   Then run `tailscale up` once on the board and open the link it prints (or use `tailscale up --auth-key=…` with a key from the Tailscale admin console). `tailscaled` takes about 30 MB of the LicheeRV Nano's 128 MB.
3. **Set the board up** (or **Update over USB**) under **Boards** and pick the server's Tailscale address under **Server address for the board**: the list shows it, marked "Tailscale". The same address works at home and away.

Keep `server.host: 0.0.0.0` (or set it to the Tailscale address) and the device token, as for any network.

#### Without Tailscale: the public listener (advanced)

If you cannot use Tailscale and your home connection has a public IPv4 address (or IPv6), the server can offer boards a port of its own on the Internet. This needs a port forward on the router and breaks when the server's own traffic goes through a VPN or an exit node, so it is for those who can control both.

Turn on the **public listener** (`uv run companion setup` asks, or set it yourself):

```yaml
# configs/companion.local.yaml
server:
  host: 0.0.0.0
  public:
    enabled: true
    port: 8766
    url: wss://myhome.example.net:8766/v1/realtime   # how boards reach it from outside
```

It is a second listener, on IPv4 and IPv6, that serves only what boards need (`/health`, `/v1/realtime` and the edge client for updates), never the dashboard or the dev client. It takes only the tokens of boards this server set up, only in the `Authorization` header, refuses an address after repeated failed logins (`max_failed_auth`, `ban_s`) and limits connections per address. It uses TLS with this server's own certificate (made once in `data/tls/`); the installer copies that certificate to each board, which then trusts exactly it, so no domain or certificate authority is needed and the connection works with an IP address, a dynamic DNS name or IPv6. It is valid from 1970 on, as a board without network has no correct clock yet. Set the boards up again after turning it on, so they learn the address: they try their home address first and the public one when that fails.

Then make that port reachable from the Internet, depending on your connection:

| Your connection at home | What to do |
|---|---|
| A public IPv4 address | Forward TCP port 8766 on the router to the server's address. If the address changes, use dynamic DNS (most routers have it built in, or a free service) for `url`. |
| IPv6 (at home and on the phone's hotspot) | Allow incoming TCP port 8766 to the server's (stable) IPv6 address in the router's firewall, nothing else. No port forwarding is needed. |
| Carrier-grade NAT, no IPv6 | Use Tailscale (above). Otherwise a small rented server in between: a relay such as frp or an SSH reverse tunnel to it, or a hosted tunnel (Cloudflare Tunnel). |
| The server's traffic goes through a VPN or an exit node | Incoming connections usually fail (the replies leave through the VPN). Use Tailscale, or exclude the server from the VPN (split tunneling). |

And allow the port in the server's firewall, for that port only: on Windows, as administrator, `New-NetFirewallRule -DisplayName "Companion devices" -Direction Inbound -Protocol TCP -LocalPort 8766 -Action Allow -Profile Private`; on Linux, e.g. `sudo ufw allow 8766/tcp`. Never allow the whole program on all ports. Check from outside: on your phone, with Wi-Fi off, open `https://<your address>:8766/health`. The browser warns about the certificate (expected: it is the server's own); it should then say `status ok`.

**Security.** An open port is never free of risk, and the public listener is built to keep it small (see above). The largest risk is not the network but the agent: Hermes and OpenClaw can have terminal and file tools on their host, and anyone holding a board's token can talk to the agent. Lock out a board that is lost at once, and limit what the agent may do (its own tool and approval settings). Run the server as a normal user. A board's SD card is not encrypted: a stolen board leaks its token and its Wi-Fi keys, so also change the hotspot password.

### Looking after boards

Everything below works wherever the board is, over its own connection (companion-edge 1.2):

- **Log** (Devices → Log): what the board logged, including what it logged while offline (sent when it is back), kept per board in `data/devices/<id>/edge.log` also after it disconnects.
- **Diagnostics**: versions, its own checks, memory, disk, network, Wi-Fi and kernel messages, from a fixed list of read-only commands (there is no remote shell).
- **Camera**: its newest picture, **Take a picture now**, and **Live**: pictures in a row while you watch (as fast as the board manages, at most 2 a second; about one a second on a LicheeRV Nano), which stop by themselves soon after you leave the page. Live pictures only update the view; they do not go to the agent.
- **Maintenance**: **Update** to the edge client this server has (downloaded from the server, checked, installed next to the old one; if the new version cannot connect after three starts, the board goes back to the old one by itself), restart the client or reboot the board, add and remove Wi-Fi networks (not the one it uses now), and speaker and microphone tests.

When a board cannot connect at all, plug it in by USB and run `uv run companion edge doctor`: it shows the board's version, whether the client runs, its checks, settings, log, Wi-Fi and clock. Set it up again to fix settings or Wi-Fi.

## Proactive behavior

The companion can speak first, under strict rules. Every noticed event goes through a fixed policy:

```text
event -> salience -> repeat? -> anyone talking / turn in progress?
      -> proactive switch, do-not-disturb, quiet hours, cooldown, hourly budget
      -> IGNORE | REMEMBER | CONTEXT_ONLY | SPEAK
```

| Decision | Effect |
|---|---|
| `ignore` | Nothing |
| `remember` | Kept as an episodic record |
| `context_only` | Told to the agent with your next message ("Noticed since the user's last message: …") |
| `speak` | A proactive turn: the agent gets a description of the event (and the current camera image in agent mode) and may say one short remark, or reply `[SILENT]` to stay quiet |

Events come from the camera and from clients:

- **Scene change:** the camera view changes a lot (`attention.scene_change_threshold`, in difference-hash bits). Bigger changes score higher; a moderate change is context only.
- **Activity resumed:** a big change after the view stayed still for `attention.absence_s` (20 minutes by default), e.g. you came back to your desk. Scores high enough to speak.
- **Client events:** `vision.event` with an optional `salience` (0–1), for devices that detect things themselves.
- **Idle check-ins** (off by default): with `attention.idle: true`, once nobody has spoken for a random while between `attention.idle_min_s` and `attention.idle_max_s` (30–60 minutes by default), the agent may start a conversation. It is shown a fresh camera frame if vision is on. If the guards below block speech at that moment, the wait simply starts over.

The rules that keep it polite are deterministic; no model can override them:

- at most one proactive turn every `attention.min_interval_s` (15 minutes) and `attention.max_per_hour` (4);
- nothing within `attention.grace_after_activity_s` of you or the companion speaking, and nothing while a turn is in progress;
- quiet hours, `23:00`–`08:00` by default, in the **host's time zone** unless `attention.quiet_hours.timezone` names one (e.g. `Asia/Tokyo`);
- do-not-disturb at any time: the dev client's **quiet** button (`attention.quiet`);
- `attention.proactive: false` turns proactive speech off entirely; normal conversation is unaffected.

The companion never reacts to its own voice: audio input is ignored while it speaks. A proactive turn that is still waiting for the agent keeps listening, so you can simply start talking, and your turn takes over. Set `attention.debug: true` to see every decision and its reasons in the dev client.

With slow local models, a proactive turn takes as long as any other turn; the agent's reply is only spoken once it is complete.

## Performance and hardware

The time from the end of your sentence to the first spoken word is roughly:

```text
end-of-utterance pause  +  STT  +  agent first token  +  TTS of the first sentence
```

Measured with v0.1.5:

| Stage | Measured | Setup |
|---|---|---|
| End-of-utterance pause | 1.0 s | `vad.min_silence_ms` default |
| STT | 0.2–0.35 s for 1–3 s of speech | Whisper `large-v3-turbo` fp16, RTX 4070 Ti |
| TTS, per sentence | 0.4–0.5 s | Irodori `v4.1-Small-MF` (4 steps), RTX 4070 Ti |
| TTS, per sentence | ~2 s | Irodori `v4.1-Small` (40 steps), Tesla V100 shared with an LLM |
| Agent, first token | 10 s (prompt cached) to 50–95 s | OpenClaw agent with a ~23k-token prompt; Qwen 27B Q8 on llama.cpp across 2× V100 that also serve other GPU jobs; reasoning enabled |

**With a local LLM, the agent is almost always the bottleneck.** Speech recognition and synthesis take seconds at most; the agent can take a minute. Two causes dominate:

- **Prompt size.** Agent frameworks send large system prompts: tools, skills, memory, character sheets. In the run above, a ~23k-token prompt took 48 s to process (about 490 tokens/s) whenever the server could not reuse its prompt cache.
- **Reasoning ("thinking") tokens.** One 31-character reply was preceded by ~1,800 generated tokens, which took 65 s at 27 tokens/s.

The companion streams each sentence to TTS as soon as it is complete. The dev client shows how long the agent has been thinking, so a slow turn is visibly slow rather than apparently stuck.

### Making a local setup responsive

For the agent:

- **Use a lean agent for voice.** A dedicated agent with few tools and a short system prompt (a few thousand tokens rather than 20k+) cuts prompt processing proportionally. Copy the character or persona into it rather than reusing a heavyweight general-purpose agent.
- **Turn reasoning off for voice turns.** Disable it in the agent's model settings. For `direct` with llama.cpp or vLLM serving a Qwen3-style model, send it with the request:

  ```yaml
  llm:
    extra_body:
      chat_template_kwargs: {enable_thinking: false}
  ```

- **Keep the prompt cache warm.** llama.cpp only skips work for a prompt prefix it has already processed. If first-token time swings between turns (for example 10 s, then 60 s), check the LLM server's log for full prompt re-evaluation. Running the voice agent's model without other concurrent users (a dedicated instance or slot) helps.
- **Cap reply length** where the backend honours it: `agent.<backend>.extra_body: {max_tokens: 200}`.
- **Prefer a smaller or faster model for conversation**, or a cloud model, and keep heavyweight models for agent tasks that are not real-time.

For TTS:

- Use a distilled model such as Irodori `v4.1-Small-MF` (4 steps) when you do not need a voice trained on a specific base model.
- Otherwise trade quality for speed with fewer sampling steps: `tts.extra_body: {irodori: {num_steps: 16}}`.

Use a shorter `vad.min_silence_ms` (e.g. 700) for snappier turns if you speak in continuous sentences.

### GPU memory

| Component | VRAM (measured) |
|---|---|
| Whisper `large-v3-turbo`, fp16 | ~2.3 GB |
| Irodori-TTS `v4.1-Small`, fp32 | ~4 GB |
| VLM | depends on the model; small models (a few billion parameters) are enough |
| LLM | depends on model and context size; usually by far the largest |

The companion server itself needs a GPU only for STT (and the built-in voice). STT can stay on the machine running the companion while the agent, LLM and TTS run on a server. Turning a [built-in engine](#built-in-speech) off frees its GPU memory at once, for example to run something else on the card for a while.

### Suggested setups

| Setup | Where things run | Notes |
|---|---|---|
| One PC, ~12 GB GPU | Companion, STT and TTS locally; agent backed by a cloud model or another machine | STT and TTS together take most of a 12 GB card; a local LLM of useful size does not fit beside them. |
| PC + GPU server | Companion and STT on the PC; agent, LLM and TTS on the server, reached over Tailscale or an SSH tunnel | Give the conversational model a GPU that is not busy with other jobs during conversations; use a lean agent with reasoning off. |
| No NVIDIA GPU | STT `small` on the CPU; agent with a cloud model; TTS on another machine, or text only | Expect a few seconds of STT per utterance. |

## Configuration

| File | Purpose | Committed |
|---|---|---|
| `configs/companion.yaml` | Defaults (non-secret) | yes |
| `configs/companion.local.yaml` | Your overrides, deep-merged over the defaults | no (git-ignored) |
| `.env` | Secrets: API keys and tokens | no (git-ignored) |
| `personas/default.yaml` | The bundled persona: system prompt, language, voice | yes |
| `personas/<name>.yaml` | Your own personas (`agent.persona`) | no (git-ignored) |

The config refers to secrets by environment-variable *name* (`api_key_env`), never by value. `extra_body` on `agent.hermes`, `agent.openclaw`, `llm` and `tts` adds backend-specific fields to every request; core fields such as `model`, `messages` and `stream` cannot be overridden on the agent backends. Example `configs/companion.local.yaml`:

```yaml
agent:
  provider: openclaw
  openclaw:
    base_url: http://100.64.0.12:18789/v1
stt:
  language: null
```

Other options: `uv run companion --config path\to\file.yaml`, `--host`, `--port`, or the `COMPANION_CONFIG` environment variable.

### Admin dashboard

`/admin/` shows the server's status, connected devices with their round-trip time and last reported status, latency per pipeline stage, and every setting, which you can change there.

- Each setting is marked **live** or **restart**. Live settings (vad, attention, realtime, most of vision, `metrics.emit_turn`) apply as soon as you save, also to conversations already open; the rest wait for a restart, which the page offers to do. The server then stops, reads its config files again and starts in the same process; if the new settings cannot start (say the port is taken), it starts again with the previous ones.
- Values are checked before saving, and invalid ones are marked at the setting.
- Saving writes only what differs from `configs/companion.yaml` into `configs/companion.local.yaml`; `companion.yaml` is never changed. The version before each save is kept as `companion.previous.local.yaml`, and a file you wrote by hand (with your comments) as `companion.original.local.yaml`, once.
- `--host`/`--port` on the command line win over the file; such settings are marked.
- An override can add or change keys of a mapping such as `extra_body` but cannot remove one that `companion.yaml` sets; the page says so, and that change has to be made in `companion.yaml`.
- Secrets never appear: settings that name an environment variable show only "set in .env" or "not set". Edit `.env` itself to change them.

The **Personas** page lists the personas in `personas_dir` and edits their display name, language, TTS voice and system prompt. **Use this persona** switches at once (from the next reply) and saves `agent.persona`; editing the persona in use also applies from the next reply. The bundled `default` persona is not edited in place, so updates to the project cannot conflict with your changes: **Save a copy** makes your own, which git ignores. A persona in use, now or after a restart, cannot be deleted.

The **Devices** page lists the connected devices with their last reported status. Edge devices running companion-edge 1.1 or newer started with `--config` show their settings there, and can be changed, or set up from one of the [hardware profiles](#edge-devices): the device checks the new settings, writes the changed lines into its TOML file (keeping the previous file as `edge.toml.bak`) and restarts with them in the same process, so no service manager is needed. `server`, `token_env`, `device_id` and `state_file` can only be changed on the device, as a mistake there would cut it off.

The Devices page also has, per device, its **Log**, **Camera**, **Diagnostics** and **Maintenance** (see [Looking after boards](#looking-after-boards)), and lists boards that are not connected but left their log here. The **Boards** page sets boards up over USB and lists the boards this server set up (see [Setting up a board from the dashboard](#setting-up-a-board-from-the-dashboard)).

The **Events** page shows what the server is doing as it happens: devices connecting and leaving, state changes, attention decisions with their reasons, camera observations, turns, errors and saved settings, with filters and a pause. What people say is not part of it unless `server.dashboard_conversation: true`: otherwise transcripts and replies show only their length.

The **Conversations** page shows what the companion heard and said, per conversation, while `server.dashboard_conversation` is on (from then on; nothing is kept while it is off), and the recordings kept with `stt.keep_audio: true`: each utterance as the server received it, what speech recognition made of it and what became of it, to listen to when the companion mishears.

The dashboard and its API (`/v1/admin/*`) have their own token, so a device's `COMPANION_TOKEN` cannot change the server:

- With `COMPANION_ADMIN_TOKEN` unset, they answer only this machine (open `http://127.0.0.1:8765/admin/` on the server itself).
- With `COMPANION_ADMIN_TOKEN=<a long random string>` in `.env`, any address works once you enter the token (or open the page once as `/admin/?token=…`).

`server.dashboard: false` turns the page off. The built page is committed in `client/dashboard/dist/`, so running the server needs no Node; to change it, see [Development](#development).

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness (no token needed) |
| `GET /v1/status?probe=true` | Version, sessions, connected devices and their status, providers, agent reachability |
| `GET /v1/metrics` | Latency per pipeline stage and counters (barge-ins, echoes dropped, recoveries) |
| `GET /v1/config` | Effective configuration (secret values are never included) |
| `POST /v1/chat` | One text turn: `{"text": "...", "session_id": "optional"}` → `{"session_id", "text"}` |
| `WS /v1/realtime` | Streaming text and voice (below) |
| `GET /dev/` | Browser development client |
| `GET /admin/` | Admin dashboard |
| `GET /v1/admin/config` | Running and saved settings, their JSON schema, which need a restart or wait for one, which secrets are set (admin token) |
| `PUT /v1/admin/config` | Save every setting (`{"config": {...}}`, as `saved` above); applies the live ones, returns `applied` and `pending_restart` (admin token) |
| `GET /v1/admin/builtin` | The built-in speech engines: installed, state, process, GPU memory, log (admin token) |
| `POST /v1/admin/builtin/{stt\|tts}/install`, `PUT /v1/admin/builtin/{stt\|tts}` | Install an engine in the background; turn one on or off (`{"enabled": true}`, saved and applied at once) (admin token) |
| `POST /v1/admin/restart` | Restart the server with its saved settings (admin token) |
| `GET /v1/admin/personas`, `GET /v1/admin/personas/{name}` | List personas; read one (admin token) |
| `PUT /v1/admin/personas/{name}` | Replace a persona, or create it with `?create=true` (admin token) |
| `DELETE /v1/admin/personas/{name}` | Delete a persona not in use (admin token) |
| `POST /v1/admin/personas/{name}/activate` | Use this persona now and after restarts (admin token) |
| `GET /v1/admin/cameras` | Configured cameras and what each is doing (admin token) |
| `PUT /v1/admin/cameras` | Replace the camera list (`{"cameras": [...]}`); applied at once (admin token) |
| `POST /v1/admin/cameras/test` | One JPEG frame from camera settings, saved or not (admin token) |
| `GET /v1/admin/cameras/{id}/snapshot` | The current view of a running camera, as JPEG (admin token) |
| `GET /v1/admin/devices` | Connected devices, with the settings of those that offer them (admin token) |
| `PUT /v1/admin/devices/{id}/config` | Send settings to an edge device (`{"config": {...}}`); it saves them and restarts (admin token) |
| `GET /v1/admin/edge-profiles` | The hardware profiles in `client/edge/profiles` (admin token) |
| `WS /v1/admin/events` | Live events for the dashboard: `{"recent": [...]}`, then `{"event": {...}}` each; conversation text is reduced to its length (admin token, `?token=` in browsers) |
| `GET /v1/edge/package/<version>.tar.gz` | The edge client this server offers, for `device.update` (device token) |
| `GET /v1/admin/enrolled`, `POST /v1/admin/enrolled`, `DELETE /v1/admin/enrolled/{id}` | Boards with tokens of their own: list, create or replace a token (shown once), lock out (admin token) |
| `GET /v1/admin/enrolled/{id}/root-password` | The root password the installer gave a board (admin token) |
| `GET /v1/admin/edge/setup`, `POST /v1/admin/edge/install`, `GET /v1/admin/edge/install/{job}` | Boards on USB and this server's addresses; set a board up (a job with its steps) (admin token) |
| `GET /v1/admin/devices/{id}/logs`, `/logs/file`, `GET /v1/admin/device-logs` | A device's log lines, its whole log file, devices with logs (admin token) |
| `POST /v1/admin/devices/{id}/diagnostics` | Ask a device for its diagnostics (admin token) |
| `GET /v1/admin/devices/{id}/frame`, `GET /v1/admin/frames`, `POST /v1/admin/devices/{id}/capture` | A camera's newest frame; which devices sent one; ask for a fresh one (admin token) |
| `POST /v1/admin/devices/{id}/update`, `/restart`, `/speaker-test`, `/mic-test`; `GET`/`PUT .../wifi` | Look after a device remotely (admin token) |
| `GET /v1/admin/conversation`, `GET /v1/admin/recordings`, `GET /v1/admin/recordings/{id}/audio` | What was said (with `server.dashboard_conversation`), recordings (with `stt.keep_audio`) (admin token) |
| `GET /v1/admin/vision/images`, `GET /v1/admin/vision/images/{day}/{name}` | Images kept by `vision.store_images` (admin token) |
| `GET /v1/admin/updates`, `GET /v1/admin/updates/latest` | Version, update log and changelog; the newest release on GitHub (admin token) |
| `GET /v1/admin/status`, `/v1/admin/metrics` | `/v1/status` and `/v1/metrics` for the dashboard (admin token) |

### Realtime protocol (1.2)

1.1 added `device.config`, `device.config.set` and `device.config.result` (remote settings for devices); a 1.0 client never sees them. 1.2 added `device.log`, `device.diagnostics`, `device.update`, `device.restart` and `device.wifi.set` with their answers (looking after devices remotely); older devices ignore them, and the dashboard says they do not answer.

With a token configured, connect to `/v1/realtime?token=…` or send `Authorization: Bearer …`.

Text frames are JSON envelopes:

```json
{"type": "conversation.text", "session_id": "…", "device_id": "web-dev", "timestamp": "RFC3339", "payload": {"text": "こんにちは"}}
```

Binary frames carry a 4-byte big-endian header length, then the JSON envelope, then the raw payload: `[uint32 len][envelope JSON][bytes]`.

| Direction | Event | Payload |
|---|---|---|
| → | `session.start` (first frame) | `{session_id?, join?, roles?}`: resume or join a session; `join: true` without an id joins the most recently started one; `roles` ⊆ `mic`, `speaker`, `camera` (default `mic`, `speaker`) |
| → | `conversation.text` | `{text}` |
| → | `conversation.cancel` | stop the current reply |
| → | `audio.input.start` / `audio.input.stop` | `{sample_rate: 16000, encoding: "pcm_s16le" or "opus", channels: 1, full_duplex?, aec?}`; `full_duplex: true` keeps the mic open during replies (barge-in); `aec: "device"` if the device cancels its own echo |
| → | `audio.input.chunk` (binary) | PCM s16le mono 16 kHz; for `opus`, one or more packets each prefixed with a big-endian uint16 length. Optional payload `{pos, t}`: first sample's index in the mic stream and its capture time (device clock, ms since the epoch) |
| → | `device.status` | free-form device health, e.g. `{cpu_temp_c, mem_free_mb, wifi_signal_dbm, uptime_s}` |
| → | `device.config` | `{config, schema, editable, file?}`: the device's settings, after `session.started`, from devices that can be set up remotely; `schema`: `{path: {type, editable, choices?}}` (1.1) |
| → | `device.config.result` | `{request_id, ok, restarting, error?}`: answer to `device.config.set` (1.1) |
| → | `device.log` | `{boot, records: [{seq, t, level, logger, msg}]}`: the device's own log lines, also those logged while offline; `boot` changes per process start (1.2) |
| → | `device.diagnostics` | `{request_id, sections: {name: text}}`: answer to `device.diagnostics.request` (1.2) |
| → | `device.update.result` | `{request_id?, ok, version, restarting?, error?, previous?, connected?}`: answer to `device.update`; sent again with `connected: true` by the new version once connected (1.2) |
| → | `device.restart.result` | `{request_id, ok}` (1.2) |
| → | `device.wifi.result` | `{request_id, ok, networks, current, error?}`: answer to `device.wifi.set` (1.2) |
| → | `audio.output.played` | `{turn_id}`: playback finished; the server resumes listening |
| → | `audio.output.progress` | `{turn_id, seq, state: "started", pos?}`: a clip began playing; `pos` = mic stream position at that moment |
| → | `audio.output.stopped` | `{turn_id, reason?}`: answer to `audio.output.stop` |
| → | `system.pong` | `{ping_id, t_server, t_device}`: answer to `system.ping` with the device's clock |
| → | `vision.enable` / `vision.disable` | opt in or out of vision for this session |
| → | `vision.frame` (binary) | JPEG, PNG or WebP: `{mime, reason, frame_id?, captured_at?, event?}`; reason is `periodic`, `change` or `manual`; `event: {kind, score, area}` from a detector at the camera |
| → | `vision.event` | `{description or label, tags?, confidence?, salience?}`: an observation made by the client |
| → | `attention.quiet` | `{enabled}`: do-not-disturb on or off |
| ← | `session.started` | `{session_id, resumed, history, protocol, roles, devices, vision_enabled, …}` |
| ← | `session.devices` | `{devices: [{device_id, roles}]}`: a device joined or left |
| ← | `session.ended` | `{reason}`: to remaining camera devices when the conversation ends |
| ← | `system.state` | `{state, previous, reason?, turn_id?}`: `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `interrupting` or `recovering` |
| ← | `system.ping` | `{ping_id, t_server}`: clock sync and round trip; answer with `system.pong` |
| ← | `system.error` | `{code, message, recoverable}` |
| ← | `audio.vad` | `speech_start` or `speech_end` |
| ← | `conversation.transcript` | `{text, final}` |
| ← | `conversation.response.start` / `.delta` / `.done` | `{turn_id, text, cancelled?}`; `start` has `proactive: true` and `reason` when the companion spoke first; a cancelled `done` has `reason` and, if the device reported progress, `heard_text` |
| ← | `agent.tool.progress` | `{turn_id, tool, label, status}` |
| ← | `audio.output.chunk` (binary) | encoded audio for one sentence: `{turn_id, seq, mime, text}` |
| ← | `audio.output.done` | `{turn_id}` |
| ← | `audio.output.stop` | `{turn_id, reason}`: stop playing this turn now and drop the rest of its audio |
| ← | `vision.state` | `{enabled, available}` |
| ← | `vision.frame.status` | `{frame_id, status, detail?}`: `accepted`, `duplicate`, `rate_limited`, `rejected` or `disabled` |
| ← | `vision.observation` | `{id, timestamp, device_id, description, confidence, tags, source, source_event_id}` |
| ← | `vision.frame.used` | `{frame_id, turn_id}`: agent mode attached this frame to the turn |
| ← | `vision.capture.request` | `{request_id, reason}`: to `camera` devices: send a fresh `manual` frame now |
| ← | `attention.state` | `{proactive, quiet, speech_blockers}` |
| ← | `attention.decision` | `{kind, description, salience, source, decision, reasons}` (with `attention.debug`) |
| ← | `metrics.turn` | `{turn_id, <stage>: seconds…}` (with `metrics.emit_turn`) |
| ← | `device.config.set` | `{request_id, config}`: new settings from the admin dashboard; check them, save them and answer `device.config.result` (1.1) |
| ← | `device.diagnostics.request` | `{request_id}`: collect versions, checks, memory, disk, network, Wi-Fi and kernel messages (1.2) |
| ← | `device.update` | `{request_id, version, sha256}`: download `/v1/edge/package/<version>.tar.gz` from this server with the device token, check it, install it and restart (1.2) |
| ← | `device.restart` | `{request_id, what: "client" or "board"}` (1.2) |
| ← | `camera.live` | `{seconds, fps}`: send pictures (reason `live`) about `fps` times a second for `seconds`, for the dashboard's live view only; again to extend, `seconds: 0` stops (1.2) |
| ← | `device.wifi.set` | `{request_id, add: [{ssid, psk?}], remove: [ssid]}`: `psk` is the WPA key (64 hex digits), never the password; both empty: just report (1.2) |

A session can have several devices attached at once. Every device receives the conversation events; speech audio goes only to `speaker` devices, and only the device that sent `audio.input.start` feeds the microphone. The session ends when its last `mic` or `speaker` device disconnects; remaining camera devices then receive `session.ended` and are disconnected (a camera alone does not keep a conversation alive). Simple clients stay simple: a client that declares no roles gets `mic` and `speaker`, one that does not ask for `full_duplex` is treated as half duplex, and vision traffic only appears after a client sends `vision.enable`. Clients and server must ignore event types they do not know. The namespaces `conversation.*`, `audio.*`, `vision.*`, `memory.*`, `agent.*`, `system.*`, `session.*` and `device.*` are reserved.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` / `invalid peer certificate: UnknownIssuer` | A proxy or antivirus is inspecting HTTPS. The server already trusts the OS certificate store; for uv itself set `UV_NATIVE_TLS=1` (or pass `--native-tls`). |
| `cannot listen on 127.0.0.1:8765: the port is already in use or blocked` | Another instance is running, or Windows/Hyper-V reserved the port: use `--port`. |
| `provider_unavailable` … `cannot connect` | The agent or TTS server is not running, the URL is wrong, or a firewall blocks it. Check with `uv run companion check`. |
| `agent_empty_response` | The agent ran but produced no text. Usually no model provider is configured in Hermes/OpenClaw; check the agent's own logs. |
| `provider_auth` | The key in `.env` does not match the agent's `API_SERVER_KEY` or gateway token. |
| `server.host is '0.0.0.0' but no token is set` | Set `COMPANION_TOKEN` in `.env` before listening beyond localhost. |
| Dev client says the server needs a token | Open it once as `/dev/?token=<COMPANION_TOKEN>`. |
| Dashboard says it only answers this machine | Open it on the server as `http://127.0.0.1:…/admin/`, or set `COMPANION_ADMIN_TOKEN` in `.env` and restart. |
| STT runs on the CPU although you have a GPU | Install with `--extra cuda`; `check` prints the device it actually loaded. |
| The microphone or camera does not work from another device | Browsers allow them only on `localhost` or HTTPS. |
| **Start camera** is greyed out | `vision.enabled: false`, or VLM mode without a vision model (`vlm.provider: none`). |
| The agent ignores the camera image | Its model does not accept images: check `--mmproj` for llama.cpp and the model's `input` list in OpenClaw. |
| The first TTS reply takes very long | Irodori warms up on its first request; later sentences take well under a second on a GPU. |
| Replies take tens of seconds | Usually the agent's prompt processing or reasoning; `/v1/metrics` names the slowest stage. See [Performance and hardware](#performance-and-hardware). |
| The companion stops talking by itself | Its own voice (or a TV) is taken as you cutting in. Raise `realtime.barge_in_min_speech_ms`, use a microphone with echo cancellation, try `realtime.echo.canceller: nlms`, or set `full_duplex = false` on that device. |
| It answers its own voice | The echo guard missed it: lower `realtime.echo.guard_similarity`, or move the microphone away from the speaker. |
| **Set up** finds no board | Wait until it has booted (about 30 s), use a data USB cable and the right port, then **Look again**. `uv run companion edge discover` lists boards on USB; give the address under "another address" otherwise. |
| A board is set up but does not connect | Its log is under Devices (also when it is offline, once it was connected before). Over USB, `uv run companion edge doctor` shows its Wi-Fi state and log: usually a Wi-Fi network out of reach, or an address of the server it cannot reach (another network, firewall). |
| A board connects at home but not outside | Easiest: use [Tailscale](#reaching-the-server-from-outside) on the server and the board, and set the board up again with the server's Tailscale address. With the public listener, check `https://<your address>:8766/health` from your phone with Wi-Fi off: no answer means the router or firewall does not let port 8766 in, or your connection has no public address (see [Reaching the server from outside](#reaching-the-server-from-outside)). Set the board up again after turning on the public listener. |
| Choppy audio from a Bluetooth speaker | Set `output_lead_in_ms = 300`; make sure nothing else holds the bluealsa PCM (only one `aplay` can use it). |

## Development

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

## Roadmap

| Version | Outcome |
|---|---|
| **v0.1** | Windows-capable server: text and voice conversation, agent adapters, persistent memory |
| **v0.2** | Image ingestion: frames to a multimodal agent or a VLM, visual observations; v0.2.1 adds multi-device sessions and IP cameras |
| **v0.3** | Attention engine and controlled proactive conversation |
| **v0.4** | Portable Linux edge client (camera, mic, speaker) talking to the home server, tested on a LicheeRV Nano with a Bluetooth speaker |
| **v0.5** | Realtime multimodal interaction: full-duplex audio with barge-in, echo handling, state machine with recovery, latency metrics, change-driven vision |
| **v1.0** | First stable release: protocol 1.0, speech recognition on another host, several camera images per turn, measured end to end from an edge board through the server and agent and back |
| **v1.1** | Admin dashboard: status, latency, every setting (applied live where possible, in-process restart otherwise), personas, cameras run by the server, edge devices set up remotely (protocol 1.1, edge client 1.1) from hardware profiles; a one-file device template |
| **v1.2** | Boards from zero: first-run setup, boards set up over USB from the dashboard with tokens of their own, their logs, diagnostics, camera and conversations in the dashboard, remote updates with automatic rollback and Wi-Fi changes, and a public listener with the server's own TLS certificate for boards away from home (protocol 1.2, edge client 1.2) |

Possible directions, not scheduled: several users per server, native mobile clients, a plugin interface for third-party devices and providers, and ready-made hardware.

## License

[Mozilla Public License 2.0](LICENSE). You may use the server in larger works under other licenses, but changes to files covered by the MPL must be shared under the MPL.

Third-party components (Hermes Agent, OpenClaw, faster-whisper, Irodori-TTS, Pillow, and the models they download) are separate projects with their own licenses. The Silero VAD model in `companion/audio/` is under the MIT license (see the `.LICENSE` file next to it). The built-in speech engines download faster-whisper, Irodori-TTS-Server, Irodori-TTS, PyTorch and their models when you install and start them.
