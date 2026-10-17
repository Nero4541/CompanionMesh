<script>
  import { api } from "../lib/api.js";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  const { state: info, refresh } = poll("/builtin", 2000, (err) => onAuthError?.(err));
  let busy = $state({});
  // Logs opened or closed by hand stay so across refreshes; until then they open
  // by themselves while installing or after a failure.
  let logOpen = $state({});
  let message = $state(null);

  const ENGINES = [
    {
      name: "stt",
      title: "Speech recognition",
      what: "Whisper (faster-whisper)",
      settings: "builtin.stt (device, compute_type) and stt.language, stt.prompt",
    },
    {
      name: "tts",
      title: "Voice",
      what: "Irodori-TTS, a Japanese voice",
      settings: "builtin.tts (device, precision) and tts.voice",
    },
  ];

  const STATES = {
    off: ["Off", ""],
    not_installed: ["Not installed", "warn"],
    installing: ["Installing…", "live"],
    starting: ["Starting…", "live"],
    running: ["Running", "live"],
    failed: ["Failed", "warn"],
  };

  async function act(name, what, run) {
    busy = { ...busy, [name]: what };
    message = null;
    try {
      await run();
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      message = { kind: "error", text: `${name}: ${err.message}` };
    } finally {
      busy = { ...busy, [name]: null };
      refresh();
    }
  }

  function toggle(name, enabled) {
    act(name, enabled ? "starting" : "stopping", async () => {
      await api(`/builtin/${name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!enabled) message = { kind: "ok", text: `${name}: stopped; its memory is free.` };
    });
  }

  function useModel(name, model) {
    act(name, "switching", async () => {
      await api(`/builtin/${name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model }),
      });
      message = { kind: "ok", text: `${name}: now ${model}; it restarts with it if it is on.` };
    });
  }

  // The model folders, and the configured model's folder even before it is downloaded.
  const choices = (s) => (s.models.includes(s.model_folder) ? s.models : [s.model_folder, ...s.models]);

  function install(name) {
    act(name, "installing", () => api(`/builtin/${name}/install`, { method: "POST" }));
  }

  const gb = (mb) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`);
  const when = (s) => (s ? new Date(s * 1000).toLocaleString() : "–");
</script>

<div class="head">
  <h2>Speech</h2>
  <span class="muted">
    Built-in speech recognition and voice, installed and run by this server: no other server needed.
    Off, an engine's process ends and its GPU memory is free at once.
  </span>
</div>

{#if message}
  <p class={message.kind === "error" ? "error" : "banner"} role="status">{message.text}</p>
{/if}
{#if info.error && !info.data}<p class="error">{info.error.message}</p>{/if}

{#if info.data}
  <p class="muted">
    Libraries: <strong>{info.data.libraries === "cuda" ? "CUDA (NVIDIA GPU)" : "CPU"}</strong>
    {#if !info.data.gpu}(no NVIDIA GPU found: speech is slower, the voice much slower){/if}
    · <code>builtin.libraries</code> in Configuration changes it.
    {#if info.data.gpu_memory.total_mb}
      <br />GPU memory in use, all programs: <strong>{gb(info.data.gpu_memory.used_mb)}</strong> of
      {gb(info.data.gpu_memory.total_mb)}
    {/if}
  </p>
  {#each ENGINES as e (e.name)}
    {@const s = info.data.engines[e.name]}
    {@const [label, tone] = STATES[s.state] ?? [s.state, ""]}
    <section class="card">
      <div class="row top">
        <h3>{e.title} <span class="muted">· {e.what}</span></h3>
        <span class="pill {tone}">{label}</span>
        <label class="switch">
          <input
            type="checkbox"
            checked={s.enabled}
            disabled={!info.data.editable || busy[e.name] || s.state === "installing"}
            onchange={(ev) => toggle(e.name, ev.currentTarget.checked)}
          />
          {s.enabled ? "On" : "Off"}
        </label>
      </div>

      <dl>
        <dt>Model</dt>
        <dd class="model">
          <select
            value={s.model_folder}
            disabled={!info.data.editable || busy[e.name] || s.state === "installing"}
            onchange={(ev) => useModel(e.name, ev.currentTarget.value)}
            aria-label={`${e.title} model`}
          >
            {#each choices(s) as m (m)}
              <option value={m}>{m}{s.models.includes(m) ? "" : " (downloads when it starts)"}</option>
            {/each}
          </select>
          <span class="muted small">
            model folders in <code>{s.models_dir}</code>{#if s.voices_dir}; voices and embeddings in
              <code>{s.voices_dir}</code>{/if}
          </span>
        </dd>
        <dt>Installed</dt>
        <dd>
          {#if s.installed}{s.installed.libraries === "cuda" ? "CUDA" : "CPU"} libraries, {when(s.installed.installed_at)}
          {:else}no{/if}
        </dd>
        {#if s.pid}
          <dt>Process</dt>
          <dd>{s.pid}{s.in_use ? ", in use" : ""}</dd>
          <dt>GPU memory</dt>
          <dd>
            {#if s.vram_mb != null}{gb(s.vram_mb)}
            {:else if s.installed?.libraries === "cuda"}<span class="muted">not reported per program here; see the total above</span>
            {:else}none (on the CPU){/if}
          </dd>
        {/if}
        <dt>Since</dt>
        <dd>{when(s.since)}</dd>
      </dl>

      {#if s.error}<p class="error">{s.error}</p>{/if}
      {#if s.enabled && s.state === "not_installed"}
        <p class="muted">It is on, but not installed yet: install it and it starts by itself.</p>
      {/if}
      {#if s.state === "starting"}
        <p class="muted">The first start downloads the model; that can take a few minutes.</p>
      {/if}

      <div class="row">
        <button
          class={s.installed ? "small" : "primary"}
          disabled={busy[e.name] || s.state === "installing"}
          onclick={() => install(e.name)}
        >
          {s.state === "installing" ? "Installing…" : s.installed ? "Install again" : "Install (several GB)"}
        </button>
        <span class="muted small">Settings: {e.settings}</span>
      </div>

      {#if s.log.length}
        <details
          open={logOpen[e.name] ?? (s.state === "installing" || s.state === "failed")}
          ontoggle={(ev) => (logOpen[e.name] = ev.currentTarget.open)}
        >
          <summary>{s.state === "installing" ? "Install log" : "Log"}</summary>
          <pre>{s.log.join("\n")}</pre>
        </details>
      {/if}
    </section>
  {/each}
{:else if !info.error}
  <p class="muted">Loading…</p>
{/if}

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
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .top h3 {
    flex: 1;
  }
  .switch {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  dl {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 2px 12px;
    margin: 0;
    font-size: 14px;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
  }
  dd.model {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 8px;
    align-items: center;
  }
  dd.model code {
    word-break: break-all;
  }
  p {
    margin: 0;
  }
  .banner {
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 6px 10px;
  }
  .small {
    font-size: 12px;
  }
  pre {
    max-height: 240px;
    overflow: auto;
    font-size: 12px;
    background: var(--bg);
    padding: 8px;
    border-radius: 6px;
    white-space: pre-wrap;
    word-break: break-all;
  }
  summary {
    cursor: pointer;
  }
</style>
