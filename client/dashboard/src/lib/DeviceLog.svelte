<script>
  import { api, getToken } from "./api.js";

  let { deviceId } = $props();

  const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"];
  let lines = $state([]);
  let error = $state(null);
  let level = $state("INFO");
  let follow = $state(true);
  let hasFile = $state(false);
  let box = $state(null);
  let after = 0;

  async function load() {
    try {
      const data = await api(`/devices/${encodeURIComponent(deviceId)}/logs?after=${after}`);
      hasFile = data.file;
      if (data.lines.length) {
        after = data.lines.at(-1).id;
        lines = [...lines, ...data.lines].slice(-1000);
        if (follow) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
      }
      error = null;
    } catch (err) {
      error = err.message;
    }
  }

  $effect(() => {
    deviceId; // reload for another device
    lines = [];
    after = 0;
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 2000);
    return () => clearInterval(timer);
  });

  const shown = $derived(lines.filter((l) => LEVELS.indexOf(l.level) >= LEVELS.indexOf(level)));

  async function download() {
    const headers = {};
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`/v1/admin/devices/${encodeURIComponent(deviceId)}/logs/file`, { headers });
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${deviceId}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function stamp(t) {
    const d = new Date(t * 1000);
    return d.getFullYear() < 2000 ? `(clock unset) ${d.toISOString().slice(11, 19)}` : d.toLocaleString();
  }
</script>

<div class="bar">
  <label>Show
    <select bind:value={level}>
      {#each LEVELS as l}<option value={l}>{l} and up</option>{/each}
    </select>
  </label>
  <label><input type="checkbox" bind:checked={follow} /> follow</label>
  {#if hasFile}<button class="small" onclick={download}>Download the whole log</button>{/if}
  <span class="muted">Lines the device logged, also while it was offline (sent when it is back).</span>
</div>
{#if error}<p class="error">{error}</p>{/if}
<div class="log" bind:this={box}>
  {#each shown as l (l.id)}
    <div class={`line ${l.level.toLowerCase()}`}>
      <span class="t">{stamp(l.t)}</span>
      <span class="lv">{l.level}</span>
      <span class="msg">{l.msg}</span>
    </div>
  {:else}
    <p class="muted">No log lines from this device yet. companion-edge 1.2 and newer send them.</p>
  {/each}
</div>

<style>
  .bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
    margin-bottom: 8px;
  }
  .log {
    max-height: 60vh;
    overflow: auto;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 8px 12px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
  }
  .line {
    display: grid;
    grid-template-columns: auto 64px minmax(0, 1fr);
    gap: 8px;
    padding: 1px 0;
  }
  .t {
    color: var(--muted);
    white-space: nowrap;
  }
  .msg {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .warning .lv {
    color: var(--warn);
  }
  .error .lv {
    color: var(--err);
  }
  .debug {
    color: var(--muted);
  }
  .small {
    font-size: 12px;
  }
  @media (max-width: 720px) {
    .line {
      grid-template-columns: minmax(0, 1fr);
      gap: 0;
      padding: 3px 0;
    }
  }
</style>
