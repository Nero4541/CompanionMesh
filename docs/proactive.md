# Proactive behavior

> With [people](people.md) on, arrivals and departures (`person_arrived`, `user_returned`, …) are events too, and with `routing.enabled` talk between people, strangers and the TV is no longer answered: it becomes context (see [people](people.md#answering-only-what-is-meant-for-the-companion)).

The companion can speak first, under strict rules. Every noticed event goes through a fixed policy:

```text
event -> salience -> repeat? -> anyone talking / turn in progress?
      -> proactive switch, do-not-disturb, quiet hours, cooldown, hourly budget
      -> IGNORE | REMEMBER | CONTEXT_ONLY | SPEAK
```

| Decision | Effect |
|---|---|
| `ignore` | Nothing |
| `remember` | Kept as an episodic record |
| `context_only` | Told to the agent with your next message ("Noticed since the user's last message: …") |
| `speak` | A proactive turn: the agent gets a description of the event (and the current camera image in agent mode) and may say one short remark, or reply `[SILENT]` to stay quiet |

Events come from the camera and from clients:

- **Scene change:** the camera view changes a lot (`attention.scene_change_threshold`, in difference-hash bits). Bigger changes score higher; a moderate change is context only.
- **Activity resumed:** a big change after the view stayed still for `attention.absence_s` (20 minutes by default), e.g. you came back to your desk. Scores high enough to speak.
- **Client events:** `vision.event` with an optional `salience` (0–1), for devices that detect things themselves.
- **Idle check-ins** (off by default): with `attention.idle: true`, once nobody has spoken for a random while between `attention.idle_min_s` and `attention.idle_max_s` (30–60 minutes by default), the agent may start a conversation. It is shown a fresh camera frame if vision is on. If the guards below block speech at that moment, the wait simply starts over.

The rules that keep it polite are deterministic; no model can override them:

- at most one proactive turn every `attention.min_interval_s` (15 minutes) and `attention.max_per_hour` (4);
- nothing within `attention.grace_after_activity_s` of you or the companion speaking, and nothing while a turn is in progress;
- quiet hours, `23:00`–`08:00` by default, in the **host's time zone** unless `attention.quiet_hours.timezone` names one (e.g. `Asia/Tokyo`);
- do-not-disturb at any time: the dev client's **quiet** button (`attention.quiet`);
- `attention.proactive: false` turns proactive speech off entirely; normal conversation is unaffected.

The companion never reacts to its own voice: audio input is ignored while it speaks. A proactive turn that is still waiting for the agent keeps listening, so you can simply start talking, and your turn takes over. Set `attention.debug: true` to see every decision and its reasons in the dev client.

With slow local models, a proactive turn takes as long as any other turn; the agent's reply is only spoken once it is complete.

## Explicit decisions

Free text with `[SILENT]` leaves room for mistakes: models answer anyway, or offer two alternatives. With `attention.decisions: true` (off by default) a proactive turn asks the agent for one JSON object instead, with the event, who is around (with [people](people.md) on), what has been going on and the time:

```json
{"action": "reply", "text": "...", "reason": "the user has been gaming for two hours and it is late"}
{"action": "silent", "reason": "nothing new"}
{"action": "remember", "note": "Kenta visited at 20:10", "reason": "worth keeping, not worth saying"}
```

`reply` is spoken; `remember` is kept as an episodic note (like `remember` above); `silent` says nothing. The answer is read leniently (the first JSON object in it, code fences and text around it ignored); anything else counts as silent, and is logged and counted as `proactive_unparsed` (Latency page). A bare `[SILENT]` still means silent. The guards above still come first: no answer can make the companion speak when they say no.

**Triage model** (`attention.triage`, off by default): a small, fast model on an OpenAI-compatible server (Ollama, llama.cpp, vLLM, LM Studio) is asked the same question first; only when it does not choose silent is the agent asked. That keeps slow agent turns for events that matter.

```yaml
attention:
  decisions: true
  triage:
    enabled: true
    base_url: http://127.0.0.1:11434/v1
    model: qwen3:1.7b
    extra_body: {chat_template_kwargs: {enable_thinking: false}}
```

Each decision (by the agent or the triage model, with its reason) shows on the dashboard's **Events** page as `attention.proactive`.
