# Companion Server

An open-source, self-hosted AI companion server. Talk to it by voice or text from a browser or from a small Linux board with a camera, microphone and speaker; it listens, can look through your camera when you allow it, thinks through the agent framework of your choice, and answers with streaming text and speech.

**Status:** v1.4.0. The realtime protocol is version 1.2: 1.x releases only add events and optional fields, so a client written for 1.0 keeps working as long as it ignores what it does not recognize.

- **Bring your own agent**: [Hermes Agent](https://github.com/NousResearch/hermes-agent), [OpenClaw](https://openclaw.ai), or any OpenAI-compatible LLM, on this machine or another one.
- **Conversational**: streaming replies spoken sentence by sentence; talk over the companion and it stops to listen.
- **A body for the companion**: a small Linux board (LicheeRV Nano tested) with microphone, speaker and camera, set up over USB from the dashboard and looked after from it, at home or away.
- **Opt-in vision and proactive speech**: it looks only when a session allows it, and speaks first only within strict limits.
- **Knows who is around** (opt-in): recognises you and the people you add by face and voice, tells who said what, answers only what is meant for it, and keeps a timeline of what everyone has been doing, with a local vision model and tools for the agent over MCP.
- **Windows first, Linux supported**, with local speech recognition and synthesis it can install itself.
- **Private by default**: with an external agent nothing of the conversation is written to disk; audio and images are never stored unless you turn that on.

## Quick start

Download `companion-mesh-<version>-windows.zip` (or `-linux.tar.gz`) from the [Releases](https://github.com/Nero4541/CompanionMesh/releases) page and unpack it, or clone the repository. You need [uv](https://docs.astral.sh/uv/) and an agent (Hermes, OpenClaw or an OpenAI-compatible LLM). Then, in the folder:

```powershell
uv python install 3.13
uv sync
```

Double-click **`start-companion.bat`** (Linux: `./start-companion.sh`). The first time it asks for your agent's address and key and how speech should work, then starts the server and opens the dashboard at **http://127.0.0.1:8765/admin/**. Talk to the companion at **http://127.0.0.1:8765/dev/**.

Details, options and Linux: [Getting started](docs/getting-started.md).

## A board as the companion's body

Flash the board's SD card with the maker's image, plug the board into the server's computer by USB, and press **Set up** on the dashboard's **Boards** page. Nothing has to be typed on the board. The dashboard then shows its log, camera and diagnostics, and updates it remotely. See [Edge devices](docs/edge-devices.md).

## Away from home

For a board on mobile data or another Wi-Fi, use [Tailscale](https://tailscale.com) on the server and the board: no router or firewall changes, and it also works next to another VPN on the server. See [Reaching the server from outside](docs/remote-access.md).

## Updating

Stop the server and run **`update-companion.bat`** (Linux: `./update-companion.sh`): it installs the newest release, keeps your settings and data, and backs up what it replaces. See [Updating](docs/updating.md) and the [changelog](CHANGELOG.md).

## Documentation

| | |
|---|---|
| [Getting started](docs/getting-started.md) | How it works, requirements, installation |
| [Configuration](docs/configuration.md) · [Admin dashboard](docs/dashboard.md) | Settings, secrets, where data lives |
| [Agents](docs/agents.md) · [Speech](docs/speech.md) · [Vision](docs/vision.md) · [People](docs/people.md) | The agent and its tools, speech, cameras, who is around |
| [Realtime conversation](docs/realtime.md) · [Proactive behavior](docs/proactive.md) · [Performance](docs/performance.md) | Interrupting, speaking first, latency and hardware |
| [Edge devices](docs/edge-devices.md) · [Reaching the server from outside](docs/remote-access.md) | Boards, at home and away |
| [API](docs/api.md) · [Troubleshooting](docs/troubleshooting.md) · [Development](docs/development.md) · [Roadmap](docs/roadmap.md) | Reference |

All pages: [docs/](docs/README.md).

## License

[Mozilla Public License 2.0](LICENSE). You may use the server in larger works under other licenses, but changes to files covered by the MPL must be shared under the MPL.

Third-party components (Hermes Agent, OpenClaw, faster-whisper, Irodori-TTS, Pillow, and the models they download) are separate projects with their own licenses. The Silero VAD model in `companion/audio/` is under the MIT license (see the `.LICENSE` file next to it). The built-in speech engines download faster-whisper, Irodori-TTS-Server, Irodori-TTS, PyTorch and their models when you install and start them.
