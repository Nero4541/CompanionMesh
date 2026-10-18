# Changelog

What changed in each release of the Companion Server and its edge client. The newest is first. To update, stop the server and run `update-companion.bat` (Windows) or `./update-companion.sh` (Linux); see [docs/updating.md](docs/updating.md).

## Unreleased

- **Board passwords stay as they are**: setting a board up (or updating it) replaces the root password only when it is the stock one (or empty). A password of the board's own, or the one this server set before, stays as it is; before, a board logged in with this server's key or with a password you gave got a new random one. The installer now asks the board itself which password it has.

## 1.4.0 — 2026-10-18

Built-in speech in its own folder with a model picker, every setting explained, and clearer setup questions. Protocol 1.2 and edge client 1.2.0, both unchanged.

- **Every setting explained**: the Configuration page says under each setting, and each group, what it does in plain words, with units, examples and when to change it, for people new to the project. The tags now read **applies now** and **needs restart** (were *live* and *restart*).
- **Clearer setup questions**: when a server you entered does not answer, setup asks "Save these settings anyway?" (No, or Enter, asks for them again) instead of "Change these settings?". Speech is now two parts that no longer mirror each other: *Hearing* (speech recognition, your voice into text) and *Speaking* (the companion's voice), with "speech recognition server" and "voice server" instead of speech-to-text and text-to-speech.
- **Built-in speech keeps everything in its own folder**: `builtin/stt` and `builtin/tts` hold the engine's environment (`.venv`), its models (`models/<name>`, one folder each) and, for the voice, `voices/` with reference clips and speaker embeddings. Nothing of it goes to `data/builtin` or the user's Hugging Face cache any more, and updates leave it alone. `builtin.stt.model` and `builtin.tts.checkpoint` name a folder in `models/` (a size or repository not there yet is downloaded there first); the Speech page lists the folders and switches between them. Voices in `data/builtin/tts/voices` are copied over; the old `data/builtin` can be deleted (the engines install again, from uv's cache, and download their models once more).

## 1.3.0 — 2026-10-17

Built-in speech, and fixes for speech from boards. Protocol 1.2 and edge client 1.2.0, both unchanged.

- **Built-in speech**: without an STT or TTS server, the server installs and runs Whisper (faster-whisper) and Irodori-TTS itself, each in an environment of its own under `data/builtin`. Choose **Built-in** in `companion setup`, or install them from the new **Speech** page of the dashboard. Each has an on/off switch that applies at once: off, its process ends and its GPU memory is free; the engines also end with the server. Settings under `builtin`.
- **Speech server addresses**: an STT URL ending in `/audio/transcriptions` (or a TTS URL ending in `/audio/speech`) is cut back to the API's base; before, the endpoint was added twice and every request failed with 404. Setup now tests the STT and TTS servers you enter, and asks again when one does not answer like an OpenAI API.
- **`stt.prompt`**: a short text that steers how speech recognition writes, such as Cantonese in traditional characters. Sent with every utterance, also to OpenAI-compatible servers (`prompt`); applies at once. None by default.
- **Speech from boards with speech recognition on another machine**: voice activity detection and the Opus decoder no longer come with the `stt` and `opus` extras, which a server with remote speech recognition does not install. The board's microphone sent audio, but no speech was ever detected (`companion check`: `vad FAILED: onnxruntime is not installed`). Silero VAD's model now comes with the server, and onnxruntime and PyAV are base dependencies; `--extra opus` still works but is no longer needed.

## 1.2.0

Boards from zero, and looking after them from the dashboard. Protocol 1.2 (additive: 1.x clients keep working), edge client 1.2.0.

- **First-run setup**: `uv run companion setup` asks for the agent's address and key (and tests them), speech, and whether boards connect over the network, then writes `.env` and the local config. The start scripts run it when there is no `.env` yet.
- **Boards page**: set a board up over USB from the dashboard (or `companion edge install`): SSH key, a random root password instead of the stock one, a token of its own, the edge client, settings from the hardware profile, Wi-Fi networks, a service that starts at boot. **Update over USB** brings a board up to date (software, settings, Wi-Fi) and keeps its token. **Lock out** stops a lost board at once.
- **Per device** (Devices page): its log (also what it logged while offline), diagnostics, the camera's newest picture, **Take a picture now** and a **Live** view, and maintenance: update the edge client remotely (it goes back to the old version by itself if the new one cannot connect), restart, reboot, Wi-Fi networks, speaker and microphone tests.
- **Conversations page**: what was heard and said (`server.dashboard_conversation`) and recordings of what reached speech recognition (`stt.keep_audio`). Both off by default.
- **Away from home**: Tailscale on the server and the board is the recommended way; the Boards page offers the server's Tailscale address. Without Tailscale, an optional public listener (`server.public`) with the server's own TLS certificate, for a router port forward.
- **Updates page** and `update-companion.bat` / `.sh`: update to the newest release from GitHub, with a backup and a log of every update.
- The dashboard refreshes devices and boards live.
- `companion edge doctor` shows what a board connected by USB says about itself when it cannot connect.

## 1.1.0 — 2026-10-16

Admin dashboard. Protocol 1.1, edge client 1.1.0.

- **Admin dashboard** (`/admin/`): status, providers, sessions, devices and latency per pipeline stage; every setting, checked before saving (live where possible, otherwise an in-process restart that falls back to the previous settings); personas; IP cameras and phones run by the server; edge devices set up remotely from hardware profiles. Its own admin token.
- Edge client 1.1.0: saves settings changed from the dashboard and restarts with them in the same process.
- Camera-only edge devices join the conversation someone else started.
- Hardware profiles for the LicheeRV Nano, a Raspberry Pi with a speakerphone, Bluetooth speakers, speaker only and camera only.
- `start-companion.bat` / `.sh`, and a one-file device template (`client/template/device.py`).

## 1.0.0

First stable release: protocol 1.0, speech recognition on another host, several camera images per turn, measured end to end from an edge board through the server and agent and back. Earlier versions (0.1 to 0.5) are summarized in [docs/roadmap.md](docs/roadmap.md).
