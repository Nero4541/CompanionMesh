<script>
  import { api, apiImage } from "./api.js";

  let { deviceId, connected = true, roles = null } = $props();

  // null: not connected now (roles unknown); otherwise whether it joined as a camera.
  const hasCamera = $derived(roles === null || roles.includes("camera"));

  let src = $state(null);
  let info = $state(null); // {age_s, reason}
  let message = $state(null);
  let busy = $state(false);

  async function load() {
    try {
      const list = (await api("/frames")).frames;
      info = list.find((f) => f.device_id === deviceId) ?? null;
      if (!info) return;
      const url = await apiImage(`/devices/${encodeURIComponent(deviceId)}/frame`);
      if (src) URL.revokeObjectURL(src);
      src = url;
    } catch (err) {
      message = err.message;
    }
  }

  $effect(() => {
    deviceId;
    info = null;
    load();
    const timer = setInterval(() => {
      if (!document.hidden) load();
    }, 3000);
    return () => {
      clearInterval(timer);
      if (src) URL.revokeObjectURL(src);
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
  <button onclick={capture} disabled={busy || !connected || !hasCamera}>Take a picture now</button>
  {#if info}<span class="muted">newest picture: {Math.round(info.age_s)} s ago ({info.reason})</span>{/if}
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
