# Changelog

What changed in each release of the Companion Server and its edge client. The newest is first. To update, stop the server and run `update-companion.bat` (Windows) or `./update-companion.sh` (Linux); see [docs/updating.md](docs/updating.md).

## Unreleased

Protocol 1.3 (additive), edge client 1.3.0.

- **Follow the companion on your phone** (`server.viewer`, off by default): a read-only page at `/view/` for phones and computers on the server's Tailscale network, with the conversation as it happens and the connected devices with what their cameras see (newest picture and live pictures). Nobody outside the tailnet gets in; the Conversations page says where to open it.
- **Conversations page, newest first, recordings in place**: the conversation now reads from the newest line down, and the recordings table is gone: each line you said carries its recording (Listen, length, recognition time), and utterances that never got an answer show among the lines, greyed out.
- **Bluetooth speakers from the dashboard**: a device's **Maintenance** tab pairs a Bluetooth speaker (look for speakers in pairing mode, or type the address), plays through it from then on and keeps it connected, also after a reboot; **Forget** goes back to the output from before. Needs BlueZ and bluez-alsa on the board (Sipeed's LicheeRV Nano image has both).
- **Tailscale for boards from the dashboard**: **Put the board on Tailscale** when setting it up over USB (also `companion edge install --tailscale`), or **Install and join** in the Maintenance tab. The server downloads Tailscale's static build for the board from pkgs.tailscale.com, checks it and copies it over; the board joins with an auth key or through a login link shown on the page, and then also tries the server's Tailscale address, so it reaches the server away from home.
- **A new address after a Wi-Fi switch** ([#1](https://github.com/Nero4541/CompanionMesh/issues/1)): when a board moved to another Wi-Fi network (the hotspot went away and home came in range, or networks were changed from the dashboard or over USB), udhcpc kept the old network's address and the board was cut off until a reboot. The edge client now asks udhcpc for a new address whenever the network changes, and the installer does the same after changing Wi-Fi.
- **Bluetooth speakers named in `~/.asoundrc` play after a reboot** ([#2](https://github.com/Nero4541/CompanionMesh/issues/2)): started at boot, the edge client had `HOME=/`, so ALSA missed `/root/.asoundrc` and nothing played (`audio open error: No such file or directory`), although `aplay` over SSH worked. The service script sets `HOME`, and the client sets it itself when it is missing (for boards updated remotely).
- **Boards are set up with this network's address**: the server address for a board (Boards page and `companion edge install`) was this server's Tailscale address whenever it had one, which a board not on Tailscale never reaches (`server unreachable: TimeoutError`). It is now this network's address; a board put on Tailscale gets the Tailscale one as a further address, and picking the Tailscale address for a board that is not put on it shows a warning. Updating a board keeps the address it uses unless you pick another (the page says which one it uses; `companion edge update` likewise, unless `--server` is given), and **Put the board on Tailscale** is ticked for a board on the tailnet already (remembered by the installer and the Maintenance tab).
- **Log a phone in with a QR code**: a **Phone** card on the Overview page makes the dashboard's admin token if there is none (saved in `.env`, used at once, no restart) and shows a QR code that opens the dashboard on a phone and keeps it logged in, for this machine's Tailscale address or one on this network (or a link to copy); **New token** logs every phone out. A phone whose camera app does not open links can press **Scan the QR code** on the dashboard's sign-in page: it takes a photo of the code and reads it in the page. This machine itself no longer needs the token once one is set: before, setting it made the dashboard on the server ask for it too.
- **A quieter console**: web requests that went well (the dashboard asks every few seconds) are no longer logged at `INFO`; failed ones still are, and `logging.level: DEBUG` shows them all.
- **An installation always uses its own settings and data**: the server took `configs/`, `.env` and `data/` from the folder it was started in, so one installation started from another's folder ran with the other's settings and boards (its set-up boards seemed gone). It now uses those of the folder it is installed in, wherever it is started; `--config` and `COMPANION_CONFIG` still choose another configuration file.
- **Board passwords stay as they are**: setting a board up (or updating it) replaces the root password only when it is the stock one (or empty). A password of the board's own, or the one this server set before, stays as it is; before, a board logged in with this server's key or with a password you gave got a new random one. The installer now asks the board itself which password it has; a password you give that the board has is remembered like the one this server set.
- **Whisper runs only as built-in speech recognition**: speech recognition now comes from the built-in engine (`builtin.stt`), a server of your own or a service (`stt.provider: openai_compatible`), like the voice. `stt.provider: faster_whisper`, the shipped default, ran Whisper inside the server process, which took GPU memory with every built-in engine off. It is gone, and so are `stt.device`, `stt.compute_type` and `stt.beam_size`: a configuration that still has them turns on `builtin.stt` with the same model and settings (install it on the Speech page if it is not installed yet). The `stt` and `cuda` extras install nothing now; `uv sync` is enough. The shipped default is `stt.provider: openai_compatible`; `companion setup` asks as before.
- **Built-in vision model** (`builtin.vlm`, new **Vision** page, off by default): without a vision model server of your own, the server installs and runs [Mage-VL](https://huggingface.co/microsoft/Mage-VL) itself, like the built-in speech: in FP8 with fused kernels built for the GPU on RTX 40 and newer, BF16/FP16 on older cards. While on, it is used instead of `vlm.provider` (for `vision.mode: vlm`); it serves the OpenAI chat API like any other vision model. See [docs/vision.md](docs/vision.md#built-in-vision-model).

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
