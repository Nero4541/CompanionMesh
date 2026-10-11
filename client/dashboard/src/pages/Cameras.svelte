<script>
  import { api, apiImage } from "../lib/api.js";
  import { duration } from "../lib/format.js";

  let { onAuthError } = $props();

  const ID = /^[A-Za-z0-9_-]{1,64}$/;
  const BLANK = {
    id: "",
    url: "",
    enabled: true,
    username_env: "CAMERA_USERNAME",
    password_env: "CAMERA_PASSWORD",
    mode: "change",
    interval_s: 5,
    heartbeat_s: 300,
    analysis_interval_s: 1,
    session_id: null,
    enable_vision: true,
  };
  const STATES = {
    joined: { label: "in the conversation", kind: "ok" },
    waiting: { label: "waiting for a conversation", kind: "" },
    connecting: { label: "connecting", kind: "" },
    opening: { label: "opening the camera", kind: "" },
    unavailable: { label: "camera unreachable", kind: "warn" },
    disconnected: { label: "reconnecting", kind: "warn" },
    failed: { label: "stopped after an error", kind: "err" },
  };

  let data = $state(null);
  let loadError = $state(null);
  let editing = $state(null); // { index: number | null (new), form }
  let formErrors = $state({});
  let busy = $state(false);
  let message = $state(null);
  let preview = $state(null); // { url, label } | { error, label }
  let previewBusy = $state(null); // camera id or "form"

  const statusOf = (id) => data?.status.find((s) => s.id === id);

  // Env names typed into the form are checked too, before they are saved.
  function query() {
    const names = editing ? [editing.form.username_env, editing.form.password_env].filter(Boolean) : [];
    return names.length ? `?${names.map((n) => `env=${encodeURIComponent(n)}`).join("&")}` : "";
  }

  async function load() {
    try {
      data = await api(`/cameras${query()}`);
      loadError = null;
    } catch (err) {
      loadError = err;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  $effect(() => {
    load();
    const timer = setInterval(() => {
      if (!document.hidden && !busy) load();
    }, 3000);
    return () => clearInterval(timer);
  });

  $effect(() => () => {
    if (preview?.url) URL.revokeObjectURL(preview.url);
  });

  function showPreview(next) {
    if (preview?.url) URL.revokeObjectURL(preview.url);
    preview = next;
  }

  function errorText(err) {
    const list = err.detail?.errors;
    return Array.isArray(list) ? list.map((e) => `${e.path ? `${e.path}: ` : ""}${e.message}`).join("; ") : err.message;
  }

  // Saves the whole list; the server starts, stops or restarts what changed.
  async function saveList(list, done) {
    busy = true;
    message = null;
    formErrors = {};
    try {
      await api("/cameras", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cameras: list }),
      });
      await load();
      message = { kind: "ok", text: done };
      return true;
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      const list_ = err.detail?.errors;
      if (Array.isArray(list_)) {
        for (const e of list_) {
          const field = e.path.split(".")[2];
          if (field) formErrors[field] = e.message;
        }
      }
      message = { kind: "error", text: errorText(err) };
      return false;
    } finally {
      busy = false;
    }
  }

  const current = () => $state.snapshot(data.cameras);

  function edit(index) {
    message = null;
    formErrors = {};
    showPreview(null);
    editing = { index, form: index === null ? { ...BLANK } : { ...BLANK, ...$state.snapshot(data.cameras[index]) } };
    load(); // learn whether its env names are set
  }

  function clean(form) {
    const out = { ...form };
    for (const key of ["username_env", "password_env", "session_id"]) {
      if (typeof out[key] === "string" && out[key].trim() === "") out[key] = null;
    }
    for (const key of ["interval_s", "heartbeat_s", "analysis_interval_s"]) out[key] = Number(out[key]);
    return out;
  }

  const idError = $derived.by(() => {
    if (!editing) return null;
    const id = editing.form.id;
    if (!id) return null;
    if (!ID.test(id)) return "letters, digits, - and _ only";
    const clash = data.cameras.some((c, i) => i !== editing.index && c.id.toLowerCase() === id.toLowerCase());
    return clash ? "another camera has this id" : null;
  });

  async function saveForm() {
    const list = current();
    const camera = clean($state.snapshot(editing.form));
    if (editing.index === null) list.push(camera);
    else list[editing.index] = camera;
    if (await saveList(list, `Saved ${camera.id}.`)) editing = null;
  }

  async function toggle(index) {
    const list = current();
    list[index].enabled = !list[index].enabled;
    await saveList(list, `${list[index].id} ${list[index].enabled ? "started" : "stopped"}.`);
  }

  async function remove(index) {
    const list = current();
    const [gone] = list.splice(index, 1);
    if (!confirm(`Remove the camera ${gone.id}?`)) return;
    if (await saveList(list, `Removed ${gone.id}.`)) editing = null;
  }

  async function testForm() {
    previewBusy = "form";
    try {
      const camera = clean($state.snapshot(editing.form));
      const url = await apiImage("/cameras/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(camera),
      });
      showPreview({ url, label: "Test frame" });
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      showPreview({ error: errorText(err), label: "Test failed" });
    } finally {
      previewBusy = null;
    }
  }

  async function snapshot(id) {
    previewBusy = id;
    try {
      const url = await apiImage(`/cameras/${encodeURIComponent(id)}/snapshot`);
      showPreview({ url, label: `${id}, ${new Date().toLocaleTimeString()}` });
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      showPreview({ error: errorText(err), label: id });
    } finally {
      previewBusy = null;
    }
  }
</script>

<div class="head">
  <h2>Cameras</h2>
  <span class="muted">
    IP cameras and phones the server watches itself. Each joins the conversation as a camera when
    one is open.
  </span>
</div>

{#if message}
  <p class={message.kind === "ok" ? "banner ok" : "error"} role="status">{message.text}</p>
{/if}

{#if loadError && !data}
  <p class="error">{loadError.message}</p>
{:else if data}
  {#if !data.editable}
    <p class="note">Read-only: this server was not started from a config file.</p>
  {/if}

  <div class="layout">
    <div class="list">
      {#each data.cameras as camera, index (camera.id)}
        {@const st = statusOf(camera.id)}
        {@const info = st?.state ? (STATES[st.state] ?? { label: st.state, kind: "" }) : null}
        <div class="card camera" class:off={!camera.enabled}>
          <div class="top">
            <h3><code>{camera.id}</code></h3>
            {#if !camera.enabled}
              <span class="pill">stopped</span>
            {:else if info}
              <span class="pill {info.kind}" class:err-pill={info.kind === "err"}>{info.label}</span>
            {:else}
              <span class="pill">starting</span>
            {/if}
          </div>
          <p class="url"><code>{camera.url}</code></p>
          <p class="muted">
            {camera.mode === "change"
              ? `sends a frame when the view changes, at least every ${duration(camera.heartbeat_s)}`
              : camera.interval_s > 0
                ? `sends a frame every ${duration(camera.interval_s)}`
                : "sends frames only on request"}
          </p>
          {#if st?.running}
            <p class="muted">
              {st.frames_sent} sent · {st.frames_analyzed} looked at
              {#if st.last_frame_at}· last {new Date(st.last_frame_at).toLocaleTimeString()}{/if}
              {#if st.state === "joined"}· vision {st.vision_enabled ? "on" : "off"}{/if}
            </p>
          {/if}
          {#if st?.error && st.state !== "joined"}<p class="err small">{st.error}</p>{/if}
          <div class="actions">
            <button class="small" onclick={() => snapshot(camera.id)} disabled={!st?.running || previewBusy === camera.id}>
              {previewBusy === camera.id ? "Loading…" : "View"}
            </button>
            {#if data.editable}
              <button class="small" onclick={() => toggle(index)} disabled={busy}>
                {camera.enabled ? "Stop" : "Start"}
              </button>
              <button class="small" onclick={() => edit(index)} disabled={busy}>Edit</button>
            {/if}
          </div>
        </div>
      {:else}
        <p class="muted">No cameras yet.</p>
      {/each}
      {#if data.editable && !editing}
        <button onclick={() => edit(null)} disabled={busy}>Add camera</button>
      {/if}
    </div>

    <div class="side">
      {#if editing}
        <div class="card">
          <h3>{editing.index === null ? "New camera" : `Edit ${data.cameras[editing.index].id}`}</h3>
          <label class="field">
            <span>Id <span class="muted">(also its device id)</span></span>
            <input bind:value={editing.form.id} placeholder="e.g. livingroom" spellcheck="false" autocomplete="off" />
            {#if idError || formErrors.id}<span class="err small">{idError || formErrors.id}</span>{/if}
          </label>
          <label class="field">
            <span>URL <span class="muted">snapshot (…/shot.jpg), MJPEG (…/video) or rtsp://</span></span>
            <input bind:value={editing.form.url} placeholder="http://192.168.1.20:8080/shot.jpg" spellcheck="false" autocomplete="off" />
            {#if formErrors.url}<span class="err small">{formErrors.url}</span>{/if}
          </label>
          <div class="grid">
            <label class="field">
              <span>Username from <span class="muted">.env</span></span>
              <input bind:value={editing.form.username_env} placeholder="none" spellcheck="false" onchange={load} />
              {#if editing.form.username_env}
                {@const known = editing.form.username_env in data.secrets}
                <span class="small {data.secrets[editing.form.username_env] ? 'okc' : 'muted'}">
                  {!known ? "checking…" : data.secrets[editing.form.username_env] ? "set in .env" : "not set (fine without a login)"}
                </span>
              {/if}
            </label>
            <label class="field">
              <span>Password from <span class="muted">.env</span></span>
              <input bind:value={editing.form.password_env} placeholder="none" spellcheck="false" onchange={load} />
              {#if editing.form.password_env}
                {@const known = editing.form.password_env in data.secrets}
                <span class="small {data.secrets[editing.form.password_env] ? 'okc' : 'muted'}">
                  {!known ? "checking…" : data.secrets[editing.form.password_env] ? "set in .env" : "not set"}
                </span>
              {/if}
            </label>
          </div>
          <p class="muted small">
            Credentials never go into the URL or this page: put them in the server's .env under the
            names above. New variables need a restart to be read.
          </p>
          <div class="grid">
            <label class="field">
              <span>Mode</span>
              <select bind:value={editing.form.mode}>
                <option value="change">change: when the view changes</option>
                <option value="periodic">periodic: on a fixed interval</option>
              </select>
            </label>
            {#if editing.form.mode === "change"}
              <label class="field">
                <span>Routine frame every (s)</span>
                <input type="number" min="1" bind:value={editing.form.heartbeat_s} />
              </label>
              <label class="field">
                <span>Look for changes every (s)</span>
                <input type="number" min="0.1" step="0.1" bind:value={editing.form.analysis_interval_s} />
              </label>
            {:else}
              <label class="field">
                <span>Frame every (s) <span class="muted">0 = on request</span></span>
                <input type="number" min="0" bind:value={editing.form.interval_s} />
              </label>
            {/if}
          </div>
          <label class="field">
            <span>Session <span class="muted">(empty: the conversation started last)</span></span>
            <input bind:value={editing.form.session_id} placeholder="any" spellcheck="false" />
          </label>
          <label class="check">
            <input type="checkbox" bind:checked={editing.form.enable_vision} />
            Switch vision on when joining a conversation
          </label>
          <label class="check">
            <input type="checkbox" bind:checked={editing.form.enabled} />
            Running
          </label>
          <div class="actions">
            <button class="primary" onclick={saveForm} disabled={busy || !editing.form.id || !editing.form.url || idError}>
              {busy ? "Saving…" : "Save"}
            </button>
            <button onclick={testForm} disabled={!editing.form.url || previewBusy === "form"}>
              {previewBusy === "form" ? "Testing…" : "Test"}
            </button>
            <button onclick={() => (editing = null)} disabled={busy}>Cancel</button>
            {#if editing.index !== null}
              <button class="danger" onclick={() => remove(editing.index)} disabled={busy}>Remove</button>
            {/if}
          </div>
        </div>
      {/if}

      {#if preview}
        <figure class="card preview">
          {#if preview.url}
            <img src={preview.url} alt={`Camera view: ${preview.label}`} />
          {:else}
            <p class="err">{preview.error}</p>
          {/if}
          <figcaption class="muted">
            {preview.label}
            <button class="small" onclick={() => showPreview(null)}>Close</button>
          </figcaption>
        </figure>
      {/if}
    </div>
  </div>
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  .list,
  .side {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
  }
  .camera p {
    margin: 4px 0;
  }
  .camera.off {
    opacity: 0.7;
  }
  .top {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .top h3 {
    margin: 0;
  }
  .url {
    overflow-wrap: anywhere;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-top: 8px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0 12px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 10px;
    min-width: 0;
  }
  .field > span:first-child {
    font-size: 14px;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 8px;
    font-size: 14px;
  }
  .preview {
    margin: 0;
  }
  .preview img {
    width: 100%;
    border-radius: 6px;
    display: block;
  }
  figcaption {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 6px;
  }
  .danger {
    color: var(--err);
    border-color: var(--err);
    margin-left: auto;
  }
  .err,
  .err-pill {
    color: var(--err);
    border-color: var(--err);
  }
  .okc {
    color: var(--ok);
  }
  .small {
    font-size: 12px;
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
  .banner.ok {
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  @media (max-width: 820px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
