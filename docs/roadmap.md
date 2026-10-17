# Roadmap

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
