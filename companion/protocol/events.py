"""Event type names of the realtime protocol (v0.5).

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
    conversation.transcript  payload: {text, final}
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
    metrics.turn             payload: {turn_id, <stage>: seconds, ...}  (only with
                             metrics.emit_turn)
"""

PROTOCOL_VERSION = "0.5"

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
METRICS_TURN = "metrics.turn"
