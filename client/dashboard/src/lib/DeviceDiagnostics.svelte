<script>
  import { api } from "./api.js";

  let { deviceId, connected = true } = $props();

  let result = $state(null);
  let error = $state(null);
  let busy = $state(false);

  $effect(() => {
    deviceId;
    result = null;
    error = null;
  });

  async function run() {
    busy = true;
    error = null;
    try {
      result = await api(`/devices/${encodeURIComponent(deviceId)}/diagnostics`, { method: "POST" });
    } catch (err) {
      error = err.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="bar">
  <button onclick={run} disabled={busy || !connected}>{busy ? "Asking the device…" : "Collect diagnostics"}</button>
  <span class="muted">
    Versions, the device's own checks, memory, disk, network, Wi-Fi and kernel messages. Read-only; nothing is
    changed on the device.
  </span>
</div>
{#if error}<p class="error">{error}</p>{/if}
{#if result}
  <p class="muted">Collected {new Date(result.at * 1000).toLocaleTimeString()}.</p>
  {#each Object.entries(result.sections) as [name, text], i}
    <details open={i < 2}>
      <summary>{name}</summary>
      <pre>{text}</pre>
    </details>
  {/each}
{/if}

<style>
  .bar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    align-items: center;
    margin-bottom: 8px;
  }
  details {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 6px 12px;
    margin-bottom: 6px;
  }
  summary {
    cursor: pointer;
    font-weight: 600;
  }
  pre {
    margin: 6px 0 0;
    font-size: 12px;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
