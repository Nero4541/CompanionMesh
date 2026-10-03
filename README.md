# Companion Server

An open-source, self-hosted AI companion server. Talk to it by voice or text from a browser (and, later, from small edge devices); it listens, can look through your camera when you allow it, thinks through the agent framework of your choice, and answers with streaming text and speech.

**Status:** v0.2.1. This is an early release; the protocol may still change before v1.0.

- **Windows first, Linux supported.** Runs natively on Windows (PowerShell, no WSL required for the server itself) and on Linux.
- **Bring your own agent.** [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [OpenClaw](https://openclaw.ai) are supported through their OpenAI-compatible APIs. Without a framework, the `direct` backend talks to any OpenAI-compatible LLM.
- **Several devices, one conversation.** A browser can keep the mic and speaker while an IP camera (or a phone running a camera app) joins the same session as the companion's eyes.
- **Agents can run elsewhere.** Point the companion at an agent on another machine (LAN or Tailscale). Memory and tools stay on that machine.
- **Opt-in vision.** A client can share camera frames with the agent, either as images for a model that can see or as short descriptions from a separate vision model. Off until a session enables it.
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

The server streams the agent's reply text to the client as it arrives. Each finished sentence is synthesized and played while the rest of the reply is still being generated. v0.1.x is half-duplex: the microphone is ignored while the companion is thinking or speaking, so it never answers its own voice.

## Requirements

| | Required | Notes |
|---|---|---|
| OS | Windows 10/11 or Linux | |
| [uv](https://docs.astral.sh/uv/) | yes | Installs Python 3.13 and all dependencies. No Conda, no manual `pip`. |
| Agent backend | yes | Hermes, OpenClaw, or any OpenAI-compatible LLM (`direct`) |
| NVIDIA GPU | recommended | For Whisper. Falls back to CPU automatically. |
| TTS server | optional | Any OpenAI-compatible `/v1/audio/speech` server. Without one the companion replies in text only. |
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

Open **http://127.0.0.1:8765/dev/**, press **Start mic** and speak, or type a message. **Start camera** lets the companion see. This needs a multimodal LLM behind the agent, or `vision.mode: vlm` with a vision model configured (see [Vision](#vision)).

On Linux, use `cp .env.example .env` instead of `Copy-Item`; every other command is the same. Without an NVIDIA GPU, drop `--extra cuda`; speech recognition then runs on the CPU. That is slower, so consider a smaller model (`stt.model: small`).

`uv run companion check` prints one line per component (config, persona, agent, STT, VAD, TTS) and exits non-zero if anything is unreachable. Run it first whenever something does not work.

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

Microphone audio and synthesized speech are never written to disk. Camera images are never written to disk unless `vision.store_images: true`; they are then kept under `data/vision/` for `vision.retention_days`. Logs contain transcripts at `INFO` level; raise `logging.level` to `WARNING` if you do not want that.

With an external agent, reconnecting with the same session keeps the conversation going (the agent remembers), but the dev client will not replay earlier messages.

## Speech

### Speech-to-text

The default is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with `large-v3-turbo`. The model is downloaded on first start (about 1.6 GB).

```yaml
stt:
  model: large-v3-turbo   # small / medium / large-v3 / …
  language: ja            # null = auto-detect
  device: auto            # tries CUDA first, falls back to CPU
```

The `cuda` extra installs cuBLAS and cuDNN from PyPI, so no separate CUDA toolkit is needed. Voice activity detection uses Silero VAD (bundled with faster-whisper). An utterance ends after `vad.min_silence_ms` (default 1000 ms) of silence; lower it for snappier turns, raise it if you are cut off mid-sentence.

### Text-to-speech

Any server implementing OpenAI's `POST /v1/audio/speech` works. Each sentence is synthesized as it completes. Vendor-specific options go in `tts.extra_body` and are merged into every request.

The default configuration targets [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server), a Japanese TTS server (MIT; check the model cards for the weights' licenses):

```powershell
git clone https://github.com/Aratako/Irodori-TTS-Server.git
cd Irodori-TTS-Server
uv sync --extra cu128
Copy-Item .env.example .env   # e.g. IRODORI_HF_CHECKPOINT=Aratako/Irodori-TTS-v4.1-Small-MF
uv run --no-sync python -m irodori_openai_tts --host 127.0.0.1 --port 8088
```

`tts.voice: none` synthesizes without reference audio. For a stable character voice, add a reference clip to the server's `voices/` directory and set `tts.voice` to its id. Set `tts.provider: none` for text-only replies.

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

`vision.mode: agent` hands the image straight to the agent: your next message is sent as text plus the newest frame (an OpenAI `image_url` part). The agent's own model does the seeing, and its memory records what it saw. Each frame is sent at most once and only if it is younger than `vision.observation_max_age_s`; the companion keeps no image after the turn.

This needs an agent backend and model that accept images:

- **Hermes / OpenClaw** pass `image_url` parts through. In OpenClaw, the model definition must list image input, e.g. `"input": ["text", "image"]`, or the image is not forwarded.
- **llama.cpp** must be started with the model's multimodal projector (`--mmproj …`).
- **`direct`**: `llm` must point at a model that accepts images.

Images add to the prompt the model has to process; on a slow GPU, a smaller `vlm.max_image_side` (e.g. 512) keeps turns faster.

### VLM mode

`vision.mode: vlm` uses a separate vision model (the `vlm` section) to describe each accepted frame. Only the newest waiting frame is analyzed; descriptions arrive as `vision.observation` events, are kept as episodic memory, and are passed to the agent as text. Use it when the agent's model cannot see, or to keep images away from the agent entirely.

New observations are passed with your next message (`vision.inject: user`), which keeps the agent's prompt cache intact; each is included once. `vision.inject: system` instead lists recent observations in the system prompt on every turn. A turn waits up to `vision.wait_for_pending_s` for a frame that is still being analyzed.

### IP cameras and phones

Any camera reachable over HTTP or RTSP can be the companion's eyes. `companion camera` runs a small device process that joins the active conversation with the `camera` role, switches vision on, and sends frames (periodically and whenever the server asks):

```powershell
# A phone running an "IP camera" app on the same network (or on Tailscale)
uv run companion camera http://192.168.1.20:8080/shot.jpg      # HTTP snapshot
uv run companion camera http://192.168.1.20:8080/video         # MJPEG stream
uv run companion camera rtsp://192.168.1.20:8554/live          # RTSP, needs ffmpeg on PATH
```

The source type is detected from the URL and the response. Options: `--interval 5` (seconds between frames; `0` = only on request), `--session <id>` to join a specific session instead of the most recently started one, `--device-id`, and `--no-enable` to leave vision off until someone enables it. The bridge reconnects automatically. If vision is switched off in the session (for example with the **vision on** button in the dev client), the camera pauses and stays paused until vision is switched on again.

Start the browser conversation first, then the camera; it waits and retries until there is a session to join.

In both modes, clients that detect things themselves, such as a future edge device, can send `vision.event` with a description; it becomes an observation without any model call.

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

The companion server itself needs a GPU only for STT. STT can stay on the machine running the companion while the agent, LLM and TTS run on a server.

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
| `personas/<name>.yaml` | System prompt, language, voice (`agent.persona`) | yes |

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

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness |
| `GET /v1/status?probe=true` | Version, sessions, providers, agent reachability |
| `GET /v1/config` | Effective configuration (secret values are never included) |
| `POST /v1/chat` | One text turn: `{"text": "...", "session_id": "optional"}` → `{"session_id", "text"}` |
| `WS /v1/realtime` | Streaming text and voice (below) |
| `GET /dev/` | Browser development client |

### Realtime protocol (v0.2.1)

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
| → | `audio.input.start` / `audio.input.stop` | `{sample_rate: 16000, encoding: "pcm_s16le", channels: 1}` |
| → | `audio.input.chunk` (binary) | PCM s16le mono 16 kHz |
| → | `audio.output.played` | `{turn_id}`: playback finished; the server resumes listening |
| → | `vision.enable` / `vision.disable` | opt in or out of vision for this session |
| → | `vision.frame` (binary) | JPEG, PNG or WebP: `{mime, reason, frame_id?}`; reason is `periodic`, `change` or `manual` |
| → | `vision.event` | `{description or label, tags?, confidence?}`: an observation made by the client |
| ← | `session.started` | `{session_id, resumed, history, protocol, roles, devices, vision_enabled, …}` |
| ← | `session.devices` | `{devices: [{device_id, roles}]}`: a device joined or left |
| ← | `system.state` | `idle`, `listening`, `transcribing`, `thinking` or `speaking` |
| ← | `system.error` | `{code, message, recoverable}` |
| ← | `audio.vad` | `speech_start` or `speech_end` |
| ← | `conversation.transcript` | `{text, final}` |
| ← | `conversation.response.start` / `.delta` / `.done` | `{turn_id, text, cancelled?}` |
| ← | `agent.tool.progress` | `{turn_id, tool, label, status}` |
| ← | `audio.output.chunk` (binary) | encoded audio for one sentence: `{turn_id, seq, mime, text}` |
| ← | `audio.output.done` | `{turn_id}` |
| ← | `vision.state` | `{enabled, available}` |
| ← | `vision.frame.status` | `{frame_id, status, detail?}`: `accepted`, `duplicate`, `rate_limited`, `rejected` or `disabled` |
| ← | `vision.observation` | `{id, timestamp, device_id, description, confidence, tags, source, source_event_id}` |
| ← | `vision.frame.used` | `{frame_id, turn_id}`: agent mode attached this frame to the turn |
| ← | `vision.capture.request` | `{request_id, reason}`: to `camera` devices: send a fresh `manual` frame now |

A session can have several devices attached at once. Every device receives the conversation events; speech audio goes only to `speaker` devices, and only the device that sent `audio.input.start` feeds the microphone. The session ends when its last device disconnects. v0.1 clients keep working unchanged: they get the default roles, and vision traffic only appears after a client sends `vision.enable`. Clients and server must ignore event types they do not know. The namespaces `conversation.*`, `audio.*`, `vision.*`, `memory.*`, `agent.*`, `system.*` and `session.*` are reserved.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` / `invalid peer certificate: UnknownIssuer` | A proxy or antivirus is inspecting HTTPS. The server already trusts the OS certificate store; for uv itself set `UV_NATIVE_TLS=1` (or pass `--native-tls`). |
| `cannot listen on 127.0.0.1:8765: the port is already in use or blocked` | Another instance is running, or Windows/Hyper-V reserved the port: use `--port`. |
| `provider_unavailable` … `cannot connect` | The agent or TTS server is not running, the URL is wrong, or a firewall blocks it. Check with `uv run companion check`. |
| `agent_empty_response` | The agent ran but produced no text. Usually no model provider is configured in Hermes/OpenClaw; check the agent's own logs. |
| `provider_auth` | The key in `.env` does not match the agent's `API_SERVER_KEY` or gateway token. |
| STT runs on the CPU although you have a GPU | Install with `--extra cuda`; `check` prints the device it actually loaded. |
| The microphone or camera does not work from another device | Browsers allow them only on `localhost` or HTTPS. |
| **Start camera** is greyed out | `vision.enabled: false`, or VLM mode without a vision model (`vlm.provider: none`). |
| The agent ignores the camera image | Its model does not accept images: check `--mmproj` for llama.cpp and the model's `input` list in OpenClaw. |
| The first TTS reply takes very long | Irodori warms up on its first request; later sentences take well under a second on a GPU. |
| Replies take tens of seconds | Usually the agent's prompt processing or reasoning; see [Performance and hardware](#performance-and-hardware). |

## Development

```powershell
uv sync --extra stt
uv run ruff check .
uv run ruff format .
uv run companion check      # validate config and reach every provider
```

CI runs lint and an import check on Windows and Linux.

```text
companion/
  api/         FastAPI app, HTTP routes, WebSocket transport
  core/        config, runtime, session state machine, sentence splitter, event bus
  agent/       Hermes, OpenClaw and direct backends
  audio/       PCM helpers, VAD, utterance segmentation
  vision/      image validation, dedup, observations, per-session vision pipeline
  devices/     device processes: IP camera bridge and frame sources
  memory/      transcript stores (in-memory, SQLite)
  protocol/    envelope and binary frame codec, event names
  providers/   llm / stt / tts / vlm implementations
client/web/    browser development client
configs/       default configuration
personas/      persona definitions
```

## Roadmap

| Version | Outcome |
|---|---|
| **v0.1** | Windows-capable server: text and voice conversation, agent adapters, persistent memory ✅ |
| **v0.2** | Image ingestion: frames to a multimodal agent or a VLM, visual observations; v0.2.1 adds multi-device sessions and IP cameras |
| v0.3 | Attention engine and controlled proactive conversation |
| v0.4 | Portable Linux edge client (camera, mic, speaker) talking to the home server |
| v0.5 | Realtime multimodal interaction: continuous vision, echo-aware audio, barge-in |

## License

[Mozilla Public License 2.0](LICENSE). You may use the server in larger works under other licenses, but changes to files covered by the MPL must be shared under the MPL.

Third-party components (Hermes Agent, OpenClaw, faster-whisper, Irodori-TTS, Pillow, and the models they download) are separate projects with their own licenses.
