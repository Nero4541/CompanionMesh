# Configuration

| File | Purpose | Committed |
|---|---|---|
| `configs/companion.yaml` | Defaults (non-secret) | yes |
| `configs/companion.local.yaml` | Your overrides, deep-merged over the defaults | no (git-ignored) |
| `.env` | Secrets: API keys and tokens | no (git-ignored) |
| `personas/default.yaml` | The bundled persona: system prompt, language, voice | yes |
| `personas/<name>.yaml` | Your own personas (`agent.persona`) | no (git-ignored) |

The config refers to secrets by environment-variable *name* (`api_key_env`), never by value. `extra_body` on `agent.hermes`, `agent.openclaw`, `llm` and `tts` adds backend-specific fields to every request; core fields such as `model`, `messages` and `stream` cannot be overridden on the agent backends. Example `configs/companion.local.yaml`:

```yaml
agent:
  provider: openclaw
  openclaw:
    base_url: http://100.64.0.12:18789/v1
stt:
  language: null
```

Other options: `uv run companion --config path\to\file.yaml`, `--host`, `--port`, or the `COMPANION_CONFIG` environment variable.

## Where data lives

| Backend | Conversation text on the companion server | Long-term memory |
|---|---|---|
| `hermes`, `openclaw` | In memory only, dropped when the connection closes; nothing is written to disk | On the agent host |
| `direct` | SQLite at `data/companion.db` | Same SQLite file (full-text recall) |

Visual observations (VLM mode descriptions and `vision.event`s) follow the same rule: in memory only with an external agent, which receives them as part of the conversation; in SQLite with `direct`, deleted after `vision.retention_days`.

Microphone audio and synthesized speech are not written to disk, except utterances kept for checking speech recognition when `stt.keep_audio: true` (and microphone tests from the dashboard), under `data/recordings/` for `stt.keep_audio_days`. Camera images are never written to disk unless `vision.store_images: true`; they are then kept under `data/vision/` for `vision.retention_days`. People the companion knows are in `data/people/` (`registry.json` with names and roles, and per person its face and voice samples and voice clips; see [people](people.md)), as are faces found to be posters and, for `people.stranger_retention_h`, recent strangers. `data/` also holds the boards this server set up (`devices.json`: token hashes, the root passwords it gave them, their last settings), their logs (`devices/<id>/edge.log`), this server's SSH key for its boards (`ssh/`) and the public listener's certificate (`tls/`); keep it private. Logs contain transcripts at `INFO` level; raise `logging.level` to `WARNING` if you do not want that.

With an external agent, reconnecting with the same session keeps the conversation going (the agent remembers), but the dev client will not replay earlier messages.
