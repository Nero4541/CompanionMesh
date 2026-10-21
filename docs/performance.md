# Performance and hardware

The time from the end of your sentence to the first spoken word is roughly:

```text
end-of-utterance pause  +  STT  +  agent first token  +  TTS of the first sentence
```

Measured with v0.1.5:

| Stage | Measured | Setup |
|---|---|---|
| End-of-utterance pause | 1.0 s | `vad.min_silence_ms` default |
| STT | 0.2–0.35 s for 1–3 s of speech | Whisper `large-v3-turbo` fp16, RTX 4070 Ti |
| TTS, per sentence | 0.4–0.5 s | Irodori `v4.1-Small-MF` (4 steps), RTX 4070 Ti |
| TTS, per sentence | ~2 s | Irodori `v4.1-Small` (40 steps), Tesla V100 shared with an LLM |
| Agent, first token | 10 s (prompt cached) to 50–95 s | OpenClaw agent with a ~23k-token prompt; Qwen 27B Q8 on llama.cpp across 2× V100 that also serve other GPU jobs; reasoning enabled |

**With a local LLM, the agent is almost always the bottleneck.** Speech recognition and synthesis take seconds at most; the agent can take a minute. Two causes dominate:

- **Prompt size.** Agent frameworks send large system prompts: tools, skills, memory, character sheets. In the run above, a ~23k-token prompt took 48 s to process (about 490 tokens/s) whenever the server could not reuse its prompt cache.
- **Reasoning ("thinking") tokens.** One 31-character reply was preceded by ~1,800 generated tokens, which took 65 s at 27 tokens/s.

The companion streams each sentence to TTS as soon as it is complete. The dev client shows how long the agent has been thinking, so a slow turn is visibly slow rather than apparently stuck.

## Making a local setup responsive

For the agent:

- **Use a lean agent for voice.** A dedicated agent with few tools and a short system prompt (a few thousand tokens rather than 20k+) cuts prompt processing proportionally. Copy the character or persona into it rather than reusing a heavyweight general-purpose agent.
- **Turn reasoning off for voice turns.** Disable it in the agent's model settings. For `direct` with llama.cpp or vLLM serving a Qwen3-style model, send it with the request:

  ```yaml
  llm:
    extra_body:
      chat_template_kwargs: {enable_thinking: false}
  ```

- **Keep the prompt cache warm.** llama.cpp only skips work for a prompt prefix it has already processed. If first-token time swings between turns (for example 10 s, then 60 s), check the LLM server's log for full prompt re-evaluation. Running the voice agent's model without other concurrent users (a dedicated instance or slot) helps.
- **Cap reply length** where the backend honours it: `agent.<backend>.extra_body: {max_tokens: 200}`.
- **Prefer a smaller or faster model for conversation**, or a cloud model, and keep heavyweight models for agent tasks that are not real-time.

For TTS:

- Use a distilled model such as Irodori `v4.1-Small-MF` (4 steps) when you do not need a voice trained on a specific base model.
- Otherwise trade quality for speed with fewer sampling steps: `tts.extra_body: {irodori: {num_steps: 16}}`.

Use a shorter `vad.min_silence_ms` (e.g. 700) for snappier turns if you speak in continuous sentences.

## GPU memory

| Component | VRAM (measured) |
|---|---|
| Whisper `large-v3-turbo`, fp16 | ~2.3 GB |
| Irodori-TTS `v4.1-Small`, fp32 | ~4 GB |
| Perception engine (faces and voices, [people](people.md)) | ~0.6 GB |
| Built-in VLM, Mage-VL 4B in FP8 (RTX 40 series and newer) | ~6.4 GB with its fused kernels (BF16 on older cards: ~9 GB) |
| Other VLM | depends on the model; small models (a few billion parameters) are enough |
| LLM | depends on model and context size; usually by far the largest |

The companion server itself needs a GPU only for STT (and the built-in voice). STT can stay on the machine running the companion while the agent, LLM and TTS run on a server. Turning a [built-in engine](speech.md#built-in-speech) off frees its GPU memory at once, for example to run something else on the card for a while.

## Suggested setups

| Setup | Where things run | Notes |
|---|---|---|
| One PC, ~12 GB GPU | Companion, STT and TTS locally; agent backed by a cloud model or another machine | STT and TTS together take most of a 12 GB card; a local LLM of useful size does not fit beside them. |
| PC + GPU server | Companion and STT on the PC; agent, LLM and TTS on the server, reached over Tailscale or an SSH tunnel | Give the conversational model a GPU that is not busy with other jobs during conversations; use a lean agent with reasoning off. |
| No NVIDIA GPU | STT `small` on the CPU; agent with a cloud model; TTS on another machine, or text only | Expect a few seconds of STT per utterance. |
