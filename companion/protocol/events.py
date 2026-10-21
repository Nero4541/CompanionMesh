"""Event type names of the realtime protocol (1.4).

Client -> server
    session.start            payload: {session_id?, join?, roles?}
                             join: true without session_id attaches to the
                             most recently active session (e.g. an IP camera
                             bridge joining the browser's conversation).
                             roles: subset of ["mic", "speaker", "camera"];
                             absent = ["mic", "speaker"]. Audio output goes to "speaker"
                             devices, capture requests to "camera" devices.
    conversation.text        payload: {text}
    conversation.cancel      payload: {}
    audio.input.start        payload: {sample_rate, encoding, channels, full_duplex?, aec?}
                             encoding: pcm_s16le (16 kHz) or opus
                             full_duplex: true = the client keeps sending while
                             reply audio plays, so the user can cut in (barge-in)
                             aec: "device" if the device cancels its own speaker's
                             echo (browsers, speakerphones), else "none"
    audio.input.chunk        binary: PCM s16le mono, or for opus one or more
                             packets each prefixed with a big-endian uint16 length;
                             payload (optional) {pos, t}: index of the first sample
                             in the microphone stream (16 kHz samples) and its
                             capture time (device clock, ms since the Unix epoch)
    audio.input.stop         payload: {}
    audio.output.progress    payload: {turn_id, seq, state: "started" | "finished", pos?}
                             a clip started/finished playing; pos = microphone
                             sample index at that moment (lines up echo references)
    audio.output.stopped     payload: {turn_id, reason?}  (answer to audio.output.stop)
    audio.output.played      payload: {turn_id}  (all of a turn's audio has played)
    system.pong              payload: {ping_id, t_server, t_device}  (answer to
                             system.ping; t_device = device clock, ms since epoch)
    vision.enable            payload: {}  (opt in; vision is off by default)
    vision.disable           payload: {}  (stops all visual processing immediately)
    vision.frame             binary: JPEG/PNG/WebP; payload {mime, reason, frame_id?}
                             reason: periodic | change | manual (manual skips
                             rate limit and dedup)
    vision.event             payload: {description | label, tags?, confidence?, salience?}
    attention.quiet          payload: {enabled}  (do-not-disturb: no proactive speech)
    device.status            payload: free-form health, e.g. {cpu_temp_c, mem_free_mb,
                             wifi_signal_dbm, uptime_s, audio_buffer_ms}
    device.config            payload: {config, schema, editable, file?}  (the device's
                             settings, sent after session.started by devices that
                             can be configured remotely; schema: {path: {type,
                             editable, choices?}})
    device.config.result     payload: {request_id, ok, restarting, error?}  (answer
                             to device.config.set)
    device.log               payload: {boot, records: [{seq, t, level, logger, msg}]}
                             (1.2) the device's own log lines; boot is random per
                             process start, seq counts up within it; lines logged
                             while offline follow on the next connection
    device.diagnostics       payload: {request_id, sections: {name: text}}  (1.2)
                             answer to device.diagnostics.request
    device.update.result     payload: {request_id?, ok, version, restarting?, error?,
                             previous?, connected?}  (1.2) answer to device.update;
                             sent again (connected: true, no request_id) by the new
                             version once it is connected
    device.restart.result    payload: {request_id, ok}  (1.2)
    device.wifi.result       payload: {request_id, ok, networks: [ssid], current, error?}
                             (1.2) answer to device.wifi.set
    device.bluetooth.result  payload: {request_id, ok, error?, controller, tools,
                             speaker: {address, name, paired, connected} | null,
                             devices: [{address, name, paired, connected}],
                             found?: [{address, name, paired}], pcm, output_device,
                             restarting}  (1.3) answer to device.bluetooth; restarting:
                             the device plays to another output now and reconnects
    device.tailscale.result  payload: {request_id, ok, error?, installed, arch, machine,
                             running, state, auth_url, ips, hostname, dns_name, tailnet,
                             version, tun, joining, server_added, through_tailscale,
                             servers}  (1.3) answer to device.tailscale; state is
                             Tailscale's (NeedsLogin, Starting, Running, ...)

Server -> client
    session.started          payload: {session_id, history, protocol, devices, ...}
    session.devices          payload: {devices: [{device_id, roles}]} (on join/leave)
    session.ended            payload: {reason}  (sent to remaining camera devices when
                             the last mic/speaker device leaves; the server then
                             closes their connections)
    system.state             payload: {state, previous?, reason?, turn_id?}
                             state: idle | listening | transcribing | thinking |
                             speaking | interrupting | recovering
    system.ping              payload: {ping_id, t_server}  (clock sync and round trip)
    system.error             payload: {code, message, recoverable}
    audio.vad                payload: {state: "speech_start" | "speech_end"}
    conversation.transcript  payload: {text, final, speaker?, route?}  speaker (1.4, with
                             perception.speakers): {person_id, name, role, confidence,
                             on_screen, how}; role: user | acquaintance | stranger |
                             media | unknown. route (1.4, with routing.enabled): turn
                             (answered) | context (overheard, kept for the next turn) |
                             log (not for the companion)
    conversation.response.start  payload: {turn_id, proactive?, reason?}
    conversation.response.delta  payload: {turn_id, text}
    conversation.response.done   payload: {turn_id, text, cancelled, reason?, heard_text?}
                             heard_text: what had played before the user cut in
    agent.tool.progress      payload: {turn_id, tool, label}
    audio.output.chunk       binary: encoded audio; payload {turn_id, seq, mime, text}
    audio.output.done        payload: {turn_id}
    audio.output.stop        payload: {turn_id, reason}  (stop playing this turn now and
                             drop its remaining audio; answer audio.output.stopped)
    vision.state             payload: {enabled, available}
    vision.frame.status      payload: {frame_id, status, detail?}
                             status: accepted | duplicate | rate_limited |
                             rejected | disabled
    vision.observation       payload: {id, timestamp, device_id, description,
                             confidence, tags, source, source_event_id}
    vision.frame.used        payload: {frame_id, turn_id}  (agent mode: the frame
                             was attached to this turn's message)
    vision.capture.request   payload: {request_id, reason}  (to camera devices:
                             send a fresh "manual" frame now)
    attention.state          payload: {proactive, quiet, speech_blockers}
    attention.decision       payload: {kind, description, salience, source,
                             decision, reasons}  (only with attention.debug)
    attention.proactive      payload: {kind, action, reason, parsed, by, session_id}
                             (1.4, event bus / dashboard only) what the agent (by: agent)
                             or the triage model (by: triage) decided about an event,
                             with attention.decisions: reply | silent | remember
    routing.decision         payload: {route, reasons, speaker}  (1.4, only with
                             routing.debug) why a sentence was answered or not
    presence.update          payload: {people: [{person_id, name, role, since,
                             last_seen_s, camera, track_id?}], tracks: [{track_id,
                             camera, person_id, name, role, kind, since, last_seen,
                             frames, box, yaw}]}  (1.4, with perception.enabled) who
                             is here changed; role: user | acquaintance | stranger
    metrics.turn             payload: {turn_id, <stage>: seconds, ...}  (only with
                             metrics.emit_turn)
    device.config.set        payload: {request_id, config}  (new settings for this
                             device, from the admin dashboard; answer
                             device.config.result)
    device.diagnostics.request  payload: {request_id}  (1.2) collect a fixed set of
                             facts about the device (versions, check, memory, disk,
                             Wi-Fi, kernel log) and answer device.diagnostics
    device.update            payload: {request_id, version, sha256}  (1.2) download
                             GET /v1/edge/package/<version>.tar.gz from this server
                             (device token), check it, install it, restart with it
    device.restart           payload: {request_id, what: "client" | "board"}  (1.2)
    camera.live              payload: {seconds, fps}  (1.2) send pictures (reason
                             "live") about fps times a second for seconds, for someone
                             watching on the dashboard; again to extend, seconds 0 stops.
                             Live pictures only update the dashboard's view.
    device.wifi.set          payload: {request_id, add: [{ssid, psk?}], remove: [ssid]}
                             (1.2) psk is the 64-hex-digit WPA key, never the password;
                             both lists empty: just report the networks
    device.bluetooth         payload: {request_id, action, address?, seconds?}  (1.3)
                             action: state | scan (seconds, then also report what is
                             in reach) | pair (pair, trust and connect the speaker at
                             address; play to it) | remove (forget address; if it was
                             the speaker, play to the output from before)
    device.tailscale         payload: {request_id, action, package?, sha256?, auth_key?,
                             server_url?}  (1.3) action: state | install (download
                             GET /v1/edge/tailscale/<package> from this server, check
                             it, install it, then join) | up (join; without auth_key
                             the answer carries the login link) | logout. server_url:
                             the server's Tailscale address, which the device adds to
                             its addresses once it is on the tailnet
"""

PROTOCOL_VERSION = "1.4"

# client -> server
SESSION_START = "session.start"
CONVERSATION_TEXT = "conversation.text"
CONVERSATION_CANCEL = "conversation.cancel"
AUDIO_INPUT_START = "audio.input.start"
AUDIO_INPUT_CHUNK = "audio.input.chunk"
AUDIO_INPUT_STOP = "audio.input.stop"
AUDIO_OUTPUT_PLAYED = "audio.output.played"
AUDIO_OUTPUT_PROGRESS = "audio.output.progress"
AUDIO_OUTPUT_STOPPED = "audio.output.stopped"
SYSTEM_PONG = "system.pong"
VISION_ENABLE = "vision.enable"
VISION_DISABLE = "vision.disable"
VISION_FRAME = "vision.frame"
VISION_EVENT = "vision.event"
ATTENTION_QUIET = "attention.quiet"
DEVICE_STATUS = "device.status"
DEVICE_CONFIG = "device.config"
DEVICE_CONFIG_RESULT = "device.config.result"
DEVICE_LOG = "device.log"
DEVICE_DIAGNOSTICS = "device.diagnostics"
DEVICE_UPDATE_RESULT = "device.update.result"
DEVICE_RESTART_RESULT = "device.restart.result"
DEVICE_WIFI_RESULT = "device.wifi.result"
DEVICE_BLUETOOTH_RESULT = "device.bluetooth.result"
DEVICE_TAILSCALE_RESULT = "device.tailscale.result"

# server -> client
SESSION_STARTED = "session.started"
SESSION_DEVICES = "session.devices"
SESSION_ENDED = "session.ended"
SYSTEM_STATE = "system.state"
SYSTEM_ERROR = "system.error"
SYSTEM_PING = "system.ping"
AUDIO_VAD = "audio.vad"
CONVERSATION_TRANSCRIPT = "conversation.transcript"
RESPONSE_START = "conversation.response.start"
RESPONSE_DELTA = "conversation.response.delta"
RESPONSE_DONE = "conversation.response.done"
AGENT_TOOL_PROGRESS = "agent.tool.progress"
AUDIO_OUTPUT_CHUNK = "audio.output.chunk"
AUDIO_OUTPUT_DONE = "audio.output.done"
AUDIO_OUTPUT_STOP = "audio.output.stop"
VISION_STATE = "vision.state"
VISION_FRAME_STATUS = "vision.frame.status"
VISION_OBSERVATION = "vision.observation"
VISION_FRAME_USED = "vision.frame.used"
VISION_CAPTURE_REQUEST = "vision.capture.request"
ATTENTION_STATE = "attention.state"
ATTENTION_DECISION = "attention.decision"
PRESENCE_UPDATE = "presence.update"
ROUTING_DECISION = "routing.decision"
ATTENTION_PROACTIVE = "attention.proactive"
METRICS_TURN = "metrics.turn"
DEVICE_CONFIG_SET = "device.config.set"
DEVICE_DIAGNOSTICS_REQUEST = "device.diagnostics.request"
DEVICE_UPDATE = "device.update"
DEVICE_RESTART = "device.restart"
DEVICE_WIFI_SET = "device.wifi.set"
DEVICE_BLUETOOTH = "device.bluetooth"
DEVICE_TAILSCALE = "device.tailscale"
CAMERA_LIVE = "camera.live"
