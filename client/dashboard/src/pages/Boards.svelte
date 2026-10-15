<script>
  import { api } from "../lib/api.js";
  import { duration } from "../lib/format.js";

  let { onAuthError } = $props();

  // --- the boards this server has set up ----------------------------------------
  let enrolled = $state(null);
  let listError = $state(null);
  let passwords = $state({}); // device_id -> revealed root password

  async function loadEnrolled() {
    try {
      enrolled = (await api("/enrolled")).devices;
      listError = null;
    } catch (err) {
      listError = err;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function revoke(id) {
    if (!confirm(`Lock ${id} out? Its token stops working at once; set it up again to let it back in.`)) return;
    try {
      await api(`/enrolled/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadEnrolled();
    } catch (err) {
      alert(err.message);
    }
  }

  async function reveal(id) {
    try {
      passwords[id] = (await api(`/enrolled/${encodeURIComponent(id)}/root-password`)).root_password;
    } catch (err) {
      alert(err.message);
    }
  }

  // --- the Add a board form ---------------------------------------------------------
  let info = $state(null);
  let scanning = $state(false);
  let form = $state({
    device_id: "",
    host: "",
    hostOther: "",
    profile: "licheerv-nano",
    server_url: "",
    serverOther: "",
    camera: "profile", // profile | on | off
    rotate: 0,
    wifi: [{ ssid: "", password: "" }],
    password: "",
  });
  let job = $state(null);
  let formError = $state(null);
  let installing = $derived(job?.state === "running");

  async function scan() {
    scanning = true;
    try {
      info = await api("/edge/setup");
      if (!form.host && info.boards.length) form.host = info.boards[0];
      if (!form.server_url && info.server_urls.length) form.server_url = info.server_urls[0];
    } catch (err) {
      formError = err.message;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      scanning = false;
    }
  }

  $effect(() => {
    loadEnrolled();
    scan();
    const timer = setInterval(() => {
      if (!document.hidden) loadEnrolled();
    }, 5000);
    return () => clearInterval(timer);
  });

  const host = $derived(form.host === "other" ? form.hostOther.trim() : form.host);
  const serverUrl = $derived(form.server_url === "other" ? form.serverOther.trim() : form.server_url);
  const nameOk = $derived(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(form.device_id));
  const wifiErrors = $derived(
    form.wifi.map((w) =>
      w.ssid && w.password && (w.password.length < 8 || w.password.length > 63) ? "8 to 63 characters" : null,
    ),
  );
  const ready = $derived(nameOk && host && /^wss?:\/\//.test(serverUrl) && !wifiErrors.some(Boolean));
  const localOnly = $derived(info && ["127.0.0.1", "localhost", "::1"].includes(info.listening));
  const replacing = $derived(enrolled?.some((d) => d.device_id === form.device_id));

  async function install() {
    formError = null;
    const body = {
      device_id: form.device_id,
      host,
      server_url: serverUrl,
      profile: form.profile,
      camera: form.camera === "profile" ? null : form.camera === "on",
      rotate: form.camera === "on" ? Number(form.rotate) : null,
      wifi: form.wifi.filter((w) => w.ssid.trim()).map((w) => ({ ssid: w.ssid.trim(), password: w.password || null })),
      password: form.password || null,
    };
    try {
      const { job: id } = await api("/edge/install", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      job = { id, state: "running", steps: [] };
      poll(id);
    } catch (err) {
      formError = err.message;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function poll(id) {
    while (job?.id === id) {
      try {
        job = await api(`/edge/install/${id}`);
      } catch (err) {
        formError = err.message;
        return;
      }
      if (job.state !== "running") {
        form.password = "";
        for (const w of form.wifi) w.password = "";
        loadEnrolled();
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  const MARK = { run: "…", ok: "✓", warn: "!", fail: "✗" };
</script>

<div class="head">
  <h2>Boards</h2>
  <span class="muted">Set up a board over USB, and the boards this server has set up.</span>
</div>

<section class="card">
  <h3>Add a board</h3>
  <ol class="howto muted">
    <li>Flash the board's SD card with the maker's image and let it boot (about 30 s).</li>
    <li>Plug the board into this computer by USB (on a LicheeRV Nano: the USB-C port).</li>
    <li>Fill in the form and press <strong>Set up</strong>. Then unplug it and power it from anywhere with Wi-Fi.</li>
  </ol>

  {#if localOnly}
    <p class="warnbox">
      This server listens only on this machine (<code>server.host: {info.listening}</code>), so a board cannot
      connect yet. Set <code>server.host</code> to <code>0.0.0.0</code> under Configuration and restart.
    </p>
  {/if}

  <div class="form">
    <label for="b-name">Name</label>
    <div>
      <input id="b-name" type="text" bind:value={form.device_id} placeholder="e.g. walker" spellcheck="false" disabled={installing} />
      {#if form.device_id && !nameOk}<span class="err small">letters, digits, - _ . (no spaces)</span>{/if}
      {#if replacing}<span class="muted small">set up before: it gets a new token, the old one stops working</span>{/if}
    </div>

    <label for="b-host">Board</label>
    <div class="row">
      <select id="b-host" bind:value={form.host} disabled={installing}>
        {#each info?.boards ?? [] as b}<option value={b}>{b} (USB)</option>{/each}
        <option value="other">another address…</option>
      </select>
      {#if form.host === "other"}
        <input type="text" bind:value={form.hostOther} placeholder="IP address or name" spellcheck="false" disabled={installing} />
      {/if}
      <button class="small" onclick={scan} disabled={scanning || installing}>{scanning ? "Looking…" : "Look again"}</button>
      {#if info && !info.boards.length}<span class="muted small">no board found on USB</span>{/if}
    </div>

    <label for="b-profile">Hardware</label>
    <select id="b-profile" bind:value={form.profile} disabled={installing}>
      {#each info?.profiles ?? [form.profile] as p}<option value={p}>{p}</option>{/each}
    </select>

    <label for="b-server">Server address for the board</label>
    <div class="row">
      <select id="b-server" bind:value={form.server_url} disabled={installing}>
        {#each info?.server_urls ?? [] as u}<option value={u}>{u}</option>{/each}
        <option value="other">another address…</option>
      </select>
      {#if form.server_url === "other"}
        <input type="text" bind:value={form.serverOther} placeholder="ws://192.168.1.10:8765/v1/realtime" spellcheck="false" disabled={installing} />
      {/if}
    </div>

    <span class="label">Away from home</span>
    <div>
      {#if info?.public_url}
        <span>The board also tries <code>{info.public_url}</code> (TLS, this server's own certificate).</span>
      {:else if info?.public_enabled}
        <span class="muted">The public listener is on, but <code>server.public.url</code> is not set, so boards do
          not know its address yet.</span>
      {:else}
        <span class="muted">Only at home. To reach this server from anywhere, turn on <code>server.public</code>
          (see README, "Reaching the server from outside"), then set the board up again.</span>
      {/if}
    </div>

    <label for="b-camera">Camera</label>
    <div class="row">
      <select id="b-camera" bind:value={form.camera} disabled={installing}>
        <option value="profile">as the hardware profile says</option>
        <option value="on">on</option>
        <option value="off">off</option>
      </select>
      {#if form.camera === "on"}
        <label class="inline">turned
          <select bind:value={form.rotate} disabled={installing}>
            {#each [0, 90, 180, 270] as r}<option value={r}>{r}°</option>{/each}
          </select>
        </label>
      {/if}
    </div>

    <span class="label">Wi-Fi</span>
    <div class="wifi">
      {#each form.wifi as w, i}
        <div class="row">
          <input type="text" bind:value={w.ssid} placeholder="network name" spellcheck="false" disabled={installing} />
          <input type="password" bind:value={w.password} placeholder="password (empty: open)" autocomplete="new-password" disabled={installing} />
          {#if form.wifi.length > 1}
            <button class="small" onclick={() => form.wifi.splice(i, 1)} disabled={installing}>Remove</button>
          {/if}
          {#if wifiErrors[i]}<span class="err small">{wifiErrors[i]}</span>{/if}
        </div>
      {/each}
      <button class="small" onclick={() => form.wifi.push({ ssid: "", password: "" })} disabled={installing}>Add a network</button>
      <span class="muted small">Home Wi-Fi and your phone's hotspot. The first one is preferred. Networks the board knows already are kept.</span>
    </div>

    <label for="b-pass">Board password</label>
    <div>
      <input id="b-pass" type="password" bind:value={form.password} placeholder="empty: the stock one (root)" autocomplete="off" disabled={installing} />
      <span class="muted small">Only if you changed it. The stock password is replaced with a random one, kept below.</span>
    </div>
  </div>

  {#if formError}<p class="error">{formError}</p>{/if}
  <button class="primary" onclick={install} disabled={!ready || installing}>{installing ? "Setting up…" : "Set up"}</button>

  {#if job}
    <ol class="steps">
      {#each job.steps as s}
        <li class={s.status}>
          <span class="mark">{MARK[s.status] ?? ""}</span>
          <strong>{s.step}</strong>
          {#if s.detail}<pre>{s.detail}</pre>{/if}
        </li>
      {/each}
    </ol>
    {#if job.state === "failed"}
      <p class="error">{job.error}</p>
    {:else if job.state === "done" && job.steps.at(-1)?.status === "ok"}
      <p class="banner ok">
        {job.device_id} is set up (companion-edge {job.result.version}) and connected. Unplug it; it connects over
        Wi-Fi from now on.
      </p>
    {:else if job.state === "done"}
      <p class="warnbox">
        {job.device_id} is set up (companion-edge {job.result.version}) but has not connected yet. Check that a
        Wi-Fi network in the list is in reach and that this server's address is right; the board's log below
        says what it tried. It keeps trying by itself.
      </p>
      {#if job.result.log_tail}
        <details>
          <summary>The board's log</summary>
          <pre>{job.result.log_tail}</pre>
        </details>
      {/if}
    {/if}
  {/if}
</section>

<section class="card">
  <h3>Boards set up by this server</h3>
  {#if listError && !enrolled}
    <p class="error">{listError.message}</p>
  {:else if enrolled?.length}
    <div class="scroll">
      <table>
        <thead>
          <tr><th>Name</th><th>Connected</th><th>Last seen</th><th>Hardware</th><th>Version</th><th>Root password</th><th></th></tr>
        </thead>
        <tbody>
          {#each enrolled as d (d.device_id)}
            <tr>
              <td><code>{d.device_id}</code></td>
              <td>{#if d.connected}<span class="pill ok">now</span>{:else}<span class="pill">no</span>{/if}</td>
              <td>{d.last_seen ? `${duration(Date.now() / 1000 - d.last_seen)} ago` : "never"}</td>
              <td>{d.profile ?? "–"}</td>
              <td>{d.installed_version ?? "–"}</td>
              <td>
                {#if passwords[d.device_id]}<code>{passwords[d.device_id]}</code>
                {:else if d.has_root_password}<button class="small" onclick={() => reveal(d.device_id)}>Show</button>
                {:else}–{/if}
              </td>
              <td><button class="small" onclick={() => revoke(d.device_id)}>Lock out</button></td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if enrolled}
    <p class="muted">None yet.</p>
  {/if}
</section>

<style>
  section {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  h3 {
    margin: 0;
  }
  .primary {
    align-self: flex-start;
  }
  .howto {
    margin: 0;
    padding-left: 20px;
  }
  .form {
    display: grid;
    grid-template-columns: minmax(140px, 200px) minmax(0, 1fr);
    gap: 10px 12px;
    align-items: start;
  }
  .form > label,
  .form > .label {
    padding-top: 6px;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .inline {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .wifi {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: flex-start;
  }
  input[type="text"],
  input[type="password"],
  select {
    max-width: 100%;
  }
  .form > div > input,
  .form > select {
    width: 100%;
    max-width: 360px;
  }
  .small {
    font-size: 12px;
  }
  .err {
    color: var(--err);
  }
  .warnbox {
    margin: 0;
    border: 1px solid var(--warn);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  .banner {
    margin: 0;
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
  }
  .steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 14px;
  }
  .steps li {
    display: grid;
    grid-template-columns: 20px auto;
    gap: 0 8px;
  }
  .steps pre,
  details pre {
    grid-column: 2;
    margin: 2px 0 0;
    font-size: 12px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--muted);
  }
  .steps .ok .mark {
    color: var(--ok);
  }
  .steps .warn .mark {
    color: var(--warn);
  }
  .steps .fail .mark {
    color: var(--err);
  }
  summary {
    cursor: pointer;
  }
  @media (max-width: 720px) {
    .form {
      grid-template-columns: minmax(0, 1fr);
    }
    .form > label,
    .form > .label {
      padding-top: 0;
    }
  }
</style>
