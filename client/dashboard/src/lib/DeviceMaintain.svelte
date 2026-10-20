<script>
  import { api, getToken } from "./api.js";

  let { device } = $props();

  const id = $derived(device.device_id);
  const enc = (s) => encodeURIComponent(s);
  let serverVersion = $state(null);
  let busy = $state(null); // which action is running
  let message = $state(null); // {kind, text}
  let wifi = $state(null); // {networks, current}
  let newNet = $state({ ssid: "", password: "" });
  let sayText = $state("This is a speaker test.");
  let micSeconds = $state(5);
  let micAudio = $state(null);
  let bt = $state(null); // the device's Bluetooth state
  let found = $state(null); // what a scan found
  let btAddress = $state("");
  let ts = $state(null); // the device's Tailscale state
  let tsKey = $state("");
  let tsWaiting = $state(false); // polling until someone opens the login link

  $effect(() => {
    api("/edge/version")
      .then((r) => (serverVersion = r.version))
      .catch(() => {});
  });

  $effect(() => {
    id;
    wifi = null;
    message = null;
    if (micAudio) URL.revokeObjectURL(micAudio);
    micAudio = null;
    bt = found = ts = null;
    btAddress = tsKey = "";
    tsWaiting = false;
  });

  async function run(name, fn) {
    busy = name;
    message = null;
    try {
      message = { kind: "ok", text: await fn() };
    } catch (err) {
      message = { kind: "error", text: err.message };
    } finally {
      busy = null;
    }
  }

  const post = (path, body) =>
    api(`/devices/${enc(id)}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });

  function update() {
    run("update", async () => {
      const r = await post("/update");
      return r.restarting
        ? `companion-edge ${r.version} is installed; the device restarts with it and comes back in a minute. If it cannot connect, it goes back to the version before by itself.`
        : "It already runs this version.";
    });
  }

  function restart(what) {
    const text = what === "board" ? "Reboot the whole board?" : "Restart the companion client on the device?";
    if (!confirm(text)) return;
    run("restart", async () => {
      await post("/restart", { what });
      return what === "board" ? "The board reboots; it is back in about a minute." : "The client restarts.";
    });
  }

  function loadWifi() {
    run("wifi", async () => {
      wifi = await api(`/devices/${enc(id)}/wifi`);
      return null;
    });
  }

  function changeWifi(body) {
    run("wifi", async () => {
      wifi = await api(`/devices/${enc(id)}/wifi`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      newNet = { ssid: "", password: "" };
      return "Saved on the device.";
    });
  }

  function loadBluetooth() {
    run("bluetooth", async () => {
      bt = await api(`/devices/${enc(id)}/bluetooth`);
      return null;
    });
  }

  function scanBluetooth() {
    run("bt-scan", async () => {
      bt = await post("/bluetooth/scan", { seconds: 10 });
      found = bt.found ?? [];
      return found.length ? null : "Nothing found. Is the speaker in pairing mode? Or type its address below.";
    });
  }

  function pairBluetooth(address) {
    run("bt-pair", async () => {
      bt = await post("/bluetooth/pair", { address });
      found = null;
      btAddress = "";
      const name = bt.speaker?.name || address;
      if (bt.speaker && !bt.speaker.connected)
        return `Paired with ${name}, but not connected yet; the device keeps trying (is the speaker on?).`;
      return bt.restarting
        ? `Paired with ${name}. The device plays through it from now on and reconnects in a moment.`
        : `Paired with ${name}; the device plays through it.`;
    });
  }

  function forgetBluetooth(device) {
    const speaker = bt?.speaker?.address === device.address;
    const text = speaker
      ? `Forget ${device.name || device.address}? The device goes back to the speaker it used before.`
      : `Forget ${device.name || device.address}?`;
    if (!confirm(text)) return;
    run("bt-pair", async () => {
      bt = await post("/bluetooth/remove", { address: device.address });
      return bt.restarting ? "Forgotten. The device plays through its speaker from before and reconnects." : "Forgotten.";
    });
  }

  function loadTailscale() {
    run("tailscale", async () => {
      ts = await api(`/devices/${enc(id)}/tailscale`);
      return null;
    });
  }

  function joinTailscale() {
    run("ts-up", async () => {
      ts = await post("/tailscale", { auth_key: tsKey.trim() || null });
      tsKey = "";
      if (ts.state === "Running") return tsJoined();
      if (ts.auth_url) {
        waitForLogin();
        return "Open the link below and approve the board in your Tailscale account.";
      }
      return `Tailscale says: ${ts.state ?? "not running"}. Look again in a moment.`;
    });
  }

  function tsJoined() {
    return ts.server_added
      ? "The board is on the tailnet and now also tries this server's Tailscale address, so it reaches it away from home too."
      : "The board is on the tailnet.";
  }

  async function waitForLogin() {
    const device = id;
    tsWaiting = true;
    const until = Date.now() + 10 * 60 * 1000;
    while (tsWaiting && device === id && Date.now() < until) {
      await new Promise((r) => setTimeout(r, 3000));
      if (!tsWaiting || device !== id) break;
      try {
        ts = await api(`/devices/${enc(id)}/tailscale`);
      } catch {
        continue; // the device may be busy for a moment
      }
      if (ts.state === "Running") {
        message = { kind: "ok", text: tsJoined() };
        break;
      }
    }
    if (device === id) tsWaiting = false;
  }

  function leaveTailscale() {
    if (!confirm("Take the board off the tailnet? It then reaches this server only on the same network.")) return;
    run("ts-up", async () => {
      ts = await post("/tailscale/logout");
      return "The board left the tailnet.";
    });
  }

  function speaker() {
    run("speaker", async () => {
      await post("/speaker-test", { text: sayText });
      return "Sent; you should hear it on the device now.";
    });
  }

  function mic() {
    run("mic", async () => {
      const rec = await post("/mic-test", { seconds: Number(micSeconds) });
      const headers = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(`/v1/admin/recordings/${rec.id}/audio`, { headers });
      if (micAudio) URL.revokeObjectURL(micAudio);
      micAudio = URL.createObjectURL(await res.blob());
      return `Recorded ${rec.duration_s.toFixed(1)} s of what the microphone sends. Listen below.`;
    });
  }

  const deviceVersion = $derived(device.status?.version ?? null);
  const pwShort = $derived(newNet.password && (newNet.password.length < 8 || newNet.password.length > 63));
  const addressOk = $derived(/^[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}$/.test(btAddress.trim()));
  const keyBad = $derived(tsKey.trim() !== "" && !/^tskey-[A-Za-z0-9_-]+$/.test(tsKey.trim()));
  const servers = $derived((ts?.servers ?? []).map((u) => u.replace(/\/v1\/realtime$/, "")));
</script>

{#if message?.text}
  <p class={message.kind === "error" ? "error" : "banner"} role="status">{message.text}</p>
{/if}

<section class="card">
  <h3>Software</h3>
  <p>
    The device runs companion-edge <strong>{deviceVersion ?? "(unknown: older than 1.2)"}</strong>; this server
    has <strong>{serverVersion ?? "…"}</strong>.
  </p>
  <div class="row">
    <button onclick={update} disabled={busy || !serverVersion || deviceVersion === serverVersion}>
      {busy === "update" ? "Updating…" : `Update to ${serverVersion ?? "…"}`}
    </button>
    <button onclick={() => restart("client")} disabled={busy}>Restart the client</button>
    <button onclick={() => restart("board")} disabled={busy}>Reboot the board</button>
  </div>
  <p class="muted">Works wherever the device is: everything goes over its own connection.</p>
</section>

<section class="card">
  <h3>Wi-Fi networks</h3>
  {#if !wifi}
    <button onclick={loadWifi} disabled={busy}>{busy === "wifi" ? "Asking…" : "Show the device's networks"}</button>
  {:else}
    <ul class="nets">
      {#each wifi.networks as ssid}
        <li>
          <code>{ssid}</code>
          {#if ssid === wifi.current}<span class="pill ok">connected now</span>
          {:else}<button class="small" onclick={() => changeWifi({ remove: [ssid] })} disabled={busy}>Remove</button>{/if}
        </li>
      {:else}
        <li class="muted">None.</li>
      {/each}
    </ul>
    <div class="row">
      <input type="text" bind:value={newNet.ssid} placeholder="network name" spellcheck="false" />
      <input type="password" bind:value={newNet.password} placeholder="password (empty: open)" autocomplete="new-password" />
      <button onclick={() => changeWifi({ add: [{ ssid: newNet.ssid.trim(), password: newNet.password || null }] })} disabled={busy || !newNet.ssid.trim() || pwShort}>Add</button>
      {#if pwShort}<span class="err small">8 to 63 characters</span>{/if}
    </div>
    <p class="muted">
      The password stays on this server; the device gets only the key derived from it. The network it uses now cannot
      be removed from here.
    </p>
  {/if}
</section>

<section class="card">
  <h3>Bluetooth speaker</h3>
  {#if !bt}
    <button onclick={loadBluetooth} disabled={busy}>{busy === "bluetooth" ? "Asking…" : "Show the device's Bluetooth"}</button>
  {:else if !bt.controller}
    <p class="muted">This device has no Bluetooth.</p>
  {:else if !bt.tools?.bluetoothctl || !bt.tools?.bluealsa}
    <p class="muted">
      Its system lacks {!bt.tools?.bluetoothctl ? "BlueZ (bluetoothctl)" : "bluez-alsa (bluealsa)"}, which it needs to
      play to a Bluetooth speaker.
    </p>
  {:else}
    {#if bt.speaker}
      <p>
        Plays through <strong>{bt.speaker.name || bt.speaker.address}</strong> <code>{bt.speaker.address}</code>
        {#if bt.speaker.connected}<span class="pill ok">connected</span>
        {:else if bt.speaker.paired}<span class="pill warn">not connected</span>
        {:else}<span class="pill warn">not paired any more</span>{/if}
        {#if bt.output_device !== bt.pcm}<span class="muted small">(but its output is set to <code>{bt.output_device}</code>)</span>{/if}
      </p>
      {#if bt.speaker.paired && !bt.speaker.connected}
        <p class="muted small">It connects by itself once the speaker is on and near (the device keeps trying).</p>
      {/if}
    {:else}
      <p class="muted">No Bluetooth speaker; it plays through <code>{bt.output_device}</code>.</p>
    {/if}
    {#if bt.devices.length}
      <ul class="nets">
        {#each bt.devices as d}
          <li>
            <span>{d.name || "(no name)"}</span> <code>{d.address}</code>
            {#if d.connected}<span class="pill ok">connected</span>{/if}
            <button class="small" onclick={() => forgetBluetooth(d)} disabled={busy}>Forget</button>
          </li>
        {/each}
      </ul>
    {/if}
    <div class="row">
      <button onclick={scanBluetooth} disabled={busy}>{busy === "bt-scan" ? "Looking (10 s)…" : "Look for speakers"}</button>
      <input type="text" bind:value={btAddress} placeholder="or its address, AA:BB:CC:DD:EE:FF" spellcheck="false" />
      <button onclick={() => pairBluetooth(btAddress.trim().toUpperCase())} disabled={busy || !addressOk}>
        {busy === "bt-pair" ? "Pairing…" : "Pair"}
      </button>
    </div>
    {#if found?.length}
      <ul class="nets">
        {#each found as d}
          <li>
            <span>{d.name || "(no name)"}</span> <code>{d.address}</code>
            {#if d.paired}<span class="pill">paired</span>{/if}
            <button class="small" onclick={() => pairBluetooth(d.address)} disabled={busy}>Pair</button>
          </li>
        {/each}
      </ul>
    {/if}
    <p class="muted">
      Put the speaker in pairing mode first (usually: hold its Bluetooth button until it flashes) and disconnect it
      from phones nearby. Some boards find nothing when looking: type the speaker's address instead (on its label,
      or in a phone's Bluetooth settings once paired there). The device then plays through it, keeps it connected,
      also after a restart, and waits a moment before speaking so the first words are not cut.
    </p>
  {/if}
</section>

<section class="card">
  <h3>Tailscale</h3>
  {#if !ts}
    <button onclick={loadTailscale} disabled={busy}>{busy === "tailscale" ? "Asking…" : "Show the device's Tailscale"}</button>
  {:else}
    <p>
      {#if !ts.installed}
        <span class="pill">not installed</span> It gets Tailscale from this server when it joins (about 40 MB to
        download, a few minutes on a small board).
      {:else if !ts.running}
        <span class="pill warn">not running</span> Tailscale is installed but its service does not run.
      {:else if ts.state === "Running"}
        <span class="pill ok">on the tailnet</span>
        {ts.tailnet ? `${ts.tailnet}, ` : ""}as <strong>{ts.hostname}</strong>
        {#if ts.ips.length}<code>{ts.ips.join(", ")}</code>{/if}
      {:else}
        <span class="pill warn">{ts.state === "NeedsLogin" ? "needs a login" : ts.state}</span>
      {/if}
      {#if ts.version}<span class="muted small">Tailscale {ts.version}</span>{/if}
    </p>
    {#if ts.auth_url && ts.state !== "Running"}
      <p>
        Open <a href={ts.auth_url} target="_blank" rel="noopener noreferrer">{ts.auth_url}</a> and approve the board in
        your Tailscale account.{#if tsWaiting} <span class="muted small">Waiting for it…</span>{/if}
      </p>
    {/if}
    {#if ts.installed && !ts.tun}
      <p class="err small">This board's system has no TUN device (/dev/net/tun); Tailscale cannot run on it.</p>
    {/if}
    {#if ts.state !== "Running"}
      <div class="row">
        <input type="password" bind:value={tsKey} placeholder="auth key (empty: a login link)" autocomplete="off" spellcheck="false" class="grow" />
        <button onclick={joinTailscale} disabled={busy || keyBad || tsWaiting}>
          {busy === "ts-up" ? (ts.installed ? "Joining…" : "Installing (a few minutes)…") : ts.installed ? "Join" : "Install and join"}
        </button>
        {#if keyBad}<span class="err small">an auth key starts with tskey-</span>{/if}
      </div>
    {:else}
      <div class="row">
        <button onclick={loadTailscale} disabled={busy}>Look again</button>
        <button onclick={leaveTailscale} disabled={busy || ts.through_tailscale}>Leave the tailnet</button>
        {#if ts.through_tailscale}<span class="muted small">It reaches this server through Tailscale now, so it cannot leave from here.</span>{/if}
      </div>
    {/if}
    {#if servers.length}
      <p class="muted small">It tries this server at {servers.join(", then ")}.</p>
    {/if}
    <p class="muted">
      On the same tailnet as this server, the board reaches it from anywhere (a phone's hotspot, other Wi-Fi). Use
      the account this server is signed in to. A key from the Tailscale admin console (Settings, Keys) needs no
      link; without one, open the link that shows here.
    </p>
  {/if}
</section>

<section class="card">
  <h3>Speaker and microphone</h3>
  <div class="row">
    <input type="text" bind:value={sayText} class="grow" />
    <button onclick={speaker} disabled={busy || !sayText.trim()}>{busy === "speaker" ? "Speaking…" : "Say it on the device"}</button>
  </div>
  <div class="row">
    <label>Record
      <select bind:value={micSeconds}>{#each [3, 5, 10] as s}<option value={s}>{s} s</option>{/each}</select>
    </label>
    <button onclick={mic} disabled={busy}>{busy === "mic" ? "Recording…" : "Record from its microphone"}</button>
    {#if micAudio}<audio controls src={micAudio}></audio>{/if}
  </div>
  <p class="muted">The recording is what the server receives (after the device's encoding and the network).</p>
</section>

<style>
  section {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 12px;
  }
  h3,
  p {
    margin: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .grow {
    flex: 1 1 240px;
    min-width: 0;
  }
  .nets {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .nets li {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .banner {
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 14px;
    margin: 0 0 8px;
  }
  .small {
    font-size: 12px;
  }
  .err {
    color: var(--err);
  }
  audio {
    height: 32px;
    max-width: 100%;
  }
</style>
