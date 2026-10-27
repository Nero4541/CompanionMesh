<script>
  import qrcode from "qrcode-generator";
  import { api } from "./api.js";
  import { setPath } from "./schema.js";

  let { onAuthError } = $props();

  let info = $state(null); // {token_set, token_env, editable, listening, viewer, urls: [{url, tailscale}]}
  let error = $state(null);
  let busy = $state(false);
  let shown = $state(false); // the QR code holds the token: only on request
  let picked = $state(0);
  let copied = $state(false);

  async function load() {
    try {
      info = await api("/phone");
      error = null;
    } catch (err) {
      error = err.message;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function make(renew) {
    if (renew && !confirm("Make a new phone login? Phones and browsers logged in with the old one have to log in again.")) return;
    busy = true;
    try {
      info = await api("/phone", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ renew }),
      });
      error = null;
      picked = 0;
      shown = true;
    } catch (err) {
      error = err.message;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      busy = false;
    }
  }

  // The phone page (/view/) answers nothing while server.viewer.enabled is off; it applies at once.
  async function turnOnViewer() {
    busy = true;
    try {
      const cfg = await api("/config");
      const draft = cfg.saved ?? cfg.running;
      setPath(draft, "server.viewer.enabled", true);
      await api("/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: draft }),
      });
      await load();
    } catch (err) {
      error = err.message;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      busy = false;
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
      setTimeout(() => (copied = false), 2000);
    } catch (err) {
      error = `cannot copy: ${err.message}`;
    }
  }

  $effect(() => {
    load();
  });

  const url = $derived(info?.urls?.[picked]?.url ?? null);
  // Black on white whatever the theme, so phone cameras read it.
  const svg = $derived.by(() => {
    if (!url) return "";
    const qr = qrcode(0, "M");
    qr.addData(url);
    qr.make();
    return qr.createSvgTag({ cellSize: 4, margin: 2, scalable: true });
  });
  const localOnly = $derived(info && ["127.0.0.1", "localhost", "::1"].includes(info.listening));
  const host = (u) => new URL(u).host;
</script>

<div class="card">
  <h3>Phone</h3>
  {#if error}<p class="error">{error}</p>{/if}
  {#if info}
    {#if localOnly}
      <p class="muted">
        The server listens only on this machine (<code>server.host: {info.listening}</code>): set it to
        <code>0.0.0.0</code> under Configuration and restart, so a phone can reach it.
      </p>
    {/if}
    {#if !info.viewer}
      <p class="muted">
        The phone page is off (<code>server.viewer.enabled</code>): a phone can open the dashboard, but not follow the
        conversation, talk or listen there.
      </p>
      <div class="row">
        <button class="small" onclick={turnOnViewer} disabled={busy || !info.editable}>Turn on the phone page</button>
      </div>
    {/if}
    {#if !info.token_set}
      <p class="muted">
        A phone logs in to this dashboard with an admin token, which this server has none of yet. This machine
        never needs it.
      </p>
      <div class="row">
        <button class="primary" onclick={() => make(false)} disabled={busy || !info.editable}>
          {busy ? "Making…" : "Make a phone login"}
        </button>
      </div>
    {:else if !info.urls.length}
      <p class="muted">No network address of this machine found for a phone to reach.</p>
    {:else}
      <p class="muted">
        Scan it with the phone's camera: the dashboard opens there and stays logged in. A Tailscale address works
        away from home too; the https one also lets the phone page talk (push to talk).
      </p>
      {#if info.urls.length > 1}
        <select bind:value={picked} aria-label="Address for the phone">
          {#each info.urls as u, i (u.url)}
            <option value={i}>{host(u.url)} {u.https ? "(Tailscale https: home and away, push to talk)" : u.tailscale ? "(Tailscale: home and away)" : "(this network)"}</option>
          {/each}
        </select>
      {/if}
      {#if shown}
        <div class="qr">{@html svg}</div>
      {/if}
      <div class="row">
        <button class="small" onclick={() => (shown = !shown)}>{shown ? "Hide" : "Show the QR code"}</button>
        <button class="small" onclick={copy}>{copied ? "Copied" : "Copy the link"}</button>
        <button class="small" onclick={() => make(true)} disabled={busy || !info.editable}>New token</button>
      </div>
      <span class="muted small">Whoever has the code or the link can use the dashboard; a new token logs every phone out.</span>
    {/if}
  {/if}
</div>

<style>
  .card { display: flex; flex-direction: column; gap: 8px; }
  h3, p { margin: 0; }
  .row { display: flex; flex-wrap: wrap; gap: 6px; }
  .qr { width: 200px; max-width: 100%; padding: 8px; background: #fff; border-radius: 8px; line-height: 0; }
  .qr :global(svg) { width: 100%; height: auto; }
  .small { font-size: 13px; }
</style>
