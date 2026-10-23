# Troubleshooting

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
| Built-in speech recognition runs on the CPU although you have a GPU | Set `builtin.libraries: cuda` and `builtin.stt.device: auto` or `cuda`, then press **Install again** on the Speech page; the engine's log there says which device it loaded. |
| The microphone or camera does not work from another device | Browsers allow them only on `localhost` or HTTPS. |
| **Start camera** is greyed out | `vision.enabled: false`, or VLM mode without a vision model (`vlm.provider: none`). |
| The agent ignores the camera image | Its model does not accept images: check `--mmproj` for llama.cpp and the model's `input` list in OpenClaw. |
| The first TTS reply takes very long | Irodori warms up on its first request; later sentences take well under a second on a GPU. |
| Replies take tens of seconds | Usually the agent's prompt processing or reasoning; `/v1/metrics` names the slowest stage. See [Performance and hardware](performance.md). |
| The companion stops talking by itself | Its own voice (or a TV) is taken as you cutting in. Raise `realtime.barge_in_min_speech_ms`, use a microphone with echo cancellation, try `realtime.echo.canceller: nlms`, or set `full_duplex = false` on that device. |
| It answers its own voice | The echo guard missed it: lower `realtime.echo.guard_similarity`, or move the microphone away from the speaker. |
| The VPN will not connect while Tailscale runs, or boards are unreachable while the VPN is on | See [When the server also runs a VPN](remote-access.md#when-the-server-also-runs-a-vpn): no Tailscale exit node, `tailscale set --accept-dns=false`, and `tailscaled.exe` excluded from the VPN. |
| **Set up** finds no board | Wait until it has booted (about 30 s), use a data USB cable and the right port, then **Look again**. `uv run companion edge discover` lists boards on USB; give the address under "another address" otherwise. |
| A board is set up but does not connect | Its log is under Devices (also when it is offline, once it was connected before). Over USB, `uv run companion edge doctor` shows its Wi-Fi state and log: usually a Wi-Fi network out of reach, or an address of the server it cannot reach (another network, firewall). |
| A board connects at home but not outside | Easiest: use [Tailscale](remote-access.md) on the server and the board, and set the board up again with the server's Tailscale address. With the public listener, check `https://<your address>:8766/health` from your phone with Wi-Fi off: no answer means the router or firewall does not let port 8766 in, or your connection has no public address (see [Reaching the server from outside](remote-access.md)). Set the board up again after turning on the public listener. |
| Choppy audio from a Bluetooth speaker | Set `output_lead_in_ms = 300`; make sure nothing else holds the bluealsa PCM (only one `aplay` can use it). |
