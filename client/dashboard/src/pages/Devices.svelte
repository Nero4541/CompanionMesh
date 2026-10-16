<script>
  import { api } from "../lib/api.js";
  import DeviceCamera from "../lib/DeviceCamera.svelte";
  import DeviceDiagnostics from "../lib/DeviceDiagnostics.svelte";
  import DeviceLog from "../lib/DeviceLog.svelte";
  import DeviceMaintain from "../lib/DeviceMaintain.svelte";
  import { DEVICE_EVENTS, onEvents } from "../lib/live.js";
  import { duration, value as show } from "../lib/format.js";
  import { changedPaths, equal, getPath, setPath } from "../lib/schema.js";

  let { onAuthError } = $props();

  const GROUPS = [
    { key: "", label: "Device" },
    { key: "audio", label: "Audio" },
    { key: "camera", label: "Camera" },
  ];

  let devices = $state(null);
  let profiles = $state([]);
  let loadError = $state(null);
  let selectedId = $state(null);
  let draft = $state(null); // editable copy of the selected device's config
  let base = $state(null); // the config the draft started from
  let texts = $state({}); // path -> typed text for number fields
  let errors = $state({});
  let profileName = $state("");
  let busy = $state(false);
  let message = $state(null);
  let waiting = $state(null); // { id, expect } while a device restarts
  let tab = $state("settings"); // settings | log | camera | diagnostics
  let withLogs = $state([]); // device ids the server has log lines of

  // Devices that are not connected now but left their logs here.
  const offline = $derived(withLogs.filter((id) => !devices?.some((d) => d.device_id === id)));

  const selected = $derived(devices?.find((d) => d.device_id === selectedId) ?? null);
  const settings = $derived(selected?.settings ?? null);
  const changes = $derived(draft && base ? changedPaths($state.snapshot(base), $state.snapshot(draft)) : []);
  const invalid = $derived(Object.keys(errors).length);
  const profile = $derived(profiles.find((p) => p.name === profileName) ?? null);

  function fields(group) {
    if (!settings) return [];
    return Object.entries(settings.schema)
      .filter(([path]) => (group ? path.startsWith(`${group}.`) : !path.includes(".")))
      .map(([path, info]) => ({ path, name: group ? path.slice(group.length + 1) : path, ...info }));
  }

  function select(id) {
    if (changes.length && !confirm("Discard your unsaved changes?")) return;
    selectedId = id;
    if (!devices?.some((d) => d.device_id === id)) tab = "log";
    message = null;
    profileName = "";
    reset();
  }

  function reset() {
    const d = devices?.find((x) => x.device_id === selectedId);
    base = d?.settings ? $state.snapshot(d.settings.config) : null;
    draft = base ? $state.snapshot(base) : null;
    texts = {};
    errors = {};
  }

  async function load() {
    try {
      const data = await api("/devices");
      devices = data.devices;
      api("/device-logs")
        .then((r) => (withLogs = r.devices))
        .catch(() => {});
      loadError = null;
      if (!selectedId && devices.length) select((devices.find((d) => d.configurable) ?? devices[0]).device_id);
      if (waiting) {
        const back = devices.find((d) => d.device_id === waiting.id && d.settings);
        if (back && changedPaths(waiting.expect, back.settings.config).length === 0) {
          message = { kind: "ok", text: `${waiting.id} is back with the new settings.` };
          waiting = null;
          reset();
        }
      } else if (!changes.length) {
        reset(); // follow changes made elsewhere while nothing is being edited
      }
    } catch (err) {
      loadError = err;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  $effect(() => {
    load();
    api("/edge-profiles")
      .then((r) => (profiles = r.profiles))
      .catch(() => {});
    const stop = onEvents(DEVICE_EVENTS, () => {
      if (!busy) load();
    });
    const timer = setInterval(() => {
      if (!document.hidden && !busy) load();
    }, 5000);
    return () => {
      stop();
      clearInterval(timer);
    };
  });

  function setNumber(path, type, text) {
    texts[path] = text;
    const n = Number(text.trim());
    if (text.trim() === "" || !Number.isFinite(n)) errors[path] = "a number";
    else if (type === "int" && !Number.isInteger(n)) errors[path] = "a whole number";
    else if (n < 0) errors[path] = "0 or more";
    else {
      delete errors[path];
      setPath(draft, path, n);
    }
  }

  function set(path, v) {
    delete errors[path];
    setPath(draft, path, v);
  }

  // The settings a profile would change on this device (its read-only ones never).
  const profileChanges = $derived.by(() => {
    if (!profile || !draft || !settings) return [];
    const out = [];
    for (const [path, info] of Object.entries(settings.schema)) {
      if (!info.editable) continue;
      const v = getPath(profile.config, path);
      if (v !== undefined && !equal(v, getPath(draft, path))) out.push({ path, from: getPath(draft, path), to: v });
    }
    return out;
  });

  function applyProfile() {
    for (const c of profileChanges) set(c.path, c.to);
    texts = {};
    message = { kind: "ok", text: `Applied the ${profile.name} profile to the form. Check it, then save it to the device.` };
    profileName = "";
  }

  async function save() {
    busy = true;
    message = null;
    try {
      const config = $state.snapshot(draft);
      const res = await api(`/devices/${encodeURIComponent(selectedId)}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config }),
      });
      if (res.restarting) {
        waiting = { id: selectedId, expect: config };
        message = { kind: "info", text: `${selectedId} saved the settings and is restarting with them…` };
      } else {
        message = { kind: "ok", text: "Nothing to change." };
      }
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      const list = err.detail?.errors;
      if (err.status === 422 && Array.isArray(list)) {
        for (const e of list) if (e.path) errors[e.path] = e.message;
        message = { kind: "error", text: `The device refused: ${list.map((e) => `${e.path ? `${e.path}: ` : ""}${e.message}`).join("; ")}` };
      } else {
        message = { kind: "error", text: err.message };
      }
    } finally {
      busy = false;
    }
  }
</script>

<div class="head">
  <h2>Devices</h2>
  <span class="muted">Connected devices, and boards that left their log here. Settings need companion-edge 1.1, the rest 1.2.</span>
</div>

{#if loadError && !devices}
  <p class="error">{loadError.message}</p>
{:else if devices}
  <div class="layout">
    <aside>
      {#each devices as d (d.session_id + d.device_id)}
        <button class="item" class:selected={d.device_id === selectedId} onclick={() => select(d.device_id)}>
          <span class="title">
            <code>{d.device_id ?? "(no id)"}</code>
            {#if d.configurable}<span class="pill ok">settings</span>{/if}
          </span>
          <span class="muted">{d.roles.join(", ")} · {duration(d.connected_s)}{d.rtt_ms != null ? ` · ${d.rtt_ms} ms` : ""}</span>
        </button>
      {:else}
        <p class="muted">No device is connected.</p>
      {/each}
      {#if waiting && !devices.some((d) => d.device_id === waiting.id)}
        <p class="muted">{waiting.id}: restarting…</p>
      {/if}
      {#if offline.length}
        <p class="muted group">Not connected (log kept)</p>
        {#each offline as id (id)}
          <button class="item" class:selected={id === selectedId} onclick={() => select(id)}>
            <span class="title"><code>{id}</code></span>
          </button>
        {/each}
      {/if}
    </aside>

    <div class="main">
      {#if message}
        <p class={message.kind === "error" ? "error" : `banner ${message.kind}`} role="status">{message.text}</p>
      {/if}

      {#if selectedId}
        <div class="tabs" role="tablist">
          {#each [["settings", "Status and settings"], ["log", "Log"], ["camera", "Camera"], ["diagnostics", "Diagnostics"], ["maintain", "Maintenance"]] as [id, label]}
            <button
              role="tab"
              class:active={tab === id}
              aria-selected={tab === id}
              disabled={!selected && id !== "log"}
              onclick={() => (tab = id)}>{label}</button
            >
          {/each}
        </div>
      {/if}

      {#if selectedId && tab === "log"}
        <DeviceLog deviceId={selectedId} />
      {:else if selected && tab === "camera"}
        <DeviceCamera deviceId={selectedId} />
      {:else if selected && tab === "diagnostics"}
        <DeviceDiagnostics deviceId={selectedId} />
      {:else if selected && tab === "maintain"}
        <DeviceMaintain device={selected} />
      {:else if selected}
        {#if selected.status && Object.keys(selected.status).length}
          <details class="card status">
            <summary>Last status <span class="muted">{duration(selected.status_age_s)} ago</span></summary>
            <dl>
              {#each Object.entries(selected.status) as [k, v]}
                <dt>{k}</dt><dd><code>{show(v)}</code></dd>
              {/each}
            </dl>
          </details>
        {/if}

        {#if !settings}
          <p class="note">
            This device does not offer remote settings. companion-edge 1.1 and newer do; browsers and other
            clients are set up on the device itself.
          </p>
        {:else if draft}
          {#if !settings.editable}
            <p class="note">
              Shown read-only: the device was started without <code>--config</code>, so it has no file to save to.
            </p>
          {:else}
            <div class="card profiles">
              <label class="row">
                <span>Start from a profile</span>
                <select bind:value={profileName}>
                  <option value="">choose…</option>
                  {#each profiles as p}<option value={p.name}>{p.title}</option>{/each}
                </select>
              </label>
              {#if profile}
                <p class="notes">{profile.notes}</p>
                {#if profileChanges.length}
                  <ul class="changes">
                    {#each profileChanges as c}
                      <li><code>{c.path}</code> {show(c.from)} → <strong>{show(c.to)}</strong></li>
                    {/each}
                  </ul>
                  <button onclick={applyProfile}>Apply to the form</button>
                {:else}
                  <p class="muted">This device already matches it.</p>
                {/if}
              {/if}
            </div>
          {/if}

          {#each GROUPS as g}
            {@const list = fields(g.key)}
            {#if list.length}
              <section class="card">
                <h3>{g.label}</h3>
                {#each list as f (f.path)}
                  {@const v = getPath(draft, f.path)}
                  {@const ro = !f.editable || !settings.editable || busy || waiting}
                  <div class="field" class:modified={!equal(v, getPath(base, f.path))} class:invalid={errors[f.path]}>
                    <label for={`d-${f.path}`}><code>{f.name}</code></label>
                    <div class="control">
                      {#if f.type === "bool"}
                        <input id={`d-${f.path}`} type="checkbox" checked={v} disabled={ro} onchange={(e) => set(f.path, e.currentTarget.checked)} />
                      {:else if f.choices}
                        <select id={`d-${f.path}`} value={v} disabled={ro} onchange={(e) => set(f.path, f.type === "int" || f.type === "float" ? Number(e.currentTarget.value) : e.currentTarget.value)}>
                          {#each f.choices as option}<option value={option}>{option}</option>{/each}
                        </select>
                      {:else if f.type === "int" || f.type === "float"}
                        <input
                          id={`d-${f.path}`}
                          type="text"
                          inputmode="decimal"
                          value={texts[f.path] ?? String(v)}
                          readonly={ro}
                          oninput={(e) => setNumber(f.path, f.type, e.currentTarget.value)}
                        />
                      {:else}
                        <input id={`d-${f.path}`} type="text" value={v} readonly={ro} spellcheck="false" oninput={(e) => set(f.path, e.currentTarget.value)} />
                      {/if}
                      {#if !f.editable}<span class="muted small">change on the device</span>{/if}
                    </div>
                    {#if errors[f.path]}<span class="err small">{errors[f.path]}</span>{/if}
                  </div>
                {/each}
              </section>
            {/if}
          {/each}

          {#if settings.editable && (changes.length || invalid)}
            <div class="savebar">
              <span>
                {#if invalid}<span class="err">{invalid} invalid · </span>{/if}
                {changes.length} change{changes.length === 1 ? "" : "s"} · the device restarts to use them
              </span>
              <span class="actions">
                <button onclick={reset} disabled={busy}>Discard</button>
                <button class="primary" onclick={save} disabled={busy || invalid > 0 || !changes.length || waiting}>
                  {busy ? "Sending…" : "Save to device"}
                </button>
              </span>
            </div>
          {/if}
        {/if}
      {:else if devices.length}
        <p class="muted">Pick a device.</p>
      {/if}
    </div>
  </div>
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .layout {
    display: grid;
    grid-template-columns: 240px minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  aside {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .item {
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .group {
    margin: 8px 0 0;
  }
  .tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 6px;
  }
  .tabs button.active {
    border-color: var(--accent);
    box-shadow: inset 0 -3px 0 var(--accent);
  }
  .item.selected {
    border-color: var(--accent);
    box-shadow: inset 3px 0 0 var(--accent);
  }
  .title {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-wrap: wrap;
  }
  .main {
    display: flex;
    flex-direction: column;
    gap: 12px;
    min-width: 0;
    padding-bottom: 64px;
  }
  .main > p {
    margin: 0;
  }
  .card h3 {
    margin: 0 0 4px;
  }
  .status dl {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 4px 12px;
    margin: 8px 0 0;
  }
  .status dt {
    color: var(--muted);
  }
  .status dd {
    margin: 0;
    overflow-wrap: anywhere;
  }
  summary {
    cursor: pointer;
  }
  .row {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
  }
  .notes {
    white-space: pre-wrap;
    font-size: 13px;
    color: var(--muted);
    margin: 8px 0;
  }
  .changes {
    margin: 8px 0;
    padding-left: 18px;
    font-size: 14px;
    overflow-wrap: anywhere;
  }
  .field {
    display: grid;
    grid-template-columns: minmax(140px, 200px) minmax(0, 1fr);
    gap: 4px 12px;
    align-items: center;
    padding: 6px 0 6px 8px;
    margin-left: -8px;
    border-top: 1px solid var(--line);
    border-left: 3px solid transparent;
  }
  .field.modified {
    border-left-color: var(--accent);
  }
  .field.invalid {
    border-left-color: var(--err);
  }
  .field .err {
    grid-column: 2;
  }
  .control {
    display: flex;
    gap: 8px;
    align-items: center;
    min-width: 0;
  }
  .control input[type="text"],
  .control select {
    width: 100%;
    max-width: 360px;
  }
  .savebar {
    position: sticky;
    bottom: 12px;
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: center;
    gap: 8px 16px;
    background: var(--panel);
    border: 1px solid var(--accent);
    border-radius: 10px;
    padding: 10px 14px;
    box-shadow: 0 4px 16px rgb(0 0 0 / 12%);
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
  .banner {
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  .banner.ok {
    border-color: var(--ok);
  }
  .err {
    color: var(--err);
  }
  .small {
    font-size: 12px;
  }
  @media (max-width: 720px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
    .field {
      grid-template-columns: minmax(0, 1fr);
    }
    .field .err {
      grid-column: 1;
    }
  }
</style>
