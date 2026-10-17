# Voices

Voices for the built-in voice (Irodori-TTS). The file name without its
extension is the voice's id; set `tts.voice` to it.

| File | What it is |
|---|---|
| `alice.wav` (or `.flac`, `.mp3`, ...) | A reference clip of the voice to imitate |
| `alice.speaker.safetensors` | A speaker embedding (Irodori's speaker inversion) |
| `alice.pt` | A latent made from a reference clip |
| `voices.json` | Aliases: several clips, or a clip with fixed options, under one id |

Files can be added while the engine runs; it looks here each time a voice is
asked for. `tts.voice: none` speaks without a reference. See
[Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server) for the
formats. Nothing in this folder is part of the repository, and updates leave it
alone.
