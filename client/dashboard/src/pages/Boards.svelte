<script>
  import { api } from "../lib/api.js";
  import { duration } from "../lib/format.js";
  import { DEVICE_EVENTS, onEvents } from "../lib/live.js";
  import { waitForServer } from "../lib/restart.js";

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
    keep: true, // a board set up before: update it, keeping its token and password
    tailscale: false,
    tailscaleKey: "", // empty: the board shows a login link instead
  });
  let formTop = $state(null);
  let job = $state(null);
  let formError = $state(null);
  let installing = $derived(job?.state === "running");

  async function scan() {
    scanning = true;
    try {
      info = await api("/edge/setup");
      if (!form.host && info.boards.length) form.host = info.boards[0];
      if (!form.server_url && info.server_urls.length) form.server_url = lanUrl ?? info.server_urls[0];
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
    // Live: a board connecting or leaving shows at once; the timer is a fallback
    // (and moves "last seen" along).
    const stop = onEvents(DEVICE_EVENTS, () => loadEnrolled());
    const timer = setInterval(() => {
      if (!document.hidden) loadEnrolled();
    }, 15000);
    return () => {
      stop();
      clearInterval(timer);
    };
  });

  const host = $derived(form.host === "other" ? form.hostOther.trim() : form.host);
  const serverUrl = $derived(form.server_url === "other" ? form.serverOther.trim() : form.server_url);
  const nameOk = $derived(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/.test(form.device_id));
  const wifiErrors = $derived(
    form.wifi.map((w) =>
      w.ssid && w.password && (w.password.length < 8 || w.password.length > 63) ? "8 to 63 characters" : null,
    ),
  );
  const keyBad = $derived(
    form.tailscale && form.tailscaleKey.trim() !== "" && !/^tskey-[A-Za-z0-9_-]+$/.test(form.tailscaleKey.trim()),
  );
  const ready = $derived(
    nameOk && host && /^wss?:\/\//.test(serverUrl) && !wifiErrors.some(Boolean) && !keyBad,
  );
  const localOnly = $derived(info && ["127.0.0.1", "localhost", "::1"].includes(info.listening));
  const replacing = $derived(enrolled?.some((d) => d.device_id === form.device_id));
  const updating = $derived(replacing && form.keep);

  function updateBoard(id) {
    form.device_id = id;
    form.keep = true;
    formTop?.scrollIntoView({ behavior: "smooth" });
  }

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
      keep_token: Boolean(updating),
      tailscale: form.tailscale,
      tailscale_auth_key: (form.tailscale && form.tailscaleKey.trim()) || null,
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
        form.tailscaleKey = "";
        for (const w of form.wifi) w.password = "";
        loadEnrolled();
        return;
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
  }

  const MARK = { run: "…", ok: "✓", warn: "!", fail: "✗" };

  // A step's detail as text and links (the Tailscale login link must be clickable).
  function parts(detail) {
    return detail.split(/(https:\/\/\S+)/).map((text, i) => ({ text, link: i % 2 === 1 }));
  }

  // Tailscale addresses (100.64.0.0/10) reach the server from anywhere on the tailnet.
  function tailnet(url) {
    const m = url.match(/^wss?:\/\/100\.(\d+)\./);
    return Boolean(m && Number(m[1]) >= 64 && Number(m[1]) <= 127);
  }
  // This network's address comes first: a Tailscale one reaches only a board already on
  // the tailnet; one put on it below gets that address as a further one.
  const lanUrl = $derived(info?.server_urls?.find((u) => !tailnet(u)) ?? null);
  const tailnetUnreachable = $derived(tailnet(serverUrl) && !form.tailscale);

  // --- away from home: the public listener ------------------------------------------
  let away = $state(null); // form: {enabled, address, port}
  let awayBusy = $state(null);
  let awayMessage = $state(null);
  const pub = $derived(info?.public ?? null);
  const pending = $derived(
    pub && (pub.saved.enabled !== pub.enabled || pub.saved.port !== pub.port || pub.saved.url !== pub.url),
  );
  const lanHost = $derived((lanUrl ?? "").replace(/^ws:\/\/|:\d+\/v1\/realtime$/g, ""));
  const awayPort = $derived(Number(away?.port) || 8766);
  const firewall = $derived(
    `New-NetFirewallRule -DisplayName "Companion devices" -Direction Inbound -Protocol TCP -LocalPort ${awayPort} -Action Allow -Profile Private`,
  );

  $effect(() => {
    if (pub && !away) {
      const url = pub.saved.url ?? "";
      const address = url.replace(/^wss?:\/\//, "").replace(/:\d+\/v1\/realtime$/, "").replace(/^\[|\]$/g, "");
      away = { enabled: pub.saved.enabled, address, port: pub.saved.port };
    }
  });

  async function saveAway() {
    awayBusy = "save";
    awayMessage = null;
    try {
      const r = await api("/edge/public", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: away.enabled, address: away.address, port: awayPort }),
      });
      awayMessage = { kind: "ok", text: r.pending_restart.length ? "Saved. It takes effect after a restart." : "Saved." };
      await scan();
    } catch (err) {
      awayMessage = { kind: "error", text: err.message };
    } finally {
      awayBusy = null;
    }
  }

  async function restartServer() {
    awayBusy = "restart";
    awayMessage = { kind: "ok", text: "Restarting the server…" };
    try {
      await api("/restart", { method: "POST" });
      const back = await waitForServer(window.location.origin);
      awayMessage = back
        ? { kind: "ok", text: "The server is back with the new settings." }
        : { kind: "error", text: "The server did not come back within 2 minutes; look at its window." };
      await scan();
    } catch (err) {
      awayMessage = { kind: "error", text: err.message };
    } finally {
      awayBusy = null;
    }
  }

  function copy(text) {
    navigator.clipboard?.writeText(text);
  }
</script>

<div class="head">
  <h2>Boards</h2>
  <span class="muted">Set up a board over USB, and the boards this server has set up.</span>
</div>

<section class="card" bind:this={formTop}>
  <h3>Add or update a board</h3>
  <ol class="howto muted">
    <li>Flash the board's SD card with the maker's image and let it boot (about 30 s).</li>
    <li>Plug the board into this computer by USB (on a LicheeRV Nano: the USB-C port).</li>
    <li>Fill in the form and press <strong>Set up</strong>. Then unplug it and power it from anywhere with Wi-Fi.</li>
    <li>A board set up before, that cannot connect (say, a new Wi-Fi network): plug it in and press
      <strong>Update over USB</strong> below; it keeps its token.</li>
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
      {#if replacing}
        <label class="keep">
          <input type="checkbox" bind:checked={form.keep} disabled={installing} />
          Update it: keep its token and password, bring its software, settings and Wi-Fi up to date
        </label>
        {#if !form.keep}<span class="muted small">set up anew: it gets a new token, the old one stops working</span>{/if}
      {/if}
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
        {#each info?.server_urls ?? [] as u}<option value={u}>{u}{tailnet(u) ? " (Tailscale: a board on your tailnet, home and away)" : " (this network)"}</option>{/each}
        <option value="other">another address…</option>
      </select>
      {#if form.server_url === "other"}
        <input type="text" bind:value={form.serverOther} placeholder="ws://192.168.1.10:8765/v1/realtime" spellcheck="false" disabled={installing} />
      {/if}
      {#if tailnetUnreachable}
        <span class="err small">A Tailscale address reaches this server only from a board on Tailscale: tick
          <b>Put the board on Tailscale</b> below, or pick this network's address, unless the board is on your
          tailnet already.</span>
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
          (see docs/remote-access.md), then set the board up again.</span>
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

    <span class="label">Tailscale</span>
    <div>
      <label class="keep">
        <input type="checkbox" bind:checked={form.tailscale} disabled={installing} />
        Put the board on Tailscale, so it reaches this server from anywhere
      </label>
      {#if form.tailscale}
        <input type="password" bind:value={form.tailscaleKey} placeholder="auth key (empty: a login link)" autocomplete="off" spellcheck="false" disabled={installing} />
        {#if keyBad}<span class="err small">an auth key starts with tskey-</span>{/if}
        <span class="muted small">
          The board needs Internet for it (one of the Wi-Fi networks above in reach). Without a key, a link shows
          below while it is set up: open it and approve the board in your Tailscale account. A key (Tailscale admin
          console, Settings, Keys) needs no link. Tailscale is copied to the board if it has none (about 70 MB).
        </span>
        {#if info && !info.tailscale_urls?.length}
          <span class="err small">This server is not on Tailscale itself: install it here too and sign in to the
            same account, or the board cannot reach it that way.</span>
        {:else if info}
          <span class="muted small">The board also tries this server's Tailscale address,
            <code>{info.tailscale_urls[0]}</code>.</span>
        {/if}
      {/if}
    </div>

    <label for="b-pass">Board password</label>
    <div>
      <input id="b-pass" type="password" bind:value={form.password} placeholder="empty: the stock one (root)" autocomplete="off" disabled={installing} />
      <span class="muted small">Only if you changed it; a password of your own stays as it is. The stock password is replaced with a random one, kept below.</span>
    </div>
  </div>

  {#if formError}<p class="error">{formError}</p>{/if}
  <button class="primary" onclick={install} disabled={!ready || installing}>
    {installing ? (updating ? "Updating…" : "Setting up…") : updating ? "Update" : "Set up"}
  </button>

  {#if job}
    <ol class="steps">
      {#each job.steps as s}
        <li class={s.status}>
          <span class="mark">{MARK[s.status] ?? ""}</span>
          <strong>{s.step}</strong>
          {#if s.detail}<pre>{#each parts(s.detail) as p}{#if p.link}<a href={p.text} target="_blank" rel="noopener noreferrer">{p.text}</a>{:else}{p.text}{/if}{/each}</pre>{/if}
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
  <h3>Away from home</h3>
  <p class="muted">
    For boards on mobile data or other Wi-Fi: a second port with TLS that only boards set up by this server can
    use (never the dashboard). See docs/remote-access.md.
  </p>
  {#if pub}
    <p>
      {#if pub.enabled && pub.listening}
        <span class="pill ok">on</span> listening on <code>{pub.listening.join(", ")}</code>{#if pub.url}; boards use
          <code>{pub.url}</code>{/if}
      {:else if pub.enabled}
        <span class="pill warn">on, not listening</span> no address could be used; see the server's log
      {:else}
        <span class="pill">off</span> boards reach this server only at home
      {/if}
    </p>
    {#if pub.certificate_sha256}
      <p class="muted small">Certificate (SHA-256): <code class="wrap">{pub.certificate_sha256}</code></p>
    {/if}
  {/if}
  {#if away && pub?.editable}
    <div class="form">
      <span class="label">Reach from outside</span>
      <label class="keep"><input type="checkbox" bind:checked={away.enabled} disabled={awayBusy} /> on</label>
      {#if away.enabled}
        <label for="a-address">Your home's address</label>
        <div>
          <input id="a-address" type="text" bind:value={away.address} placeholder="myhome.duckdns.org or your public IP" spellcheck="false" disabled={awayBusy} />
          <span class="muted small">A dynamic DNS name if your address changes. Your router's status page shows the
            public IP (or search "what is my IP" on a phone on home Wi-Fi).</span>
        </div>
        <label for="a-port">Port</label>
        <input id="a-port" type="text" inputmode="numeric" bind:value={away.port} disabled={awayBusy} />
      {/if}
    </div>
    <div class="row">
      <button onclick={saveAway} disabled={awayBusy || (away.enabled && !away.address.trim())}>
        {awayBusy === "save" ? "Saving…" : "Save"}
      </button>
      {#if pending && pub.can_restart}
        <span class="warn small">Saved settings wait for a restart.</span>
        <button class="primary" onclick={restartServer} disabled={awayBusy}>
          {awayBusy === "restart" ? "Restarting…" : "Restart the server"}
        </button>
      {/if}
    </div>
    {#if awayMessage}
      <p class={awayMessage.kind === "error" ? "error" : "muted"}>{awayMessage.text}</p>
    {/if}
    {#if away.enabled}
      <ol class="steps-todo">
        <li>
          Router, IPv4: forward TCP port <strong>{awayPort}</strong> to this computer,
          <code>{lanHost || "its address"}</code>. With IPv6: allow incoming TCP {awayPort} to this computer only.
        </li>
        <li>
          Firewall of this computer (PowerShell as administrator), this port only:
          <code class="wrap">{firewall}</code>
          <button class="small" onclick={() => copy(firewall)}>Copy</button>
        </li>
        <li>
          Check from outside: on your phone with Wi-Fi off, open
          <code>https://{away.address || "<your address>"}:{awayPort}/health</code>. The browser warns about the
          certificate (expected: it is this server's own); then it should say <code>status ok</code>.
        </li>
        <li>Plug each board in and press <strong>Update over USB</strong>, so it learns the address.</li>
      </ol>
    {/if}
  {:else if pub && !pub.editable}
    <p class="muted">This server was not started from a config file; set <code>server.public</code> there.</p>
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
              <td class="actions">
                <button class="small" onclick={() => updateBoard(d.device_id)}>Update over USB</button>
                <button class="small" onclick={() => revoke(d.device_id)}>Lock out</button>
              </td>
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
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .warn {
    color: var(--warn);
  }
  .wrap {
    overflow-wrap: anywhere;
    white-space: normal;
  }
  .steps-todo {
    margin: 0;
    padding-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    font-size: 14px;
  }
  .keep {
    display: flex;
    gap: 6px;
    align-items: center;
    font-size: 13px;
    margin-top: 4px;
  }
  .actions {
    display: flex;
    gap: 6px;
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
  section > p {
    margin: 0;
  }
  .form > input {
    width: 120px;
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
