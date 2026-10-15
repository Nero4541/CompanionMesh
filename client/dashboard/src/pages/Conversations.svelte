<script>
  import { api, getToken } from "../lib/api.js";

  let { onAuthError } = $props();

  let conv = $state(null); // {enabled, lines}
  let recs = $state(null); // {enabled, days, recordings}
  let error = $state(null);
  let audio = $state({}); // recording id -> object URL
  let after = 0;

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

  // Lines grouped by conversation (session), newest conversation first.
  const sessions = $derived.by(() => {
    const groups = new Map();
    for (const line of conv?.lines ?? []) {
      if (!groups.has(line.session_id)) groups.set(line.session_id, []);
      groups.get(line.session_id).push(line);
    }
    return [...groups.entries()].reverse();
  });

  const OUTCOME = { turn: "answered", echo: "dropped: its own voice", empty: "nothing recognized" };
  const time = (t) => new Date(t * 1000).toLocaleTimeString();
</script>

<div class="head">
  <h2>Conversations</h2>
  <span class="muted">What the companion heard and said, and recordings of what reached speech recognition.</span>
</div>

{#if error}<p class="error">{error}</p>{/if}

<section class="card">
  <h3>Conversation</h3>
  {#if conv && !conv.enabled}
    <p class="note">
      Off. The dashboard only sees how long each line is. To show the words, turn on
      <code>server.dashboard_conversation</code> under Configuration (takes effect at once; nothing said before
      is shown).
    </p>
  {:else if sessions.length}
    {#each sessions as [sid, lines] (sid)}
      <div class="session">
        <p class="muted">conversation <code>{sid.slice(0, 8)}</code></p>
        {#each lines as l (l.id)}
          <div class={`line ${l.role}`}>
            <span class="t">{time(l.t)}</span>
            <span class="who">{l.role === "user" ? (l.typed ? "typed" : "heard") : "companion"}</span>
            <span class="text">
              {l.text}
              {#if l.cancelled}<span class="pill">cut off</span>{/if}
              {#if l.recording_id}
                {#if audio[l.recording_id]}
                  <audio controls autoplay src={audio[l.recording_id]}></audio>
                {:else}
                  <button class="small" onclick={() => play(l.recording_id)}>Listen</button>
                {/if}
              {/if}
            </span>
          </div>
        {/each}
      </div>
    {/each}
  {:else if conv}
    <p class="muted">Nothing said since the server started (or since this was turned on).</p>
  {/if}
</section>

<section class="card">
  <h3>Recordings</h3>
  {#if recs && !recs.enabled && !recs.recordings.length}
    <p class="note">
      Off. Turn on <code>stt.keep_audio</code> under Configuration to keep each utterance as the server heard it
      (deleted after <code>stt.keep_audio_days</code> days). Useful when it mishears: listen to what the
      microphone really delivered.
    </p>
  {:else if recs?.recordings.length}
    {#if !recs.enabled}<p class="muted">Off now; these are from before and go after {recs.days} days.</p>{/if}
    <div class="scroll">
      <table>
        <thead><tr><th>Time</th><th>Device</th><th class="num">Length</th><th class="num">STT</th><th>Recognized</th><th>Result</th><th></th></tr></thead>
        <tbody>
          {#each recs.recordings as r (r.id)}
            <tr>
              <td>{new Date(r.timestamp * 1000).toLocaleString()}</td>
              <td><code>{r.device_id ?? "–"}</code></td>
              <td class="num">{r.duration_s.toFixed(1)} s</td>
              <td class="num">{r.stt_s != null ? `${r.stt_s.toFixed(2)} s` : "–"}</td>
              <td class="wrap">{r.text || "–"}</td>
              <td>{OUTCOME[r.outcome] ?? r.outcome}</td>
              <td>
                {#if audio[r.id]}<audio controls autoplay src={audio[r.id]}></audio>
                {:else}<button class="small" onclick={() => play(r.id)}>Listen</button>{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if recs}
    <p class="muted">No recordings yet.</p>
  {/if}
</section>

<style>
  section {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3 {
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
  .text {
    overflow-wrap: anywhere;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  td.wrap {
    white-space: normal;
    min-width: 160px;
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
