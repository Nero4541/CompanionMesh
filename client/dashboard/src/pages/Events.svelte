<script>
  import { ApiError, getToken } from "../lib/api.js";

  let { onAuthError } = $props();

  const MAX = 500;
  const CATEGORIES = [
    { id: "devices", label: "Devices" },
    { id: "state", label: "State" },
    { id: "attention", label: "Attention" },
    { id: "vision", label: "Vision" },
    { id: "conversation", label: "Conversation" },
    { id: "errors", label: "Errors" },
    { id: "settings", label: "Settings" },
    { id: "other", label: "Other" },
  ];

  let events = $state([]); // newest first
  let status = $state("connecting"); // connecting | live | closed
  let paused = $state(false);
  let held = $state([]); // arrived while paused
  let shown = $state(new Set(CATEGORIES.map((c) => c.id)));
  let open = $state(null); // id of the expanded event

  function category(type) {
    if (type.startsWith("notice.device_") || type.startsWith("session.")) return "devices";
    if (type === "system.state") return "state";
    if (type.startsWith("attention.")) return "attention";
    if (type.startsWith("vision.")) return "vision";
    if (type === "system.error") return "errors";
    if (type === "notice.settings_saved" || type === "notice.device_settings") return "settings";
    if (/^(conversation|agent|audio|metrics)\./.test(type)) return "conversation";
    return "other";
  }

  const fixed = (n) => (typeof n === "number" ? n.toFixed(2) : n);

  function summary(e) {
    const p = e.payload ?? {};
    switch (e.type) {
      case "notice.device_connected":
        return `${p.device_id ?? "a device"} connected (${(p.roles ?? []).join(", ")})${p.resumed ? ", resumed" : ""}`;
      case "notice.device_left":
        return `${p.device_id ?? "a device"} left`;
      case "session.devices":
        return `devices: ${(p.devices ?? []).map((d) => d.device_id ?? "?").join(", ") || "none"}`;
      case "session.ended":
        return `conversation ended${p.reason ? ` (${p.reason})` : ""}`;
      case "system.state":
        return `${p.previous ?? "?"} → ${p.state}${p.reason ? ` (${p.reason})` : ""}`;
      case "attention.decision":
        return `${p.decision}: ${p.kind}${p.description ? ` — ${p.description}` : ""} (salience ${fixed(p.salience)})`;
      case "attention.state":
        return `proactive ${p.proactive ? "on" : "off"}, quiet ${p.quiet ? "on" : "off"}${(p.speech_blockers ?? []).length ? `, held back by ${p.speech_blockers.join(", ")}` : ""}`;
      case "vision.observation":
        return p.description ?? "observation";
      case "vision.frame.status":
        return `frame ${p.status}${p.detail ? ` (${p.detail})` : ""}`;
      case "vision.state":
        return `vision ${p.enabled ? "on" : "off"}${p.available === false ? " (unavailable)" : ""}`;
      case "vision.capture.request":
        return `asked cameras for a picture (${p.reason ?? "turn"})`;
      case "vision.frame.used":
        return `frame attached to turn ${String(p.turn_id ?? "").slice(0, 8)}`;
      case "conversation.response.start":
        return p.proactive ? `the companion speaks first (${p.reason ?? "attention"})` : "reply started";
      case "conversation.response.done":
        return `reply ${p.cancelled ? "cut off" : "done"} (${p.text_length ?? 0} characters)${p.reason ? `, ${p.reason}` : ""}`;
      case "conversation.transcript":
        return `heard ${p.text_length ?? 0} characters${p.final ? "" : " (partial)"}`;
      case "audio.vad":
        return p.state === "speech_start" ? "speech started" : p.state === "speech_end" ? "speech ended" : `vad ${p.state}`;
      case "agent.tool.progress":
        return `tool ${p.tool}: ${p.label ?? ""} ${p.status ?? ""}`.trim();
      case "metrics.turn":
        return Object.entries(p)
          .filter(([k]) => k !== "turn_id")
          .map(([k, v]) => `${k} ${fixed(v)} s`)
          .join(", ");
      case "system.error":
        return `${p.code}: ${p.message}`;
      case "notice.settings_saved":
        return `settings saved${(p.applied ?? []).length ? `; applied ${p.applied.join(", ")}` : ""}${(p.pending_restart ?? []).length ? `; after a restart: ${p.pending_restart.join(", ")}` : ""}`;
      case "notice.device_settings":
        return `${p.device_id}: settings ${p.ok ? (p.restarting ? "saved, restarting" : "unchanged") : `refused (${p.error})`}`;
      default:
        return JSON.stringify(p);
    }
  }

  function add(list) {
    const fresh = list.map((e) => ({ ...e, category: category(e.type), text: summary(e) }));
    if (paused) {
      held = [...fresh.reverse(), ...held].slice(0, MAX);
      return;
    }
    events = [...fresh.reverse(), ...events].slice(0, MAX);
  }

  function resume() {
    events = [...held, ...events].slice(0, MAX);
    held = [];
    paused = false;
  }

  function toggle(id) {
    const next = new Set(shown);
    next.has(id) ? next.delete(id) : next.add(id);
    shown = next;
  }

  const visible = $derived(events.filter((e) => shown.has(e.category)));
  const counts = $derived(
    events.reduce((acc, e) => ((acc[e.category] = (acc[e.category] ?? 0) + 1), acc), {}),
  );

  $effect(() => {
    let socket = null;
    let retry = null;
    let delay = 1000;
    let stopped = false;

    function connect() {
      status = "connecting";
      const token = getToken();
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const query = token ? `?token=${encodeURIComponent(token)}` : "";
      socket = new WebSocket(`${scheme}://${window.location.host}/v1/admin/events${query}`);
      socket.onmessage = (msg) => {
        const data = JSON.parse(msg.data);
        if (data.error) {
          stopped = true;
          onAuthError?.(new ApiError(data.status, data.error));
          return;
        }
        if (data.recent) {
          events = [];
          add(data.recent);
        }
        if (data.event) add([data.event]);
        status = "live";
        delay = 1000;
      };
      socket.onclose = () => {
        if (stopped) return;
        status = "closed";
        retry = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      };
    }

    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      socket?.close();
    };
  });

  const time = (ts) => {
    const d = new Date(ts);
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString();
  };
</script>

<div class="head">
  <h2>Events</h2>
  <span class="pill" class:ok={status === "live"} class:warn={status !== "live"}>
    {status === "live" ? "live" : status === "connecting" ? "connecting…" : "reconnecting…"}
  </span>
  <span class="muted">What the server is doing, as it happens. What people say is never shown.</span>
</div>

<div class="toolbar">
  <div class="filters" role="group" aria-label="Show">
    {#each CATEGORIES as c}
      <button class="chip {c.id}" class:off={!shown.has(c.id)} onclick={() => toggle(c.id)} aria-pressed={shown.has(c.id)}>
        {c.label} <span class="count">{counts[c.id] ?? 0}</span>
      </button>
    {/each}
  </div>
  <div class="actions">
    {#if paused}
      <button class="primary" onclick={resume}>Resume{held.length ? ` (${held.length} new)` : ""}</button>
    {:else}
      <button onclick={() => (paused = true)}>Pause</button>
    {/if}
    <button onclick={() => ((events = []), (held = []))}>Clear</button>
  </div>
</div>

{#if visible.length}
  <ol class="list">
    {#each visible as e (e.id)}
      <li class:open={open === e.id}>
        <button class="row" onclick={() => (open = open === e.id ? null : e.id)} aria-expanded={open === e.id}>
          <span class="time">{time(e.timestamp)}</span>
          <span class="chip small {e.category}">{e.category}</span>
          <span class="session" title={e.session_id ?? ""}>{e.session_id ? e.session_id.slice(0, 6) : "—"}</span>
          <span class="text">{e.text}</span>
        </button>
        {#if open === e.id}
          <pre class="raw"><code>{e.type}
{JSON.stringify(e.payload, null, 2)}</code></pre>
        {/if}
      </li>
    {/each}
  </ol>
{:else}
  <p class="muted">
    {events.length ? "Nothing in the categories shown." : "Nothing yet. Events appear as devices connect and conversations happen."}
  </p>
{/if}

<style>
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 8px 16px;
    margin-bottom: 12px;
  }
  .filters,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    --c: var(--muted);
    border: 1px solid var(--c);
    color: var(--c);
    background: transparent;
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 13px;
  }
  .chip.off {
    opacity: 0.45;
  }
  .chip.small {
    padding: 0 8px;
    font-size: 12px;
    justify-self: start;
  }
  .count {
    opacity: 0.8;
    font-variant-numeric: tabular-nums;
  }
  .devices {
    --c: #2f8fa8;
  }
  .state {
    --c: var(--muted);
  }
  .attention {
    --c: #9b59b6;
  }
  .vision {
    --c: #2c8a4b;
  }
  .conversation {
    --c: var(--accent);
  }
  .errors {
    --c: var(--err);
  }
  .settings {
    --c: var(--warn);
  }
  .list {
    list-style: none;
    margin: 0;
    padding: 0;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
  }
  .list li + li {
    border-top: 1px solid var(--line);
  }
  .row {
    all: unset;
    box-sizing: border-box;
    width: 100%;
    display: grid;
    grid-template-columns: 76px 104px 64px minmax(0, 1fr);
    gap: 8px;
    align-items: baseline;
    padding: 6px 12px;
    cursor: pointer;
    font-size: 14px;
  }
  .row:hover {
    background: var(--bg);
  }
  .row:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
  .time,
  .session {
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    font-size: 13px;
  }
  .session {
    font-family: ui-monospace, Consolas, monospace;
  }
  .text {
    overflow-wrap: anywhere;
  }
  .raw {
    margin: 0;
    padding: 8px 12px 12px 12px;
    font-size: 12px;
    overflow-x: auto;
    background: var(--bg);
  }
  @media (max-width: 640px) {
    .row {
      grid-template-columns: 70px minmax(0, 1fr);
    }
    .row .session {
      display: none;
    }
    .row .chip {
      display: none;
    }
  }
</style>
