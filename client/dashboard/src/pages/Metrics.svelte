<script>
  import { latency, time } from "../lib/format.js";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  // Pipeline order; anything else the server records follows alphabetically.
  const ORDER = [
    "mic_to_vad",
    "vad_to_stt",
    "stt_to_first_token",
    "first_token_to_audio",
    "speech_to_audio",
    "network_rtt",
    "barge_in_to_stop",
    "frame_to_observation",
  ];

  const { state: res } = poll("/metrics", 5000, (err) => onAuthError?.(err));

  const stages = $derived.by(() => {
    const all = res.data?.stages ?? {};
    const names = [
      ...ORDER.filter((n) => n in all),
      ...Object.keys(all).filter((n) => !ORDER.includes(n)).sort(),
    ];
    return names.map((name) => ({ name, ...all[name] }));
  });
  const counters = $derived(Object.entries(res.data?.counters ?? {}).sort());
</script>

<div class="head">
  <h2>Latency</h2>
  {#if res.updated}<span class="muted">updated {time(res.updated)}</span>{/if}
</div>

{#if res.error}
  <p class="error">{res.error.message}</p>
{/if}

{#if res.data}
  {#if !res.data.enabled}
    <p class="note">Metrics are off (<code>metrics.enabled: false</code>).</p>
  {:else if !stages.length}
    <p class="muted">Nothing measured yet. Talk to the companion and the stages appear here.</p>
  {:else}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Stage</th><th class="num">Samples</th><th class="num">Last</th>
            <th class="num">p50</th><th class="num">p95</th><th class="num">Max</th>
          </tr>
        </thead>
        <tbody>
          {#each stages as st (st.name)}
            <tr class:bottleneck={st.name === res.data.bottleneck}>
              <td>
                <code>{st.name}</code>
                {#if st.name === res.data.bottleneck}<span class="pill warn">slowest</span>{/if}
              </td>
              <td class="num">{st.count}</td>
              <td class="num">{latency(st.last)}</td>
              <td class="num">{latency(st.p50)}</td>
              <td class="num">{latency(st.p95)}</td>
              <td class="num">{latency(st.max)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="muted">
      Percentiles over the last samples of each stage (<code>metrics.window</code>). "Slowest" is
      the pipeline stage with the highest median.
    </p>
  {/if}

  {#if counters.length}
    <h3 class="table-title">Counters</h3>
    <div class="scroll">
      <table>
        <tbody>
          {#each counters as [name, n] (name)}
            <tr><td><code>{name}</code></td><td class="num">{n}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{:else if !res.error}
  <p class="muted">Loading…</p>
{/if}

<style>
  .bottleneck td {
    background: var(--warn-bg);
  }
  .table-title {
    margin: 24px 0 8px;
  }
  .note {
    font-size: 13px;
    color: var(--muted);
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 8px 12px;
  }
</style>
