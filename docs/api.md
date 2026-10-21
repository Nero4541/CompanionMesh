# API

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
| `GET /v1/admin/builtin` | The built-in engines (speech, perception): installed, state, process, GPU memory, log (admin token) |
| `POST /v1/admin/builtin/{stt\|tts\|perception}/install`, `PUT /v1/admin/builtin/{stt\|tts\|perception}` | Install an engine in the background; turn one on or off (`{"enabled": true}`, saved and applied at once) or switch its model (`{"model": "<folder>"}`) (admin token) |
| `GET /v1/admin/people` | Known people (with their numbers of face and voice samples), who is here now, faces tracked, the perception engine (admin token) |
| `POST /v1/admin/people` `{name, role, notes, consent}`, `PATCH`/`DELETE /v1/admin/people/{id}` | Add someone (only with `consent: true`), change, forget at once (admin token) |
| `POST /v1/admin/people/{id}/face` `{camera?, track_id?, seconds?}`, `DELETE .../face` | Face samples from a camera (the person looks at it) or from a face tracked now; delete them (admin token) |
| `POST /v1/admin/people/{id}/voice` (an audio file as the body), `DELETE .../voice` | Voice samples from a recording of the person speaking alone; delete them (admin token) |
| `POST /v1/admin/people/promote` `{track_id, name, role, consent}` | Add the stranger in view as a person (admin token) |
| `POST /v1/admin/people/{id}/voice/candidates` | Add the voice samples heard lately with voice, face and lips agreeing (admin token) |
| `POST /v1/admin/people/identify?camera=` (or a JPEG as the body) | Who the faces in a picture are (admin token) |
| `POST /v1/admin/restart` | Restart the server with its saved settings (admin token) |
| `GET /v1/admin/phone`, `POST /v1/admin/phone` `{renew?}` | Addresses that log a phone in to the dashboard (with the admin token in them); make the admin token if there is none, or a new one (saved in `.env`, used at once) (admin token) |
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
| `GET /v1/admin/devices/{id}/bluetooth`, `POST .../bluetooth/scan` `{seconds}`, `.../bluetooth/pair` `{address}`, `.../bluetooth/remove` `{address}` | A device's Bluetooth speaker: state, what is in reach, pair (it plays through it from then on), forget (admin token) |
| `GET /v1/admin/devices/{id}/tailscale`, `POST .../tailscale` `{auth_key?}`, `POST .../tailscale/logout` | A device's Tailscale: state (with the login link while it waits for one), install if needed and join, leave (admin token) |
| `GET /v1/edge/tailscale/<name>` | A Tailscale package this server downloaded and checked, for `device.tailscale` (device token) |
| `GET /v1/admin/conversation`, `GET /v1/admin/recordings`, `GET /v1/admin/recordings/{id}/audio` | What was said (with `server.dashboard_conversation`), recordings (with `stt.keep_audio`) (admin token) |
| `GET /v1/admin/viewer` | Whether the read-only viewer is on and its addresses on the tailnet (admin token) |
| `GET /v1/view/state`, `GET /v1/view/conversation?after=`, `GET /v1/view/devices/{id}/frame`, `POST /v1/view/devices/{id}/live` `{seconds}` | The read-only viewer behind `/view/` (with `server.viewer.enabled`): conversations and devices, the conversation's lines, a camera's newest picture, live pictures while watching. Only from the tailnet, at the server's Tailscale address (no token) |
| `GET /v1/admin/vision/images`, `GET /v1/admin/vision/images/{day}/{name}` | Images kept by `vision.store_images` (admin token) |
| `GET /v1/admin/updates`, `GET /v1/admin/updates/latest` | Version, update log and changelog; the newest release on GitHub (admin token) |
| `GET /v1/admin/status`, `/v1/admin/metrics` | `/v1/status` and `/v1/metrics` for the dashboard (admin token) |

## Realtime protocol (1.4)

1.1 added `device.config`, `device.config.set` and `device.config.result` (remote settings for devices); a 1.0 client never sees them. 1.2 added `device.log`, `device.diagnostics`, `device.update`, `device.restart` and `device.wifi.set` with their answers (looking after devices remotely); older devices ignore them, and the dashboard says they do not answer. 1.3 added `device.bluetooth` and `device.tailscale` with their answers (a Bluetooth speaker and Tailscale from the dashboard). 1.4 added `presence.update` (who is around, with perception on), `speaker` and `route` on `conversation.transcript`, and `routing.decision`; older clients ignore them.

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
| → | `device.bluetooth.result` | `{request_id, ok, error?, controller, tools, speaker, devices, found?, pcm, output_device, restarting}`: answer to `device.bluetooth`; `restarting`: it plays to another output now and reconnects (1.3) |
| → | `device.tailscale.result` | `{request_id, ok, error?, installed, arch, running, state, auth_url, ips, hostname, tailnet, version, server_added, through_tailscale, servers}`: answer to `device.tailscale` (1.3) |
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
| ← | `conversation.transcript` `speaker` | With `perception.speakers`: `{person_id, name, role, confidence, on_screen, how}` (1.4); role: user, acquaintance, stranger, media or unknown |
| ← | `conversation.transcript` `route` | With `routing.enabled`: `turn` (answered), `context` (overheard, given with the next turn) or `log` (not for the companion) (1.4) |
| ← | `routing.decision` | `{route, reasons, speaker}` (with `routing.debug`, 1.4) |
| ← | `presence.update` | `{people: [{person_id, name, role, since, last_seen_s, camera, track_id?}], tracks: [{track_id, camera, person_id, name, role, kind, since, last_seen, frames, box, yaw}]}` (1.4, with `perception.enabled`): who is here changed |
| ← | `metrics.turn` | `{turn_id, <stage>: seconds…}` (with `metrics.emit_turn`) |
| ← | `device.config.set` | `{request_id, config}`: new settings from the admin dashboard; check them, save them and answer `device.config.result` (1.1) |
| ← | `device.diagnostics.request` | `{request_id}`: collect versions, checks, memory, disk, network, Wi-Fi and kernel messages (1.2) |
| ← | `device.update` | `{request_id, version, sha256}`: download `/v1/edge/package/<version>.tar.gz` from this server with the device token, check it, install it and restart (1.2) |
| ← | `device.restart` | `{request_id, what: "client" or "board"}` (1.2) |
| ← | `camera.live` | `{seconds, fps}`: send pictures (reason `live`) about `fps` times a second for `seconds`, for the dashboard's live view only; again to extend, `seconds: 0` stops (1.2) |
| ← | `device.wifi.set` | `{request_id, add: [{ssid, psk?}], remove: [ssid]}`: `psk` is the WPA key (64 hex digits), never the password; both empty: just report (1.2) |
| ← | `device.bluetooth` | `{request_id, action, address?, seconds?}`: `state`, `scan` (for `seconds`), `pair` (pair, trust, connect and play through the speaker at `address`), `remove` (1.3) |
| ← | `device.tailscale` | `{request_id, action, package?, sha256?, auth_key?, server_url?}`: `state`, `install` (download `GET /v1/edge/tailscale/<package>`, check it, install it, then join), `up` (join), `logout`; `server_url` is the server's Tailscale address, which the device adds to its addresses once on the tailnet (1.3) |

A session can have several devices attached at once. Every device receives the conversation events; speech audio goes only to `speaker` devices, and only the device that sent `audio.input.start` feeds the microphone. The session ends when its last `mic` or `speaker` device disconnects; remaining camera devices then receive `session.ended` and are disconnected (a camera alone does not keep a conversation alive). Simple clients stay simple: a client that declares no roles gets `mic` and `speaker`, one that does not ask for `full_duplex` is treated as half duplex, and vision traffic only appears after a client sends `vision.enable`. Clients and server must ignore event types they do not know. The namespaces `conversation.*`, `audio.*`, `vision.*`, `memory.*`, `agent.*`, `system.*`, `session.*` and `device.*` are reserved.
