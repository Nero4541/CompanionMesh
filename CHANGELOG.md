# Changelog

What changed in each release of the Companion Server and its edge client. The newest is first. To update, stop the server and run `update-companion.bat` (Windows) or `./update-companion.sh` (Linux); see the README, "Updating".

## Unreleased

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

First stable release: protocol 1.0, speech recognition on another host, several camera images per turn, measured end to end from an edge board through the server and agent and back. Earlier versions (0.1 to 0.5) are summarized in the README, "Roadmap".
