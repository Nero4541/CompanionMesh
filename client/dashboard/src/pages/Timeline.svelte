<script>
  import { api } from "../lib/api.js";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  let hours = $state(12);
  const { state: info, refresh } = poll(() => `/timeline?hours=${hours}`, 10000, (err) => onAuthError?.(err));
  let message = $state(null);

  const COLOURS = {
    desk_computer: "#5b8def",
    phone: "#b36ae2",
    gaming: "#e2556a",
    reading: "#3fae7a",
    eating: "#e8a33d",
    cooking: "#d9822b",
    watching_tv: "#7a6ff0",
    lying_down: "#8d99ae",
    sleeping: "#4a4e69",
    exercising: "#2a9d8f",
    talking: "#f4a261",
    present: "#9bb1c9",
    away: "#d9d9d9",
    other: "#b0b0b0",
  };

  $effect(() => {
    hours;
    refresh();
  });

  // Episodes grouped by person, placed on the hours shown.
  const lanes = $derived.by(() => {
    const data = info.data;
    if (!data) return [];
    const end = data.now;
    const start = end - hours * 3600;
    const byPerson = new Map();
    for (const e of data.episodes) {
      const key = e.person_id;
      if (!byPerson.has(key)) byPerson.set(key, { name: e.name, role: e.role, items: [] });
      const a = Math.max(e.start, start);
      const b = Math.min(Math.max(e.end, e.start + 30), end);
      byPerson.get(key).items.push({ ...e, left: ((a - start) / (end - start)) * 100, width: Math.max(0.3, ((b - a) / (end - start)) * 100) });
    }
    return [...byPerson.values()];
  });

  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const span = (m) => (m >= 60 ? `${Math.floor(m / 60)} h ${Math.round(m % 60)} min` : `${Math.round(m)} min`);
  const ticks = $derived(info.data ? Array.from({ length: hours + 1 }, (_, i) => info.data.now - (hours - i) * 3600) : []);

  async function removeWatch(id) {
    message = null;
    try {
      await api(`/timeline/watches/${encodeURIComponent(id)}`, { method: "DELETE" });
      refresh();
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      message = err.message;
    }
  }

  const describe = (r) =>
    `${r.person ?? "anyone"}: ${r.activity.replace("_", " ")}${r.posture ? ` (${r.posture.replace("_", " ")})` : ""}` +
    (r.when === "after" ? `, back after ${r.min_minutes} min or more` : ` for ${r.min_minutes} min`) +
    (r.after || r.before ? `, ${r.after ?? "00:00"}–${r.before ?? "24:00"}` : "");
</script>

<div class="head">
  <h2>Timeline</h2>
  <span class="muted">
    What each person has been doing, from the glances of the vision model (or only present and away
    without it). Rules turn long activities into events the companion may speak about.
  </span>
</div>

{#if message}<p class="error">{message}</p>{/if}
{#if info.error && !info.data}<p class="error">{info.error.message}</p>{/if}

{#if info.data}
  {#if !info.data.enabled}
    <p class="muted">The timeline is off: turn it on on the Vision page (or <code>timeline.enabled</code>).</p>
  {:else if !info.data.glance}
    <p class="muted">Glances are off: only present and away are recorded.</p>
  {/if}
  <p class="row">
    Last
    <select bind:value={hours} aria-label="Hours shown">
      {#each [2, 6, 12, 24, 48] as h (h)}<option value={h}>{h} hours</option>{/each}
    </select>
  </p>

  <section class="card">
    {#if !lanes.length}
      <p class="muted">Nothing recorded in this time.</p>
    {:else}
      <div class="axis">
        {#each ticks as t, i (t)}
          {#if i % Math.ceil(hours / 12) === 0}
            <span style={`left: ${(i / hours) * 100}%`}>{clock(t)}</span>
          {/if}
        {/each}
      </div>
      {#each lanes as lane (lane.name)}
        <div class="lane">
          <div class="who">{lane.name}<span class="muted small">{lane.role === "user" ? " · user" : ""}</span></div>
          <div class="bar">
            {#each lane.items as e (e.id)}
              <span
                class="ep"
                style={`left: ${e.left}%; width: ${e.width}%; background: ${COLOURS[e.activity] ?? "#b0b0b0"}`}
                title={`${e.activity.replace("_", " ")} · ${clock(e.start)}–${clock(e.end)} · ${span(e.minutes)}`}
              ></span>
            {/each}
          </div>
        </div>
      {/each}
      <p class="legend">
        {#each Object.entries(COLOURS) as [name, colour] (name)}
          <span><i style={`background: ${colour}`}></i>{name.replace("_", " ")}</span>
        {/each}
      </p>
    {/if}
  </section>

  {#if info.data.episodes.length}
    <section class="card">
      <h3>Episodes</h3>
      <table>
        <thead><tr><th>Who</th><th>Activity</th><th>From</th><th>To</th><th class="num">Length</th></tr></thead>
        <tbody>
          {#each [...info.data.episodes].reverse().slice(0, 60) as e (e.id)}
            <tr>
              <td>{e.name}</td>
              <td>{e.activity.replace("_", " ")}{e.posture !== "unknown" ? `, ${e.posture.replace("_", " ")}` : ""}</td>
              <td>{clock(e.start)}</td>
              <td>{clock(e.end)}</td>
              <td class="num">{span(e.minutes)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </section>
  {/if}

  <section class="card">
    <h3>Rules</h3>
    <ul>
      {#each info.data.rules as r (r.id)}<li><code>{r.id}</code> {describe(r)}</li>{/each}
    </ul>
    <p class="muted small">From <code>timeline.rules</code> in Configuration.</p>
    {#if info.data.watches.length}
      <h3>Watches the agent added</h3>
      <ul>
        {#each info.data.watches as w (w.id)}
          <li>
            <code>{w.id}</code> {describe(w)}{w.note ? ` — ${w.note}` : ""}
            <button class="small" onclick={() => removeWatch(w.id)}>Remove</button>
          </li>
        {/each}
      </ul>
    {/if}
  </section>
{:else if !info.error}
  <p class="muted">Loading…</p>
{/if}

<style>
  section { margin-bottom: 16px; display: flex; flex-direction: column; gap: 8px; }
  h3 { margin: 0; }
  p { margin: 0; }
  .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
  .axis { position: relative; height: 18px; margin-left: 140px; font-size: 11px; color: var(--muted); }
  .axis span { position: absolute; transform: translateX(-50%); }
  .lane { display: flex; align-items: center; gap: 8px; }
  .who { width: 132px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar { position: relative; flex: 1; height: 22px; background: var(--line); border-radius: 4px; }
  .ep { position: absolute; top: 2px; bottom: 2px; border-radius: 3px; }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; color: var(--muted); }
  .legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
  .small { font-size: 13px; }
  ul { margin: 0; padding-left: 18px; }
</style>
