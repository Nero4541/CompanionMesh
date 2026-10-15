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
