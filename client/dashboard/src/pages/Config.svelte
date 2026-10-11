<script>
  import Section from "../lib/Section.svelte";
  import Setting from "../lib/Setting.svelte";
  import { poll } from "../lib/poll.svelte.js";
  import { describe, sections } from "../lib/schema.js";

  let { onAuthError } = $props();

  const { state: res } = poll("/config", 0, (err) => onAuthError?.(err));
  const layout = $derived(res.data ? sections(res.data.schema) : null);
  const missingSecrets = $derived(
    res.data ? Object.entries(res.data.secrets).filter(([, set]) => !set).map(([n]) => n) : [],
  );
</script>

<div class="head">
  <h2>Configuration</h2>
  <p class="muted">
    Read-only for now. <span class="pill live">live</span> settings will apply to the running
    server; <span class="pill">restart</span> settings need a restart. A dot marks a value that
    differs from the built-in default.
  </p>
</div>

{#if res.error && !res.data}
  <p class="error">{res.error.message}</p>
{:else if layout}
  {#if missingSecrets.length}
    <p class="note">
      Not set in .env: {#each missingSecrets as name, i}<code>{name}</code>{i <
        missingSecrets.length - 1
          ? ", "
          : ""}{/each}. That is fine for providers you do not use.
    </p>
  {/if}

  <div class="layout">
    <nav aria-label="Sections">
      {#each layout.sections as s}
        <a href="#/config" onclick={(e) => {
          e.preventDefault();
          document.getElementById(`s-${s.name}`)?.scrollIntoView({ behavior: "smooth" });
        }}>{s.name}</a>
      {/each}
      {#if layout.loose.length}<a href="#/config" onclick={(e) => {
          e.preventDefault();
          document.getElementById("s-general")?.scrollIntoView({ behavior: "smooth" });
        }}>general</a>{/if}
    </nav>

    <div class="sections">
      {#each layout.sections as s (s.name)}
        <Section
          root={res.data.schema}
          path={s.name}
          desc={s.desc}
          value={res.data.effective[s.name]}
          restartRequired={res.data.restart_required}
          secrets={res.data.secrets}
        />
      {/each}
      {#if layout.loose.length}
        <section class="general" id="s-general">
          <h3>general</h3>
          {#each layout.loose as name}
            <Setting
              path={name}
              {name}
              desc={describe(res.data.schema, res.data.schema.properties[name])}
              value={res.data.effective[name]}
              restart={res.data.restart_required[name] ?? true}
              secrets={res.data.secrets}
            />
          {/each}
        </section>
      {/if}
    </div>
  </div>
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .head p {
    margin: 4px 0 12px;
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
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
