<script>
  import { api, getToken } from "../lib/api.js";

  let { onAuthError } = $props();

  let conv = $state(null); // {enabled, lines}
  let recs = $state(null); // {enabled, days, recordings}
  let error = $state(null);
  let audio = $state({}); // recording id -> object URL
  let viewer = $state(null); // the tailnet's read-only page: {enabled, cameras, listening, urls}
  let after = 0;

  $effect(() => {
    api("/viewer")
      .then((v) => (viewer = v))
      .catch(() => {});
  });

  async function load() {
    try {
      const c = await api(`/conversation?after=${after}`);
      if (!conv || c.enabled !== conv.enabled) conv = { enabled: c.enabled, lines: [] };
      if (c.lines.length) {
        after = c.lines.at(-1).id;
        conv.lines = [...conv.lines, ...c.lines].slice(-400);
      }
      recs = await api("/recordings?limit=100");
      error = null;
    } catch (err) {
      error = err.message;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  $effect(() => {
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 3000);
    return () => {
      clearInterval(timer);
      for (const url of Object.values(audio)) URL.revokeObjectURL(url);
    };
  });

  async function play(id) {
    if (!audio[id]) {
      const headers = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(`/v1/admin/recordings/${id}/audio`, { headers });
      if (!res.ok) return;
      audio[id] = URL.createObjectURL(await res.blob());
    }
  }

  // One timeline: the conversation's lines, each with its recording (if kept), and
  // the recordings that never became a line (dropped as the companion's own voice,
  // nothing recognized, or from before the server started). Newest first, grouped
  // by conversation (session).
  const sessions = $derived.by(() => {
    const byId = new Map((recs?.recordings ?? []).map((r) => [r.id, r]));
    const items = [];
    for (const line of conv?.lines ?? []) {
      const rec = line.recording_id ? byId.get(line.recording_id) : undefined;
      if (rec) byId.delete(rec.id);
      items.push({ key: `l${line.id}`, ...line, rec });
    }
    for (const rec of byId.values()) {
      items.push({
        key: `r${rec.id}`,
        t: rec.timestamp,
        session_id: rec.session_id ?? "?",
        role: "user",
        text: rec.text,
        device_id: rec.device_id,
        rec,
        unanswered: rec.outcome !== "turn",
      });
    }
    items.sort((a, b) => b.t - a.t);
    const groups = new Map();
    for (const item of items) {
      if (!groups.has(item.session_id)) groups.set(item.session_id, []);
      groups.get(item.session_id).push(item);
    }
    return [...groups.entries()];
  });

  const OUTCOME = { turn: "answered", echo: "dropped: its own voice", empty: "nothing recognized" };
  const today = new Date().toDateString();
  function when(t) {
    const d = new Date(t * 1000);
    return d.toDateString() === today ? d.toLocaleTimeString() : d.toLocaleString();
  }
  const SPEAKER = { stranger: "a stranger", media: "TV / media", unknown: "someone" };
  // Who said it, with perception.speakers on: a name, a stranger, the TV.
  const heard = (s) => (s ? (s.name ?? SPEAKER[s.role] ?? "heard") : "heard");
  const who = (l) => (l.role === "user" ? (l.typed ? "typed" : heard(l.speaker)) : "companion");
</script>

<div class="head">
  <h2>Conversations</h2>
  <span class="muted">What the companion heard and said, newest first, with recordings of what reached speech recognition.</span>
</div>

{#if error}<p class="error">{error}</p>{/if}

{#if viewer}
  <section class="card">
    <h3>On your phone</h3>
    {#if !viewer.enabled}
      <p class="muted">
        Turn on <code>server.viewer.enabled</code> under Configuration to follow the conversation{viewer.cameras
          ? " and what the devices see"
          : ""} on a phone in your Tailscale network (read only; applies at once).
      </p>
    {:else if !viewer.urls.length}
      <p class="muted">The viewer is on, but this machine is not on Tailscale: install it here and sign in.</p>
    {:else}
      <p>
        On a phone in the same tailnet, open
        {#each viewer.urls as u, i}{#if i}, or {/if}<a href={u} target="_blank" rel="noopener noreferrer"><code>{u}</code></a>{/each}
      </p>
      {#if ["127.0.0.1", "localhost", "::1"].includes(viewer.listening)}
        <p class="error small">
          The server listens only on this machine (<code>server.host: {viewer.listening}</code>): set it to
          <code>0.0.0.0</code> under Configuration and restart, or the phone cannot reach it.
        </p>
      {/if}
    {/if}
  </section>
{/if}

<section class="card">
  {#if conv && !conv.enabled}
    <p class="note">
      The conversation is not shown: the dashboard only sees how long each line is. To show the words, turn on
      <code>server.dashboard_conversation</code> under Configuration (takes effect at once; nothing said before
      is shown).{#if recs?.recordings.length} Below are only the recordings.{/if}
    </p>
  {/if}
  {#if recs && !recs.enabled && !recs.recordings.length}
    <p class="note">
      Recordings are off. Turn on <code>stt.keep_audio</code> under Configuration to keep each utterance as the
      server heard it (deleted after <code>stt.keep_audio_days</code> days), and listen to it here. Useful when it
      mishears: hear what the microphone really delivered.
    </p>
  {:else if recs && !recs.enabled}
    <p class="muted">Recordings are off now; those shown are from before and go after {recs.days} days.</p>
  {/if}

  {#if sessions.length}
    {#each sessions as [sid, items] (sid)}
      <div class="session">
        <p class="muted">conversation <code>{String(sid).slice(0, 8)}</code></p>
        {#each items as l (l.key)}
          <div class={`line ${l.role}${l.unanswered ? " dropped" : ""}`}>
            <span class="t">{when(l.t)}</span>
            <span class="who">{who(l)}</span>
            <span class="text">
              {l.text || "–"}
              {#if l.cancelled}<span class="pill">cut off</span>{/if}
              {#if l.unanswered}<span class="pill">{OUTCOME[l.rec.outcome] ?? l.rec.outcome}</span>{/if}
              {#if l.rec}
                {#if audio[l.rec.id]}
                  <audio controls autoplay src={audio[l.rec.id]}></audio>
                {:else}
                  <button class="small" onclick={() => play(l.rec.id)}>Listen</button>
                {/if}
                <span class="meta">
                  {l.rec.duration_s.toFixed(1)} s{#if l.rec.stt_s != null}, recognized in {l.rec.stt_s.toFixed(2)} s{/if}{#if l.rec.device_id}, from <code>{l.rec.device_id}</code>{/if}
                </span>
              {/if}
            </span>
          </div>
        {/each}
      </div>
    {/each}
  {:else if conv && recs}
    <p class="muted">Nothing said since the server started (or since this was turned on).</p>
  {/if}
</section>

<style>
  section {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3,
  section > p {
    margin: 0;
  }
  .note {
    margin: 0;
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
  .session {
    border-top: 1px solid var(--line);
    padding-top: 6px;
  }
  .session p {
    margin: 0 0 4px;
  }
  .line {
    display: grid;
    grid-template-columns: auto 80px minmax(0, 1fr);
    gap: 8px;
    padding: 3px 0;
    font-size: 14px;
  }
  .t,
  .who {
    color: var(--muted);
    font-size: 12px;
    white-space: nowrap;
    padding-top: 2px;
  }
  .companion .text {
    color: var(--accent);
  }
  .dropped .text {
    color: var(--muted);
  }
  .text {
    overflow-wrap: anywhere;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .meta {
    color: var(--muted);
    font-size: 12px;
  }
  audio {
    height: 28px;
    max-width: 240px;
  }
  .small {
    font-size: 12px;
  }
  @media (max-width: 720px) {
    .line {
      grid-template-columns: auto minmax(0, 1fr);
    }
    .line .text {
      grid-column: 1 / -1;
    }
  }
</style>
