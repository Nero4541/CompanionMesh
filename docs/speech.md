# Speech

## Built-in speech

Without a speech-to-text or text-to-speech server of your own, the server installs and runs both itself: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for recognition and [Irodori-TTS](https://github.com/Aratako/Irodori-TTS-Server), a Japanese voice. Choose **Built-in** in `uv run companion setup`, or open **Speech** in the dashboard, press **Install** and turn the engine on.

- **Installing.** Each engine gets an environment of its own under `data/builtin/stt` and `data/builtin/tts`, made by uv from the locked projects in `builtin/` (the voice runs on Python 3.10, which uv fetches by itself), so the server's own installation stays small. The first install downloads several GB: PyTorch for the voice and the NVIDIA libraries, or CPU builds without an NVIDIA GPU (`builtin.libraries`). The models download from Hugging Face on the first start: about 1.5 GB for Whisper `large-v3-turbo`, 3.4 GB for Irodori `v4.1-Small-MF`.
- **On and off.** The switch on the Speech page, or `builtin.stt.enabled` / `builtin.tts.enabled`, takes effect at once, without a restart. Off, the engine's process ends and all of its GPU memory is free at once (the page shows the GPU's total); on, it starts again, in seconds once its model is downloaded. An engine also ends with the server, however the server ends.
- **What it replaces.** While on, an engine is used instead of `stt.provider` / `tts.provider`; off, those apply again. Setup sets them to `none` when you choose built-in, so off means no speech recognition or voice.
- **Settings.** `builtin.stt` (model, device) and `builtin.tts` (checkpoint, device, `precision: bf16` to halve the memory on CUDA); changing them restarts only that engine. `stt.language`, `stt.prompt`, `tts.voice` and `tts.extra_body` apply as with any provider. Reference clips for the voice go in `data/builtin/tts/voices/`.
- **Updates.** A newer server may bring newer engines; an installed engine is brought up to date when it next starts.

Each engine serves the OpenAI audio API on `127.0.0.1` only, on a port of its own; its log is on the Speech page and in `data/builtin/<name>/worker.log`.

## Speech-to-text

The default is [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with `large-v3-turbo`. The model is downloaded on first start (about 1.6 GB).

```yaml
stt:
  model: large-v3-turbo   # small / medium / large-v3 / …
  language: ja            # null = auto-detect
  device: auto            # tries CUDA first, falls back to CPU
```

The `cuda` extra installs cuBLAS and cuDNN from PyPI, so no separate CUDA toolkit is needed. Voice activity detection uses Silero VAD, whose model comes with the server, so it also works with speech recognition on another machine. An utterance ends after `vad.min_silence_ms` (default 1000 ms) of silence; lower it for snappier turns, raise it if you are cut off mid-sentence.

To run speech recognition on another machine, point the companion at any server with the OpenAI `POST /v1/audio/transcriptions` API ([speaches](https://github.com/speaches-ai/speaches), the whisper.cpp server, OpenAI, Groq):

```yaml
stt:
  provider: openai_compatible
  base_url: http://gpu-box:8000/v1
  model: Systran/faster-whisper-large-v3-turbo   # the server's model name
  language: yue                                   # Cantonese
  api_key_env: STT_API_KEY                        # optional
```

Voice activity detection still runs in the companion; only finished utterances are sent, as 16 kHz WAV.

**Steering the writing.** Whisper often writes Cantonese in simplified characters, or as written Mandarin. `stt.prompt` shows it the style you want with a short example in that style, for instance:

```yaml
stt:
  language: yue
  prompt: 以下係廣東話對話，用香港繁體中文書寫。
```

It goes with every utterance (Whisper's initial prompt; `prompt` in the OpenAI API, which speaches, OpenAI and the built-in engine take) and applies at once. There is none by default; add one only if you need it. Keep it to one short sentence: a long or repetitive prompt can make Whisper repeat itself. It changes how words are written, not which words are heard; for better Cantonese recognition, use a model fine-tuned for it.

## Text-to-speech

Any server implementing OpenAI's `POST /v1/audio/speech` works. Each sentence is synthesized as it completes. Vendor-specific options go in `tts.extra_body` and are merged into every request.

The default configuration targets [Irodori-TTS-Server](https://github.com/Aratako/Irodori-TTS-Server), a Japanese TTS server (MIT; check the model cards for the weights' licenses). The [built-in voice](#built-in-speech) is the same server, installed and run for you. To run it yourself, for example on another machine:

```powershell
git clone https://github.com/Aratako/Irodori-TTS-Server.git
cd Irodori-TTS-Server
uv sync --extra cu128
Copy-Item .env.example .env   # e.g. IRODORI_HF_CHECKPOINT=Aratako/Irodori-TTS-v4.1-Small-MF
uv run --no-sync python -m irodori_openai_tts --host 127.0.0.1 --port 8088
```

`tts.voice: none` synthesizes without reference audio. For a stable character voice, add a reference clip to the server's `voices/` directory and set `tts.voice` to its id. Set `tts.provider: none` for text-only replies.
