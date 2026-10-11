<script>
  import Section from "../lib/Section.svelte";
  import Setting from "../lib/Setting.svelte";
  import { api } from "../lib/api.js";
  import { nextOrigin, waitForServer } from "../lib/restart.js";
  import { changedPaths, describe, sections, setPath } from "../lib/schema.js";

  let { onAuthError } = $props();

  let data = $state(null); // GET /config
  let loadError = $state(null);
  let draft = $state(null);
  let errors = $state({}); // path -> message (typed text or the server's validation)
  let formKey = $state(0); // bumped to reset every row's typed text
  let saving = $state(false);
  let result = $state(null); // last save: { applied, pending_restart, file }
  let saveError = $state(null);
  let restarting = $state(null); // { origin, sameOrigin, failed }

  const saved = $derived(data ? (data.saved ?? data.running) : null);
  const layout = $derived(data ? sections(data.schema) : null);
  const pending = $derived(new Set(data?.pending_restart ?? []));
  const commandLine = $derived(new Set(data?.command_line ?? []));
  const changes = $derived(draft && saved ? changedPaths($state.snapshot(saved), $state.snapshot(draft)) : []);
  const invalid = $derived(Object.keys(errors).length);
  const missingSecrets = $derived(
    data ? Object.entries(data.secrets).filter(([, set]) => !set).map(([n]) => n) : [],
  );

  const ctx = {
    get draft() {
      return draft;
    },
    get saved() {
      return saved;
    },
    get schema() {
      return data.schema;
    },
    get restartRequired() {
      return data.restart_required;
    },
    get secrets() {
      return data.secrets;
    },
    get pending() {
      return pending;
    },
    get commandLine() {
      return commandLine;
    },
    get errors() {
      return errors;
    },
    get editable() {
      return data.editable && !saving && !restarting;
    },
    set(path, value) {
      setPath(draft, path, value);
      if (errors[path]) delete errors[path];
    },
    setError(path, message) {
      if (message) errors[path] = message;
      else delete errors[path];
    },
  };

  async function load() {
    try {
      data = await api("/config");
      draft = structuredClone($state.snapshot(data.saved ?? data.running));
      errors = {};
      formKey += 1;
      loadError = null;
    } catch (err) {
      loadError = err;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  $effect(() => {
    load();
  });

  // Leaving with unsaved changes asks first.
  $effect(() => {
    const warn = (e) => {
      if (changes.length) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  });

  function discard() {
    draft = structuredClone($state.snapshot(saved));
    errors = {};
    formKey += 1;
  }

  async function save() {
    saving = true;
    saveError = null;
    result = null;
    try {
      result = await api("/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: $state.snapshot(draft) }),
      });
      await load();
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      const list = err.detail?.errors;
      if (err.status === 422 && Array.isArray(list)) {
        for (const e of list) if (e.path) errors[e.path] = e.message;
        const general = list.filter((e) => !e.path).map((e) => e.message);
        saveError = general.length
          ? general.join("; ")
          : `${list.length} setting${list.length > 1 ? "s" : ""} could not be saved (marked below).`;
      } else {
        saveError = err.message;
      }
    } finally {
      saving = false;
    }
  }

  async function restart() {
    if (!confirm("Restart the server now? Open conversations are interrupted; devices reconnect.")) return;
    try {
      const next = await api("/restart", { method: "POST" });
      const origin = nextOrigin(next);
      restarting = { origin, sameOrigin: origin === window.location.origin, failed: false };
      const back = await waitForServer(origin);
      if (!back) {
        restarting = { ...restarting, failed: true };
      } else if (restarting.sameOrigin) {
        restarting = null;
        result = null;
        await load();
      }
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      saveError = err.message;
    }
  }

  function jump(event, id) {
    event.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  }
</script>

<div class="head">
  <h2>Configuration</h2>
  {#if data?.files}
    <span class="muted">
      Saving writes <code>{data.files.local}</code>; the previous version is kept next to it.
    </span>
  {/if}
</div>

{#if restarting}
  <div class="banner" role="status">
    {#if restarting.failed}
      The server has not come back yet. Check its console; it may have stopped.
      <a href={`${restarting.origin}/admin/`}>Try {restarting.origin}/admin/</a>
    {:else if restarting.sameOrigin}
      Restarting… this page reloads when the server is back.
    {:else}
      Restarting at a new address: <a href={`${restarting.origin}/admin/`}>{restarting.origin}/admin/</a>
    {/if}
  </div>
{/if}

{#if loadError && !data}
  <p class="error">{loadError.message}</p>
{:else if data && layout}
  {#if data.error}
    <p class="error">The config files cannot be read, so nothing can be saved: {data.error}</p>
  {:else if !data.editable}
    <p class="note">
      Read-only: this server was not started from a config file. Showing the running settings.
    </p>
  {/if}

  {#if pending.size && !restarting}
    <div class="banner warn" role="status">
      <span>
        {pending.size} saved setting{pending.size > 1 ? "s wait" : " waits"} for a restart:
        {#each [...pending] as p, i}<code>{p}</code>{i < pending.size - 1 ? ", " : ""}{/each}.
      </span>
      {#if data.can_restart}
        <button onclick={restart} disabled={changes.length > 0} title={changes.length ? "save or discard your changes first" : ""}>
          Restart now
        </button>
      {:else}
        <span class="muted">Restart the server where it runs.</span>
      {/if}
    </div>
  {/if}

  {#if result && !restarting}
    <div class="banner ok" role="status">
      Saved.
      {#if result.applied.length}
        Applied now: {#each result.applied as p, i}<code>{p}</code>{i < result.applied.length - 1 ? ", " : ""}{/each}.
      {/if}
      {#if !result.applied.length && !result.pending_restart.length}Nothing changed.{/if}
    </div>
  {/if}

  {#if missingSecrets.length}
    <p class="note">
      Not set in .env: {#each missingSecrets as name, i}<code>{name}</code>{i < missingSecrets.length - 1 ? ", " : ""}{/each}.
      That is fine for providers you do not use.
    </p>
  {/if}

  <div class="layout">
    <nav aria-label="Sections">
      {#each layout.sections as s}
        <a href="#/config" onclick={(e) => jump(e, `s-${s.name}`)}>{s.name}</a>
      {/each}
      {#if layout.loose.length}
        <a href="#/config" onclick={(e) => jump(e, "s-general")}>general</a>
      {/if}
    </nav>

    <div class="sections">
      {#key formKey}
        {#each layout.sections as s (s.name)}
          <Section path={s.name} desc={s.desc} {ctx} />
        {/each}
        {#if layout.loose.length}
          <section class="general" id="s-general">
            <h3>general</h3>
            {#each layout.loose as name}
              <Setting
                path={name}
                {name}
                desc={describe(data.schema, data.schema.properties[name])}
                {ctx}
              />
            {/each}
          </section>
        {/if}
      {/key}
    </div>
  </div>

  {#if data.editable && (changes.length || invalid || saveError)}
    <div class="savebar" role="region" aria-label="Unsaved changes">
      <span class="summary">
        {#if saveError}<span class="err">{saveError}</span>{/if}
        {#if invalid}
          <span class="err">{invalid} invalid value{invalid > 1 ? "s" : ""}</span>
        {/if}
        {changes.length} unsaved change{changes.length === 1 ? "" : "s"}
        {#if changes.some((p) => data.restart_required[p] ?? true)}
          <span class="muted">(some take effect after a restart)</span>
        {/if}
      </span>
      <span class="actions">
        <button onclick={discard} disabled={saving}>Discard</button>
        <button class="primary" onclick={save} disabled={saving || invalid > 0 || !changes.length}>
          {saving ? "Saving…" : "Save"}
        </button>
      </span>
    </div>
  {/if}
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .head {
    margin-bottom: 12px;
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
  .banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
    border: 1px solid var(--accent);
    border-radius: 8px;
    padding: 8px 12px;
    margin-bottom: 12px;
    font-size: 14px;
    overflow-wrap: anywhere;
  }
  .banner.warn {
    border-color: var(--warn);
    background: var(--warn-bg);
  }
  .banner.ok {
    border-color: var(--ok);
  }
  .layout {
    display: grid;
    grid-template-columns: 160px minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }
  nav {
    position: sticky;
    top: 12px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  nav a {
    color: var(--fg);
    text-decoration: none;
    padding: 4px 8px;
    border-radius: 6px;
    font-size: 14px;
  }
  nav a:hover {
    background: var(--panel);
  }
  .sections {
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
    padding-bottom: 72px;
  }
  .general {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 16px;
  }
  .general h3 {
    margin: 0 0 4px;
    font-size: 15px;
  }
  .savebar {
    position: sticky;
    bottom: 12px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 8px 16px;
    background: var(--panel);
    border: 1px solid var(--accent);
    border-radius: 10px;
    padding: 10px 14px;
    box-shadow: 0 4px 16px rgb(0 0 0 / 12%);
  }
  .summary {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    align-items: baseline;
  }
  .actions {
    display: flex;
    gap: 8px;
  }
  .err {
    color: var(--err);
  }
  @media (max-width: 720px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
    nav {
      position: static;
      flex-direction: row;
      flex-wrap: wrap;
    }
  }
</style>
