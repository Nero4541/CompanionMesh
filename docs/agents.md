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

## Tools for the agent

With `mcp.enabled: true` (off by default) the server offers the agent tools of its own, through an MCP server at `/mcp` (the Model Context Protocol, streamable HTTP). Hermes and OpenClaw load it like any other MCP server.

### When you need them

Without the tools the agent still gets what the server knows, as short lines with each message: who said it (`「…」 (Yukari, the user)`), what was overheard, and in proactive turns who is around and what has been going on. For conversation, that is enough, and you need to set up nothing.

Set the tools up when the agent should find things out or act **by itself**, whenever it wants:

- check who is around, or what someone has been doing, before it answers;
- look through a camera when the user asks "what do you see?";
- remember someone new, or forget someone, when the user asks;
- keep an eye on long activities ("tell me when I have been gaming for two hours").

They are for an external agent, Hermes or OpenClaw (`agent.provider: hermes` or `openclaw`), which has to be told where they are; the `direct` backend does not use them. Most of them need [people](people.md) on:

| Tool | Gives or does | Needs |
|---|---|---|
| `who_is_here()` | who is around now: name, role (user, acquaintance, stranger), since when, which camera, what they are doing, who is speaking | `perception.enabled` |
| `timeline(minutes=120, person=null)` | what each person has been doing ([timeline](people.md#timeline)) | `timeline.enabled` |
| `recent_speech(minutes=10)` | what was said lately, by whom, and whether it was said to the companion or overheard | – |
| `look(camera=null)` | a camera's newest picture (as an image) and, with a vision model, a short description | `perception.enabled` and a camera |
| `remember_person(track_id, name, role)` | add the stranger in view as a person; only after the user said yes (see below) | `perception.enabled` |
| `forget_person(name)` | forget a person; their samples are deleted at once | – |
| `list_people()` | the people known (names, roles, notes); never face or voice data | – |
| `set_watch(activity, min_minutes, …)`, `list_watches()`, `remove_watch(id)` | watches on long activities, which become `long_activity` events | `timeline.enabled` |

Every tool also answers at `POST /v1/tools/<name>` with its arguments as a JSON object (and `GET /v1/tools` lists them), for anything that cannot load MCP servers.

### Setting them up

On the companion server, once:

1. **Turn the tools on**: under **Configuration**, `mcp.enabled` (applies at once), or `mcp: {enabled: true}` in `configs/companion.local.yaml`.
2. **Make a token** for the agent to send: `uv run companion agent-tools --token` writes `COMPANION_AGENT_TOKEN` to `.env`. Then restart the server: it reads `.env` only when it starts (the dashboard's restart does not read it again). Without a token the tools answer only this machine; with one, every caller has to send it, this machine too.
3. **Find the address** the agent will use: `http://127.0.0.1:8765/mcp` when the agent runs on this machine; from another machine, this machine's Tailscale address, `http://100.x.y.z:8765/mcp` (with `server.host: 0.0.0.0`). A Hermes inside WSL2 reaches Windows at `127.0.0.1` only with WSL's mirrored networking; otherwise use the Tailscale address.
4. `uv run companion agent-tools --url <that address>` prints the settings for both agents with the address filled in. The token itself is in `.env`.

#### Hermes

On the machine where Hermes runs:

1. Add `COMPANION_AGENT_TOKEN=<the token>` to `~/.hermes/.env`.
2. Add the server to `~/.hermes/config.yaml` (Hermes fills in `${…}` from its environment). When Hermes runs on this machine, `uv run companion agent-tools --write-hermes ~/.hermes/config.yaml` does it, keeping a copy as `.bak-companion`.

   ```yaml
   mcp_servers:
     companion:
       url: http://127.0.0.1:8765/mcp
       headers:
         Authorization: Bearer ${COMPANION_AGENT_TOKEN}
       timeout: 60
       connect_timeout: 10
   ```

3. Restart Hermes: `hermes gateway restart` (or, when `hermes gateway` runs in a terminal, stop it and start it again).
4. Check: `hermes mcp test companion` lists the tools; then ask the agent who is around.

A Hermes profile keeps both files in `~/.hermes/profiles/<name>/` instead; only that profile gets the tools.

#### OpenClaw

On the machine where OpenClaw runs:

1. Add `COMPANION_AGENT_TOKEN=<the token>` to `~/.openclaw/.env`.
2. Restart the gateway, `openclaw gateway restart`, **before** the next step: OpenClaw fills in `${COMPANION_AGENT_TOKEN}` when it loads its config, and refuses a config whose variable it does not have.
3. Add the server, with the address from above (or, when OpenClaw runs on this machine, `uv run companion agent-tools --write-openclaw ~/.openclaw/openclaw.json`, which keeps a copy as `.bak-companion`). The gateway picks it up at once.

   ```sh
   openclaw mcp set companion '{"url":"http://127.0.0.1:8765/mcp","transport":"streamable-http","connectionTimeoutMs":10000,"requestTimeoutMs":60000,"headers":{"Authorization":"Bearer ${COMPANION_AGENT_TOKEN}"}}'
   ```

   Use `set`: `openclaw mcp add` tries the server before it fills in the token, and is refused (`add --no-probe` works too).
4. Check: `openclaw mcp probe companion` answers `companion: 10 tools`; then ask the agent who is around.
5. Every agent with the `coding` or `messaging` tool profile gets the tools, as `companion__who_is_here` and so on. To keep them from an agent, add `"tools": {"deny": ["companion__*"]}` to its entry in `agents.list` of `openclaw.json`.

### Remembering people

`remember_person` adds a face the cameras see now. With `mcp.consent: conversation` (default) it works only when the user's last sentence, at most `mcp.consent_window_s` (5 minutes) ago, said yes ("yes", "OK", "はい", "いいよ", "好的", …): the agent asks first, then calls it. With `mcp.consent: dashboard` the request waits on the People page until you confirm it.

[agent-guide.md](agent-guide.md) is a text to give the agent (in its SOUL.md or AGENTS.md) about how to use all this, with the tools or without them.

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
