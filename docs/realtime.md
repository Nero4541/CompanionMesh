# Realtime conversation

Conversation is full duplex with clients that support it (the dev client and the edge client do): the microphone keeps streaming while the companion speaks.

**Barge-in.** When you start talking over a reply, the server cancels the agent's reply and any speech still being synthesized (`TTS cancellation`), tells every speaker device to stop at once (`audio.output.stop`), and treats what you say as your next turn. The interrupted reply is remembered only up to what you actually heard, and the next turn sees it ending in "…". Speaking while the companion is still *thinking* restarts the turn with both of your messages. Older clients stay half duplex: their audio is ignored while the companion thinks or speaks. Set `realtime.barge_in: false` to make every client half duplex.

**Not hearing itself.** A speaker next to a microphone means the companion's voice reaches its own ears. Three layers keep that from turning into a conversation with itself:

1. **Devices with echo cancellation** say so (`aec: "device"` in `audio.input.start`). Browsers do (`echoCancellation`), and so do most USB speakerphones. Nothing else is needed.
2. **Gating.** While reply audio plays on a device without echo cancellation, speech has to be longer (`barge_in_min_speech_ms`, 600 ms) and clearer (`barge_in_threshold`) to count as you cutting in.
3. **Echo guard.** A transcript that mostly repeats what the companion said in the last `guard_window_s` seconds is its own voice coming back. It is dropped (counted as `echo_dropped` in `/v1/metrics`).

`realtime.echo.canceller: nlms` adds a reference acoustic echo canceller on the server. Devices report when each reply clip starts playing as a position in their microphone stream. The server lines up the audio it sent with the microphone signal, finds the remaining speaker and room delay (up to `max_delay_ms`), and subtracts the echo with a frequency-domain NLMS filter. It is a reference implementation for boards without hardware AEC: it helps with a fixed speaker and microphone, while a speakerphone with built-in AEC is better.

**States and recovery.** Every session moves through `idle`, `listening`, `transcribing`, `thinking`, `speaking`, `interrupting` and `recovering`, and only along defined transitions. Each busy state has a deadline. If a transcription hangs, a device never confirms playback, or the only speaker disconnects mid-reply, the session moves to `recovering` and back to listening instead of staying stuck.

**Latency.** `GET /v1/metrics` shows where the time goes, per stage, with p50/p95 over recent turns. It records numbers only: no audio, images or text.

```json
{"stages": {"mic_to_vad": {...}, "vad_to_stt": {"p50": 0.41, "p95": 0.8, ...},
            "stt_to_first_token": {"p50": 9.8, ...}, "first_token_to_audio": {...},
            "speech_to_audio": {...}, "network_rtt": {...}, "barge_in_to_stop": {...},
            "frame_to_observation": {...}},
 "counters": {"barge_ins": 3, "echo_dropped": 1, "recoveries": 0},
 "bottleneck": "stt_to_first_token"}
```

The server pings each device every `realtime.ping_interval_s` (10 s) to measure the round trip and the offset between the device's clock and its own. Device timestamps (when audio was captured, when a frame was taken) can then be compared with server time. `/v1/status` shows each device's `rtt_ms` and `clock_offset_ms`. Set `metrics.emit_turn: true` to also send each turn's timings to clients as `metrics.turn`.

```yaml
realtime:
  barge_in: true
  barge_in_min_speech_ms: 600
  echo:
    canceller: none      # or nlms
    guard: true
```
