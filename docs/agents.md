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

With `mcp.enabled: true` (off by default) the server offers the agent tools of its own, through an MCP server at `/mcp` (the Model Context Protocol, streamable HTTP). Hermes and OpenClaw connect to it the same way:

| Tool | Gives or does |
|---|---|
| `who_is_here()` | who is around now: name, role (user, acquaintance, stranger), since when, which camera, what they are doing, who is speaking |
| `timeline(minutes=120, person=null)` | what each person has been doing ([timeline](people.md#timeline)) |
| `recent_speech(minutes=10)` | what was said lately, by whom, and whether it was said to the companion or overheard |
| `look(camera=null)` | a camera's newest picture (as an image) and, with a vision model, a short description |
| `remember_person(track_id, name, role)` | add the stranger in view as a person; only after the user said yes (see below) |
| `forget_person(name)` | forget a person; their samples are deleted at once |
| `list_people()` | the people known (names, roles, notes); never face or voice data |
| `set_watch(activity, min_minutes, …)`, `list_watches()`, `remove_watch(id)` | watches on long activities, which become `long_activity` events |

Every tool also answers at `POST /v1/tools/<name>` with its arguments as a JSON object (and `GET /v1/tools` lists them), for anything that cannot load MCP servers.

**Access.** Put a token in the server's `.env` as `COMPANION_AGENT_TOKEN` (`uv run companion agent-tools --token` makes one) and the same value in the agent's environment; the agent sends it as `Authorization: Bearer`. While it is not set, the tools answer only this machine. The agent reaches the server at `http://<this machine>:<server.port>/mcp`: `127.0.0.1` when it runs here, the machine's Tailscale address when it runs elsewhere. A Hermes inside WSL2 reaches Windows at `127.0.0.1` only with WSL's mirrored networking; otherwise use the Windows machine's address (its Tailscale address works).

**Hermes**: add to `~/.hermes/config.yaml` (Hermes reads `${…}` from its environment, e.g. `~/.hermes/.env`), then restart Hermes:

```yaml
mcp_servers:
  companion:
    url: http://127.0.0.1:8765/mcp
    headers:
      Authorization: Bearer ${COMPANION_AGENT_TOKEN}
    timeout: 60
    connect_timeout: 10
```

**OpenClaw**: `mcp.servers` in `openclaw.json` (OpenClaw's own MCP client), or the same with `openclaw mcp set companion '<the entry as JSON>'`:

```json
{
  "mcp": {
    "servers": {
      "companion": {
        "url": "http://127.0.0.1:8765/mcp",
        "transport": "streamable-http",
        "connectionTimeoutMs": 10000,
        "requestTimeoutMs": 60000,
        "headers": { "Authorization": "Bearer ${COMPANION_AGENT_TOKEN}" }
      }
    }
  }
}
```

`uv run companion agent-tools` prints both for this server's address (`--url` for another), and `--write-hermes <config.yaml>` or `--write-openclaw <openclaw.json>` adds the entry to a file, keeping a copy of it as `.bak-companion` and everything else as it was.

For OpenClaw, put the token in `~/.openclaw/.env` and restart the gateway (`systemctl --user restart openclaw-gateway`) **before** saving the entry: OpenClaw fills in `${COMPANION_AGENT_TOKEN}` when it loads its config and refuses a config whose variable is missing. `openclaw mcp probe companion` then lists the tools (`openclaw mcp add` probes with the text unfilled, so it is refused; `set`, or `add --no-probe`, saves it). Every agent with the `coding` or `messaging` tool profile gets the tools, as `companion__who_is_here` and so on; to keep them from an agent, add `"tools": {"deny": ["companion__*"]}` to its entry in `agents.list`.

**Remembering people.** `remember_person` adds a face the cameras see now. With `mcp.consent: conversation` (default) it works only when the user's last sentence, at most `mcp.consent_window_s` (5 minutes) ago, said yes ("yes", "OK", "はい", "いいよ", "好的", …): the agent asks first, then calls it. With `mcp.consent: dashboard` the request waits on the People page until you confirm it.

Without the tools the agent still gets the same facts as short lines with each message: who said it (`「…」 (Yukari, the user)`), what was overheard, who is around and what has been going on in proactive turns. [agent-guide.md](agent-guide.md) is a text to give the agent (in its SOUL.md or AGENTS.md) about how to use all this.

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
