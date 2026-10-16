<script>
  import { api } from "../lib/api.js";
  import Markdown from "../lib/Markdown.svelte";

  let { onAuthError } = $props();

  let info = $state(null);
  let latest = $state(null);
  let checking = $state(false);
  let error = $state(null);

  async function load() {
    try {
      info = await api("/updates");
    } catch (err) {
      error = err.message;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function check(refresh = false) {
    checking = true;
    try {
      latest = await api(`/updates/latest${refresh ? "?refresh=true" : ""}`);
    } catch (err) {
      latest = { error: err.message };
    } finally {
      checking = false;
    }
  }

  $effect(() => {
    load();
    check();
  });

  const when = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
  const windows = navigator.userAgent.includes("Windows");
</script>

<div class="head">
  <h2>Updates</h2>
  <span class="muted">This server's version, newer releases, and the log of updates.</span>
</div>

{#if error}<p class="error">{error}</p>{/if}

{#if info}
  <section class="card">
    <h3>Version</h3>
    <p>
      Companion Server <strong>{info.version}</strong>{#if info.edge_version}; it gives boards companion-edge
        <strong>{info.edge_version}</strong>{/if}.
      {#if info.method === "git"}<span class="muted">(a git clone)</span>{/if}
    </p>
    <div class="row">
      {#if checking}
        <span class="muted">Asking GitHub for the newest release…</span>
      {:else if latest?.error}
        <span class="err">Could not check: {latest.error}</span>
      {:else if latest?.newer}
        <span class="pill warn">update available</span>
        <span>
          <strong>{latest.version}</strong>, released {when(latest.published)}
          (<a href={latest.url} target="_blank" rel="noopener noreferrer">release page</a>)
        </span>
      {:else if latest}
        <span class="pill ok">up to date</span>
        <span class="muted">newest release: {latest.version}</span>
      {/if}
      <button class="small" onclick={() => check(true)} disabled={checking}>Check again</button>
    </div>

    {#if latest?.newer || info.method === "git"}
      <div class="how">
        <strong>To update:</strong>
        <ol>
          <li>Stop the server (close its window, or Ctrl+C).</li>
          <li>
            In the server's folder run
            {#if info.method === "git"}<code>git pull</code> and <code>uv sync</code> (with your extras)
            {:else if windows}<code>update-companion.bat</code> (double-click it)
            {:else}<code>./update-companion.sh</code>{/if}. It downloads the release, checks it, backs up the
            files it replaces and keeps your settings, data and personas.
          </li>
          <li>Start the server again, then update your boards under Devices, Maintenance.</li>
        </ol>
      </div>
    {/if}

    {#if latest?.newer && latest.notes}
      <details>
        <summary>What is new in {latest.version}</summary>
        <Markdown text={latest.notes} />
      </details>
    {/if}
  </section>

  <section class="card">
    <h3>Update log</h3>
    {#if info.history.length}
      <div class="scroll">
        <table>
          <thead><tr><th>When</th><th>From</th><th>To</th><th>How</th><th>Result</th><th>Details</th></tr></thead>
          <tbody>
            {#each info.history as h}
              <tr>
                <td>{when(h.time)}</td>
                <td>{h.from ?? "–"}</td>
                <td>{h.to ?? "–"}</td>
                <td>{h.method}</td>
                <td>
                  {#if h.ok}<span class="pill ok">done</span>{:else}<span class="pill warn">failed</span>{/if}
                  {#if h.dependencies}<span class="muted small">{h.dependencies}</span>{/if}
                </td>
                <td class="wrap">
                  {#if h.error}<span class="err">{h.error}</span>
                  {:else if h.method === "release"}{h.files_written} files written, {h.files_removed} removed; backup
                    <code>{h.backup}</code>
                  {:else if h.commit_from}{h.commit_from} → {h.commit_to}{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}
      <p class="muted">No update yet. Updates made with update-companion are listed here.</p>
    {/if}
  </section>

  {#if info.changelog}
    <section class="card">
      <h3>Changelog</h3>
      <Markdown text={info.changelog.replace(/^# .*\n/, "")} />
    </section>
  {/if}
{:else if !error}
  <p class="muted">Loading…</p>
{/if}

<style>
  section {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3,
  section > p {
    margin: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .how {
    border: 1px solid var(--warn);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  .how ol {
    margin: 6px 0 0;
    padding-left: 20px;
  }
  .err {
    color: var(--err);
  }
  .small {
    font-size: 12px;
  }
  td.wrap {
    white-space: normal;
    min-width: 200px;
  }
  summary {
    cursor: pointer;
  }
</style>
