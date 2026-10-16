<script>
  import { api, apiImage } from "./api.js";

  let { deviceId, connected = true, roles = null } = $props();

  // null: not connected now (roles unknown); otherwise whether it joined as a camera.
  const hasCamera = $derived(roles === null || roles.includes("camera"));

  let src = $state(null);
  let info = $state(null); // {age_s, received, reason}
  let message = $state(null);
  let busy = $state(false);
  let live = $state(false); // pictures in a row while this view is open
  let shown = 0; // `received` of the picture on screen
  let loading = false;

  const enc = (s) => encodeURIComponent(s);

  async function load() {
    if (loading) return;
    loading = true;
    try {
      const list = (await api("/frames")).frames;
      info = list.find((f) => f.device_id === deviceId) ?? null;
      if (!info || info.received === shown) return; // nothing new
      const url = await apiImage(`/devices/${enc(deviceId)}/frame`);
      if (src) URL.revokeObjectURL(src);
      src = url;
      shown = info.received;
    } catch (err) {
      message = err.message;
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    deviceId;
    info = null;
    shown = 0;
    load();
    // Faster while live; the picture is only fetched again when there is a new one.
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, live ? 400 : 3000);
    return () => clearInterval(timer);
  });

  $effect(() => () => src && URL.revokeObjectURL(src));

  const setLive = (seconds) =>
    api(`/devices/${enc(deviceId)}/live`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds, fps: 2 }),
    });

  // While live: ask for 15 s of pictures and renew every 10 s, so the device
  // stops by itself soon after nobody watches (page closed, network gone).
  $effect(() => {
    if (!live) return;
    const id = deviceId;
    const renew = () =>
      setLive(15).catch((err) => {
        message = err.message;
        live = false;
      });
    renew();
    const timer = setInterval(renew, 10000);
    return () => {
      clearInterval(timer);
      api(`/devices/${enc(id)}/live`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds: 0 }),
      }).catch(() => {});
    };
  });

  async function capture() {
    busy = true;
    message = null;
    try {
      await api(`/devices/${encodeURIComponent(deviceId)}/capture`, { method: "POST" });
      message = "Asked for a fresh picture; it shows up here in a few seconds.";
      setTimeout(load, 1500);
      setTimeout(load, 4000);
    } catch (err) {
      message = err.message;
    } finally {
      busy = false;
    }
  }
</script>

{#if !hasCamera}
  <p class="note">
    This device has no camera turned on: it joined as {roles.length ? roles.join(", ") : "no role"}. Turn on
    <code>camera.enabled</code> under <strong>Status and settings</strong> (if it has a camera); the device then
    restarts and joins with its camera.
  </p>
{/if}
<div class="bar">
  <button onclick={capture} disabled={busy || !connected || !hasCamera || live}>Take a picture now</button>
  <label class="live">
    <input type="checkbox" bind:checked={live} disabled={!connected || !hasCamera} />
    Live
  </label>
  {#if info}<span class="muted">newest picture: {Math.round(info.age_s)} s ago ({info.reason})</span>{/if}
  {#if live}<span class="muted">pictures in a row while this view is open (as fast as the device manages, at most 2 a second); they use mobile data</span>{/if}
</div>
{#if message}<p class="muted">{message}</p>{/if}
{#if info && src}
  <img {src} alt={`What ${deviceId} sees`} />
{:else}
  <p class="note">
    No picture from this device yet. Pictures arrive while vision is on for its conversation; the server keeps
    only the newest one in memory.
  </p>
{/if}

<style>
  .bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
    margin-bottom: 8px;
  }
  .live {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  img {
    display: block;
    max-width: 100%;
    max-height: 70vh;
    border-radius: 10px;
    border: 1px solid var(--line);
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
</style>
