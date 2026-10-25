<script>
  import { api } from "../lib/api.js";
  import { duration, time, value } from "../lib/format.js";
  import PhoneLogin from "../lib/PhoneLogin.svelte";
  import Pipeline from "../lib/Pipeline.svelte";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  const { state: res } = poll("/status", 5000, (err) => onAuthError?.(err));
  let health = $state(null);
  let probing = $state(false);

  async function probe() {
    probing = true;
    try {
      health = (await api("/status?probe=true")).agent_health;
    } catch (err) {
      health = `failed: ${err.message}`;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      probing = false;
    }
  }

  const s = $derived(res.data);
</script>

<div class="head">
  <h2>Overview</h2>
  {#if res.updated}<span class="muted">updated {time(res.updated)}</span>{/if}
</div>

{#if res.error}
  <p class="error">{res.error.message}</p>
{/if}

{#if s}
  <div class="cards">
    <div class="card">
      <h3>Server</h3>
      <dl>
        <dt>Version</dt><dd>{s.version}</dd>
        <dt>Uptime</dt><dd>{duration(s.uptime_s)}</dd>
        <dt>Persona</dt><dd>{s.persona}</dd>
        <dt>Transcripts</dt><dd class="wrap">{s.transcripts}</dd>
        <dt>Stored sessions</dt><dd>{s.stored_sessions}</dd>
      </dl>
    </div>

    <div class="card">
      <h3>Providers</h3>
      <dl>
        <dt>Agent</dt>
        <dd>
          {s.providers.agent}
          {#if health}
            <span class="pill" class:ok={health.startsWith("ok")} class:warn={!health.startsWith("ok")}
              >{health}</span
            >
          {/if}
          <button class="small" onclick={probe} disabled={probing}>
            {probing ? "Checking…" : "Check"}
          </button>
        </dd>
        <dt>STT</dt>
        <dd>{s.providers.stt ?? "off"}{s.providers.stt_device ? ` (${s.providers.stt_device})` : ""}</dd>
        <dt>TTS</dt><dd>{s.providers.tts ?? "off"}</dd>
        <dt>VLM</dt><dd>{s.providers.vlm ?? "off"}</dd>
      </dl>
    </div>

    <div class="card">
      <h3>Vision</h3>
      <dl>
        <dt>Mode</dt><dd>{s.vision.mode}</dd>
        <dt>Available</dt>
        <dd><span class="pill" class:ok={s.vision.enabled}>{s.vision.enabled ? "yes" : "no"}</span></dd>
        <dt>Stores images</dt><dd>{s.vision.store_images ? "yes" : "no"}</dd>
        <dt>Sessions using it</dt><dd>{s.vision.active_sessions}</dd>
      </dl>
    </div>

    <PhoneLogin {onAuthError} />

    <Pipeline {onAuthError} />
  </div>

  <h3 class="table-title">Sessions <span class="muted">{s.active_sessions} active</span></h3>
  {#if s.sessions.length}
    <div class="scroll">
      <table>
        <thead><tr><th>Session</th><th>State</th><th>Devices</th></tr></thead>
        <tbody>
          {#each s.sessions as session (session.session_id)}
            <tr>
              <td><code>{session.session_id}</code></td>
              <td>{session.state}</td>
              <td>
                {s.devices
                  .filter((d) => d.session_id === session.session_id)
                  .map((d) => d.device_id)
                  .join(", ") || "–"}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="muted">No session is open.</p>
  {/if}

  <h3 class="table-title">Devices <span class="muted">{s.devices.length} connected</span></h3>
  {#if s.devices.length}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Device</th><th>Roles</th><th>Connected</th><th class="num">RTT</th>
            <th class="num">Clock offset</th><th>Last status</th>
          </tr>
        </thead>
        <tbody>
          {#each s.devices as d (d.session_id + d.device_id)}
            <tr>
              <td><code>{d.device_id}</code></td>
              <td>{d.roles.join(", ")}</td>
              <td>{duration(d.connected_s)}</td>
              <td class="num">{d.rtt_ms == null ? "–" : `${d.rtt_ms} ms`}</td>
              <td class="num">{d.clock_offset_ms == null ? "–" : `${d.clock_offset_ms} ms`}</td>
              <td class="status">
                {#if d.status}
                  <code title={value(d.status)}>{value(d.status)}</code>
                  <span class="muted">{duration(d.status_age_s)} ago</span>
                {:else}–{/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="muted">No device is connected.</p>
  {/if}
{:else if !res.error}
  <p class="muted">Loading…</p>
{/if}

<style>
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px;
  }
  dl {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 6px 12px;
    margin: 0;
  }
  dt {
    color: var(--muted);
  }
  dd {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
  }
  .wrap {
    overflow-wrap: anywhere;
  }
  .table-title {
    margin: 24px 0 8px;
  }
  .status code {
    display: inline-block;
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: bottom;
  }
</style>
