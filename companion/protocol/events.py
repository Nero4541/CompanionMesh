"""Event type names used by protocol v0.1.

Client -> server
    session.start            payload: {session_id?, persona?}
    conversation.text        payload: {text}
    conversation.cancel      payload: {}
    audio.input.start        payload: {sample_rate, encoding, channels}
    audio.input.chunk        binary: PCM s16le mono at the announced rate
    audio.input.stop         payload: {}
    audio.output.played      payload: {turn_id}

Server -> client
    session.started          payload: {session_id, history, protocol}
    system.state             payload: {state}
    system.error             payload: {code, message, recoverable}
    audio.vad                payload: {state: "speech_start" | "speech_end"}
    conversation.transcript  payload: {text, final}
    conversation.response.start  payload: {turn_id}
    conversation.response.delta  payload: {turn_id, text}
    conversation.response.done   payload: {turn_id, text, cancelled}
    agent.tool.progress      payload: {turn_id, tool, label}
    audio.output.chunk       binary: encoded audio; payload {turn_id, seq, mime, text}
    audio.output.done        payload: {turn_id}
"""

PROTOCOL_VERSION = "0.1"

# client -> server
SESSION_START = "session.start"
CONVERSATION_TEXT = "conversation.text"
CONVERSATION_CANCEL = "conversation.cancel"
AUDIO_INPUT_START = "audio.input.start"
AUDIO_INPUT_CHUNK = "audio.input.chunk"
AUDIO_INPUT_STOP = "audio.input.stop"
AUDIO_OUTPUT_PLAYED = "audio.output.played"

# server -> client
SESSION_STARTED = "session.started"
SYSTEM_STATE = "system.state"
SYSTEM_ERROR = "system.error"
AUDIO_VAD = "audio.vad"
CONVERSATION_TRANSCRIPT = "conversation.transcript"
RESPONSE_START = "conversation.response.start"
RESPONSE_DELTA = "conversation.response.delta"
RESPONSE_DONE = "conversation.response.done"
AGENT_TOOL_PROGRESS = "agent.tool.progress"
AUDIO_OUTPUT_CHUNK = "audio.output.chunk"
AUDIO_OUTPUT_DONE = "audio.output.done"
