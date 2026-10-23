<script>
  import { api } from "../lib/api.js";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  const { state: engines, refresh: refreshEngines } = poll("/builtin", 2000, (err) => onAuthError?.(err));
  let busy = $state({});
  let logOpen = $state(null);
  let message = $state(null);

  const STATES = {
    off: ["Off", ""],
    not_installed: ["Not installed", "warn"],
    installing: ["Installing…", "live"],
    starting: ["Starting…", "live"],
    running: ["Running", "live"],
    failed: ["Failed", "warn"],
  };

  async function act(key, run, ok) {
    busy = { ...busy, [key]: true };
    message = null;
    try {
      const result = await run();
      if (ok) message = { kind: "ok", text: typeof ok === "function" ? ok(result) : ok };
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      message = { kind: "error", text: err.message };
    } finally {
      busy = { ...busy, [key]: false };
      refreshEngines();
    }
  }

  const json = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const toggle = (enabled) =>
    act("engine", () => api("/builtin/vlm", json("PUT", { enabled })),
      enabled ? "The vision model is starting (the first start downloads it and builds its kernels)." : "Stopped; its memory is free.");
  const useModel = (model) =>
    act("engine", () => api("/builtin/vlm", json("PUT", { model })), `Now ${model}; it restarts with it if it is on.`);
  const install = () => act("engine", () => api("/builtin/vlm/install", { method: "POST" }));

  const choices = (s) => {
    const all = s.models.includes(s.model_folder) || s.model_folder === "auto" ? s.models : [s.model_folder, ...s.models];
    return ["auto", ...all.filter((m) => m !== "auto")];
  };
  const gb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`);
</script>

<div class="head">
  <h2>Vision</h2>
  <span class="muted">
    The vision model describes camera pictures in words (<code>vision.mode: vlm</code>). Built in here, or
    any OpenAI-compatible server (<code>vlm</code> in Configuration).
  </span>
</div>

{#if message}
  <p class={message.kind === "error" ? "error" : "banner"} role="status">{message.text}</p>
{/if}

{#if engines.data}
  {@const s = engines.data.engines.vlm}
  {@const [label, tone] = STATES[s.state] ?? [s.state, ""]}
  <section class="card">
    <div class="row top">
      <h3>Built-in vision model <span class="muted">· Mage-VL</span></h3>
      <span class="pill {tone}">{label}</span>
      <label class="switch">
        <input
          type="checkbox"
          checked={s.enabled}
          disabled={!engines.data.editable || busy.engine || s.state === "installing"}
          onchange={(ev) => toggle(ev.currentTarget.checked)}
        />
        {s.enabled ? "On" : "Off"}
      </label>
    </div>
    <dl>
      <dt>Model</dt>
      <dd class="model">
        <select
          value={s.model_folder}
          disabled={!engines.data.editable || busy.engine || s.state === "installing"}
          onchange={(ev) => useModel(ev.currentTarget.value)}
          aria-label="Vision model"
        >
          {#each choices(s) as m (m)}
            <option value={m}>{m === "auto" ? "auto (FP8 on RTX 40 and newer, else BF16/FP16)" : m}</option>
          {/each}
        </select>
        <span class="muted small">model folders in <code>{s.models_dir}</code></span>
      </dd>
      <dt>Installed</dt>
      <dd>{s.installed ? (s.installed.libraries === "cuda" ? "CUDA libraries" : "CPU libraries") : "no"}</dd>
      {#if s.pid}<dt>Process</dt><dd>{s.pid}{s.in_use ? ", in use" : ""}</dd>{/if}
      {#if engines.data.gpu_memory.total_mb}
        <dt>GPU memory</dt>
        <dd>{gb(engines.data.gpu_memory.used_mb)} of {gb(engines.data.gpu_memory.total_mb)}, all programs</dd>
      {/if}
    </dl>
    {#if s.error}<p class="error">{s.error}</p>{/if}
    {#if s.state === "starting"}
      <p class="muted">The first start downloads the model (about 7 GB) and, for FP8, builds three small kernels for this card (a few minutes).</p>
    {/if}
    <div class="row">
      <button class={s.installed ? "small" : "primary"} disabled={busy.engine || s.state === "installing"} onclick={install}>
        {s.state === "installing" ? "Installing…" : s.installed ? "Install again" : "Install (several GB)"}
      </button>
      <span class="muted small">Settings: <code>builtin.vlm</code>; <code>vlm.max_image_side</code> sizes the pictures</span>
    </div>
    {#if s.log.length}
      <details open={logOpen ?? (s.state === "installing" || s.state === "failed")} ontoggle={(ev) => (logOpen = ev.currentTarget.open)}>
        <summary>{s.state === "installing" ? "Install log" : "Log"}</summary>
        <pre>{s.log.join("\n")}</pre>
      </details>
    {/if}
  </section>
{/if}

<style>
  section { margin-bottom: 16px; display: flex; flex-direction: column; gap: 8px; }
  h3 { margin: 0; }
  p { margin: 0; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .top h3 { flex: 1; }
  .switch { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; margin: 0; font-size: 14px; }
  dt { color: var(--muted); }
  dd { margin: 0; }
  dd.model { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; }
  .small { font-size: 13px; }
  .banner { border: 1px solid var(--ok); border-radius: 8px; padding: 6px 10px; }
  pre { max-height: 240px; overflow: auto; font-size: 12px; }
</style>
