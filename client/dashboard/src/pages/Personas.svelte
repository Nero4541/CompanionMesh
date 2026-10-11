<script>
  import { api } from "../lib/api.js";
  import { equal } from "../lib/schema.js";

  let { onAuthError } = $props();

  const NAME = /^[A-Za-z0-9_-]{1,64}$/;
  const BLANK = { display_name: null, language: null, voice: null, system_prompt: "" };

  let listing = $state(null);
  let listError = $state(null);
  let selected = $state(null); // name
  let original = $state(null); // persona as loaded
  let form = $state(null); // persona being edited
  let creating = $state(false); // form is a new, unsaved persona
  let newName = $state("");
  let busy = $state(false);
  let message = $state(null); // { kind: "ok" | "error", text }

  const current = $derived(listing?.personas.find((p) => p.name === selected) ?? null);
  const readOnly = $derived(!creating && Boolean(current?.builtin));
  const dirty = $derived(creating || (form && original && !equal(form, original)));
  const nameError = $derived(
    creating && newName && !NAME.test(newName)
      ? "letters, digits, - and _ only"
      : creating && listing?.personas.some((p) => p.name.toLowerCase() === newName.toLowerCase())
        ? "a persona with this name exists"
        : null,
  );

  function fail(err) {
    if (err.isAuth) onAuthError?.(err);
    const errors = err.detail?.errors;
    message = { kind: "error", text: Array.isArray(errors) ? errors.map((e) => `${e.path ? `${e.path}: ` : ""}${e.message}`).join("; ") : err.message };
  }

  async function loadList() {
    try {
      listing = await api("/personas");
      listError = null;
    } catch (err) {
      listError = err;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function open(name) {
    if (dirty && !confirm("Discard your unsaved changes?")) return;
    message = null;
    try {
      const data = await api(`/personas/${encodeURIComponent(name)}`);
      selected = name;
      creating = false;
      original = { ...BLANK, ...data.persona };
      form = $state.snapshot(original);
    } catch (err) {
      fail(err);
    }
  }

  function startNew(copyFrom = null) {
    if (!copyFrom && dirty && !confirm("Discard your unsaved changes?")) return;
    message = null;
    creating = true;
    newName = copyFrom ? `${copyFrom}-copy` : "";
    form = copyFrom ? $state.snapshot(form) : { ...BLANK };
    original = null;
  }

  const clean = (v) => (typeof v === "string" && v.trim() === "" ? null : v);

  async function save() {
    const name = creating ? newName.trim() : selected;
    busy = true;
    message = null;
    try {
      const payload = {
        display_name: clean(form.display_name),
        language: clean(form.language),
        voice: clean(form.voice),
        system_prompt: form.system_prompt,
      };
      const res = await api(`/personas/${encodeURIComponent(name)}${creating ? "?create=true" : ""}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await loadList();
      creating = false;
      selected = name;
      original = { ...payload };
      form = $state.snapshot(original);
      message = { kind: "ok", text: res.applied ? "Saved; the companion uses it from the next reply." : "Saved." };
    } catch (err) {
      fail(err);
    } finally {
      busy = false;
    }
  }

  async function activate() {
    busy = true;
    message = null;
    try {
      await api(`/personas/${encodeURIComponent(selected)}/activate`, { method: "POST" });
      await loadList();
      message = { kind: "ok", text: `Now using ${selected}, from the next reply on.` };
    } catch (err) {
      fail(err);
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (!confirm(`Delete the persona ${selected}? This removes its file.`)) return;
    busy = true;
    message = null;
    try {
      await api(`/personas/${encodeURIComponent(selected)}`, { method: "DELETE" });
      const gone = selected;
      selected = null;
      form = original = null;
      await loadList();
      message = { kind: "ok", text: `Deleted ${gone}.` };
    } catch (err) {
      fail(err);
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    loadList().then(() => {
      if (listing && !selected) open(listing.active);
    });
  });

  $effect(() => {
    const warn = (e) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  });
</script>

<div class="head">
  <h2>Personas</h2>
  {#if listing}<span class="muted">Files in <code>{listing.directory}</code></span>{/if}
</div>

{#if listError && !listing}
  <p class="error">{listError.message}</p>
{:else if listing}
  <div class="layout">
    <aside>
      <ul>
        {#each listing.personas as p (p.name)}
          <li>
            <button class="item" class:selected={!creating && p.name === selected} onclick={() => open(p.name)}>
              <span class="title">
                <code>{p.name}</code>
                {#if p.active}<span class="pill ok">in use</span>{/if}
                {#if p.builtin}<span class="pill">built-in</span>{/if}
              </span>
              {#if p.error}
                <span class="err small">cannot be read</span>
              {:else}
                <span class="muted">{p.display_name ?? "–"}{p.language ? ` · ${p.language}` : ""}</span>
              {/if}
            </button>
          </li>
        {/each}
      </ul>
      <button class="new" onclick={() => startNew()} disabled={busy}>New persona</button>
    </aside>

    <div class="editor">
      {#if message}
        <p class={message.kind === "ok" ? "banner ok" : "error"} role="status">{message.text}</p>
      {/if}

      {#if form}
        <div class="card">
          {#if creating}
            <label class="field">
              <span>Name <span class="muted">(file name: letters, digits, - and _)</span></span>
              <input bind:value={newName} placeholder="e.g. cat" autocomplete="off" spellcheck="false" />
              {#if nameError}<span class="err small">{nameError}</span>{/if}
            </label>
          {:else}
            <h3>
              <code>{selected}</code>
              {#if current?.active}<span class="pill ok">in use</span>{/if}
            </h3>
            {#if readOnly}
              <p class="muted">
                Ships with the project, so it is not edited here (updates would overwrite it).
                Save a copy to make your own.
              </p>
            {/if}
          {/if}

          <div class="grid">
            <label class="field">
              <span>Display name</span>
              <input bind:value={form.display_name} readonly={readOnly} placeholder="none" />
            </label>
            <label class="field">
              <span>Language <span class="muted">(e.g. ja, yue, en)</span></span>
              <input bind:value={form.language} readonly={readOnly} placeholder="none" spellcheck="false" />
            </label>
            <label class="field">
              <span>Voice <span class="muted">(TTS voice id)</span></span>
              <input bind:value={form.voice} readonly={readOnly} placeholder="empty: uses tts.voice" spellcheck="false" />
            </label>
          </div>

          <label class="field">
            <span>
              System prompt
              <span class="muted">
                — speaking-style rules added to the agent's own character; with
                <code>agent.provider: direct</code> this is the whole prompt
              </span>
            </span>
            <textarea bind:value={form.system_prompt} readonly={readOnly} rows="16"></textarea>
            {#if !readOnly && !form.system_prompt.trim()}<span class="err small">required</span>{/if}
          </label>

          <div class="actions">
            {#if creating}
              <button class="primary" onclick={save} disabled={busy || !newName || nameError || !form.system_prompt.trim()}>
                Create
              </button>
              <button onclick={() => (selected ? open(selected) : ((creating = false), (form = null)))} disabled={busy}>
                Cancel
              </button>
            {:else}
              {#if !readOnly}
                <button class="primary" onclick={save} disabled={busy || !dirty || !form.system_prompt.trim()}>
                  Save
                </button>
                {#if dirty}
                  <button onclick={() => (form = $state.snapshot(original))} disabled={busy}>
                    Discard
                  </button>
                {/if}
              {/if}
              <button onclick={() => startNew(selected)} disabled={busy}>Save a copy…</button>
              {#if !current?.active}
                <button
                  onclick={activate}
                  disabled={busy || dirty || !listing.can_activate}
                  title={dirty ? "save first" : !listing.can_activate ? "this server was not started from a config file" : ""}
                >
                  Use this persona
                </button>
                {#if !readOnly}
                  <button class="danger" onclick={remove} disabled={busy}>Delete</button>
                {/if}
              {/if}
            {/if}
          </div>
        </div>
      {:else}
        <p class="muted">Pick a persona, or create a new one.</p>
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
  aside ul {
    list-style: none;
    margin: 0 0 8px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .item {
    width: 100%;
    text-align: left;
    display: flex;
    flex-direction: column;
    gap: 2px;
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
  .new {
    width: 100%;
  }
  .editor {
    min-width: 0;
  }
  .card h3 {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-bottom: 12px;
    min-width: 0;
  }
  .field > span:first-child {
    font-size: 14px;
  }
  textarea {
    width: 100%;
    resize: vertical;
    line-height: 1.6;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .danger {
    color: var(--err);
    border-color: var(--err);
    margin-left: auto;
  }
  .err {
    color: var(--err);
  }
  .small {
    font-size: 12px;
  }
  .banner.ok {
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  @media (max-width: 720px) {
    .layout {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
