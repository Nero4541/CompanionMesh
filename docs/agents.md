# Choosing an agent backend

Set `agent.provider` in the config (see [Configuration](configuration.md)).

## Hermes Agent (`hermes`, default)

Hermes owns memory: each companion session maps to a Hermes session, so Hermes keeps the transcript, tool calls and long-term memory, and they survive restarts of either side.

1. Install and configure Hermes, including a model provider (`hermes model`). On Windows, Hermes runs inside WSL2; WSL forwards `localhost`, so the companion can reach it at `127.0.0.1`.
2. Enable its API server in `~/.hermes/.env`:

   ```bash
   API_SERVER_ENABLED=true
   API_SERVER_KEY=<a long random string>
   ```

3. Put the same key in the companion's `.env` as `HERMES_API_KEY`. Without a key, Hermes still answers, but it ignores session continuity and memory scoping.
4. Start Hermes with `hermes gateway`. Its API listens on `http://127.0.0.1:8642`.

## OpenClaw (`openclaw`)

OpenClaw likewise keeps each session's transcript and its agent memory on the gateway host.

1. Enable the Chat Completions endpoint in `openclaw.json` (it is off by default):

   ```json5
   {
     gateway: {
       http: { endpoints: { chatCompletions: { enabled: true } } },
     },
   }
   ```

2. Put the gateway token (`gateway.auth.token`) in the companion's `.env` as `OPENCLAW_GATEWAY_TOKEN`.
3. Choose the agent with `agent.openclaw.agent_id` (default `main`), then start the gateway. It listens on `http://127.0.0.1:18789`.

## No agent framework (`direct`)

The companion talks to an OpenAI-compatible LLM itself, such as LM Studio, Ollama, llama.cpp, vLLM or a cloud API. Configure `llm.base_url`, `llm.model` and, if needed, `LLM_API_KEY`. This backend has no tools. Its memory is the stored conversation history plus a small full-text recall over earlier conversations.

## Running the agent on another machine

Point `base_url` at the other host, e.g. `http://100.x.y.z:8642/v1`. On the agent host:

- **Hermes:** set `API_SERVER_HOST=0.0.0.0` (or the Tailscale IP). `API_SERVER_KEY` is mandatory when binding beyond loopback.
- **OpenClaw:** set `gateway.bind` to `lan` or `tailnet`, and keep token auth enabled.

Alternatively, leave the agent (and a TTS server) listening on loopback and forward the ports over SSH. The traffic is then encrypted, and nothing changes on the server:

```powershell
ssh -N -L 127.0.0.1:28789:127.0.0.1:18789 -L 127.0.0.1:28088:127.0.0.1:8088 user@agent-host
```

Then set `base_url: http://127.0.0.1:28789/v1` for the agent and `http://127.0.0.1:28088/v1` for TTS.

> **Security:** these endpoints give full control of the agent, including its terminal and file tools, and the traffic is plain HTTP. Use them only over a private network such as [Tailscale](https://tailscale.com), or through an SSH tunnel. Never expose them to the internet.
