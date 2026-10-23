# Getting started

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

The server streams the agent's reply text to the client as it arrives. Each finished sentence is synthesized and played while the rest of the reply is still being generated. The microphone stays open while the companion speaks, so you can interrupt it; see [Realtime conversation](realtime.md).

## Requirements

| | Required | Notes |
|---|---|---|
| OS | Windows 10/11 or Linux | |
| [uv](https://docs.astral.sh/uv/) | yes | Installs Python 3.13 and all dependencies. No Conda, no manual `pip`. |
| Agent backend | yes | Hermes, OpenClaw, or any OpenAI-compatible LLM (`direct`) |
| NVIDIA GPU | recommended | For Whisper and the built-in voice. Falls back to CPU automatically. |
| STT/TTS servers | optional | Any OpenAI-compatible server, or none: the server can install and run [built-in speech](speech.md#built-in-speech) itself. Without any TTS the companion replies in text only. |
| Vision | optional | For the camera: the LLM behind your agent must be multimodal. Otherwise set `vision.mode: vlm` and configure a vision model. |
| Browser | for the dev client | Chrome or Edge recommended. The microphone needs `localhost` or HTTPS. |

## Quick start (Windows, PowerShell)

```powershell
git clone https://github.com/Nero4541/CompanionMesh.git
cd CompanionMesh

# Python 3.13 + dependencies
uv python install 3.13
uv sync

# Secrets
Copy-Item .env.example .env
notepad .env

# Validate the configuration and reach every provider
uv run companion check

# Start the server
uv run companion
```

Instead of editing `.env` and the config by hand, run `uv run companion setup`: it asks for the agent's address and key (and tests them), speech recognition and synthesis, and whether boards connect over the network or from away from home, then writes `.env` (with new device and admin tokens) and `configs/companion.local.yaml`. Run it again any time; Enter keeps each current value. `start-companion.bat` / `.sh` runs it by itself on the first start, when there is no `.env` yet. Without a speech recognition or voice server of your own, choose **Built-in** for both: setup then installs them for the server to run itself (see [Built-in speech](speech.md#built-in-speech)).

Open **http://127.0.0.1:8765/dev/**, press **Start mic** and speak, or type a message. **Start camera** lets the companion see. This needs a multimodal LLM behind the agent, or `vision.mode: vlm` with a vision model configured (see [Vision](vision.md)).

**http://127.0.0.1:8765/admin/** is the admin dashboard: server status, connected devices, latency per pipeline stage and every setting (see [Admin dashboard](dashboard.md)).

After the first setup, double-click **`start-companion.bat`** in the project folder (on Linux, run `./start-companion.sh`): it starts the server (or finds it already running) and opens the dashboard once the server answers.

Instead of `git clone`, you can download a release from the [Releases](https://github.com/Nero4541/CompanionMesh/releases) page: `companion-mesh-<version>-windows.zip` or `companion-mesh-<version>-linux.tar.gz`. Unpack it (to a `companion-mesh` folder) and continue with `uv python install 3.13` above; `SHA256SUMS` lists the checksums.

On Linux, use `cp .env.example .env` instead of `Copy-Item` and `./start-companion.sh` instead of the `.bat`; every other command is the same. Without an NVIDIA GPU, built-in speech recognition runs on the CPU. That is slower, so consider a smaller model (`builtin.stt.model: small`; setup offers it).

`uv run companion check` prints one line per component (config, persona, agent, STT, VAD, TTS) and exits non-zero if anything is unreachable. Run it first whenever something does not work.
