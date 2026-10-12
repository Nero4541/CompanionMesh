# Device template

[`device.py`](device.py) is a complete, minimal Companion device in one file: it joins a conversation, streams microphone audio, plays the companion's speech, sends camera frames, answers clock pings and reconnects. Use it to build a client for hardware the [edge client](../edge) does not cover, or as a reference when writing one in another language (an ESP32, a phone app, a robot's own controller).

```bash
pip install "websockets>=14"
export COMPANION_TOKEN=…                                   # if the server has a token
python device.py --text "こんにちは"                         # a typed turn; speech is saved in replies/
python device.py --wav question.wav                        # a spoken turn (16 kHz mono 16-bit WAV)
python device.py --roles camera --jpeg room.jpg --keep-running   # a camera joining a conversation
```

## Porting

Only four methods touch hardware. Replace them and the rest stays the same:

| Method | Does | Notes |
|---|---|---|
| `microphone()` | yields 16 kHz mono 16-bit PCM, 100 ms at a time, in real time | Or Opus: send `encoding: "opus"` and length-prefixed packets (see the protocol table in the main README). |
| `play()` | plays one sentence and returns when it has finished | WAV by default (`tts.response_format` on the server). Returning only after playback matters: the server uses it to know when it may listen again. |
| `stop_playback()` | silences the speaker at once | Called when the user cuts in (`audio.output.stop`). |
| `capture()` | returns one JPEG | Send frames when the view changes or when asked (`vision.capture.request`), not as video. |

## What the server expects

1. **Connect** to `ws://<server>:8765/v1/realtime` with `Authorization: Bearer <token>`. A WebSocket library that cannot send headers can use `?token=<token>` instead.
2. **Start**: the first message is `session.start` with your `roles` (`mic`, `speaker`, `camera`). Keep the `session_id` from `session.started` and send it in the next `session.start` after a reconnect to resume the same conversation. A camera alone sends `join: true` to attach to the conversation someone else started.
3. **Microphone**: `audio.input.start`, then binary `audio.input.chunk` frames, then `audio.input.stop`. The server finds the start and end of speech itself. Stay half duplex (`full_duplex: false`) unless the device cancels its own speaker's echo.
4. **Speaker**: play binary `audio.output.chunk` frames in `seq` order. When `audio.output.done` arrives and everything of that turn has played, send `audio.output.played`; until then the server considers the companion still speaking. On `audio.output.stop`, stop, drop the rest of that turn and answer `audio.output.stopped`.
5. **Pings**: answer every `system.ping` with `system.pong`, adding `t_device` (your clock, ms since the epoch).
6. **Ignore** event types you do not know; the protocol grows by adding events.

Binary frames are `[uint32 big-endian length of the JSON header][JSON header][raw bytes]`, both ways. The JSON header is an ordinary envelope: `{"type": …, "payload": {…}}`.

The full event list is in the main README under "Realtime protocol (1.0)".
