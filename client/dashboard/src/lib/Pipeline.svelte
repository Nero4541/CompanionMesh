<script>
  // The voice pipeline of every connected device, stage by stage, with self-tests and the
  // switches for the phone and for pictures from boards.
  import { api } from "./api.js";
  import { setPath } from "./schema.js";
  import { poll } from "./poll.svelte.js";

  let { onAuthError } = $props();

  const { state: res, refresh } = poll("/pipeline", 2000, (err) => onAuthError?.(err));
  let tests = $state({}); // device id -> {running, kind, result, error}
  let switches = $state(null); // {share_session, answer_on, listen, from_devices}
  let saving = $state(false);
  let switchError = $state(null);

  const STAGES = [
    ["mic", "Microphone"],
    ["vad", "Speech"],
    ["stt", "Words"],
    ["agent", "Answer"],
    ["tts", "Voice"],
    ["speaker", "Speaker"],
  ];

  function stage(p, d, name) {
    return ["stt", "agent", "tts"].includes(name) ? p[name] : d[name];
  }

  const ago = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`);

  async function voiceTest(d) {
    const id = d.device_id;
    tests[id] = { running: true, kind: "voice" };
    try {
      const result = await api(`/devices/${encodeURIComponent(id)}/voice-test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seconds: 15 }),
      });
      tests[id] = { kind: "voice", result };
    } catch (err) {
      tests[id] = { kind: "voice", error: err.message };
      if (err.isAuth) onAuthError?.(err);
    }
    refresh();
  }

  async function answerTest(d) {
    const id = d.device_id;
    tests[id] = { running: true, kind: "answer" };
    try {
      const r = await api(`/devices/${encodeURIComponent(id)}/speaker-test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "This is a pipeline test.", confirm_s: 20 }),
      });
      const steps = [
        { stage: "tts", status: "ok", detail: `voice made in ${r.tts_s} s` },
        {
          stage: "speaker",
          status: r.played ? "ok" : "fail",
          detail: r.played ? "the device says it played it" : "the device did not say it played it within 20 s",
        },
      ];
      tests[id] = { kind: "answer", result: { ok: r.played, steps } };
    } catch (err) {
      tests[id] = { kind: "answer", error: err.message };
      if (err.isAuth) onAuthError?.(err);
    }
    refresh();
  }

  async function loadSwitches() {
    try {
      const cfg = await api("/config");
      const saved = cfg.saved ?? cfg.running;
      switches = {
        share_session: saved.phone?.share_session ?? true,
        answer_on: saved.phone?.answer_on ?? "where_said",
        listen: saved.phone?.listen ?? "push_to_talk",
        from_devices: saved.vision?.from_devices ?? true,
        editable: cfg.editable,
      };
    } catch (err) {
      switchError = err.message;
      if (err.isAuth) onAuthError?.(err);
    }
  }

  async function save(path, value) {
    saving = true;
    switchError = null;
    try {
      const cfg = await api("/config");
      const draft = cfg.saved ?? cfg.running;
      setPath(draft, path, value);
      await api("/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: draft }),
      });
    } catch (err) {
      switchError = err.message;
      if (err.isAuth) onAuthError?.(err);
    } finally {
      saving = false;
      await loadSwitches();
      refresh();
    }
  }

  $effect(() => {
    loadSwitches();
  });

  const p = $derived(res.data);
  const isPhone = (d) => d.device_id === "phone";
</script>

<div class="card wide">
  <h3>Voice pipeline</h3>
  {#if res.error}<p class="error">{res.error.message}</p>{/if}
  {#if p}
    {#if !p.devices.length}
      <p class="muted">No microphone or speaker is connected; the server's part:</p>
      <ol class="chain">
        {#each STAGES.filter(([key]) => ["stt", "agent", "tts"].includes(key)) as [key, label]}
          {@const s = p[key]}
          <li class="stage {s.status}">
            <span class="label"><span class="dot"></span>{label}</span>
            <span class="detail">{s.detail}</span>
            {#if s.age_s != null}<span class="muted small">{ago(s.age_s)}</span>{/if}
          </li>
        {/each}
      </ol>
    {/if}
    {#each p.devices as d (d.session_id + d.device_id)}
      {@const t = tests[d.device_id]}
      <div class="device">
        <div class="name">
          <code>{d.device_id ?? "device"}</code>
          <span class="muted small">in {d.session_id.slice(0, 8)}</span>
        </div>
        <ol class="chain">
          {#each STAGES as [key, label]}
            {@const s = stage(p, d, key)}
            <li class="stage {s.status}" title={s.last_failure ? `last failure ${ago(s.last_failure.age_s)}: ${s.last_failure.detail}` : ""}>
              <span class="label"><span class="dot"></span>{label}</span>
              <span class="detail">{s.detail}</span>
              {#if s.age_s != null}<span class="muted small">{ago(s.age_s)}</span>{/if}
            </li>
          {/each}
        </ol>
        <div class="row">
          {#if d.roles.includes("mic") || d.listen}
            <button class="small primary" onclick={() => voiceTest(d)} disabled={t?.running}>
              {t?.running && t.kind === "voice" ? "Speak now…" : "Test voice input"}
            </button>
          {/if}
          {#if d.roles.includes("speaker")}
            <button class="small" onclick={() => answerTest(d)} disabled={t?.running}>
              {t?.running && t.kind === "answer" ? "Playing…" : "Test the answer"}
            </button>
          {/if}
          {#if t?.running && t.kind === "voice"}
            <span class="muted small">
              {isPhone(d) && d.listen === "push_to_talk"
                ? "Hold to talk on the phone and say a short sentence (15 s)."
                : `Say a short sentence near ${d.device_id ?? "the device"} (15 s). It is recognised, not answered.`}
            </span>
          {/if}
        </div>
        {#if t?.error}<p class="error">{t.error}</p>{/if}
        {#if t?.result}
          <ul class="result">
            <li class={t.result.ok ? "ok" : "fail"}>
              <strong>{t.kind === "voice" ? "Voice input" : "Answer"}: {t.result.ok ? "works" : "does not work"}</strong>
            </li>
            {#each t.result.steps as step}
              <li class={step.status}><span class="dot"></span>{step.stage}: {step.detail}</li>
            {/each}
          </ul>
        {/if}
      </div>
    {/each}
  {:else if !res.error}
    <p class="muted">Loading…</p>
  {/if}

  {#if switches}
    <div class="switches">
      <label>
        <input type="checkbox" checked={switches.share_session} disabled={saving || !switches.editable}
          onchange={(e) => save("phone.share_session", e.currentTarget.checked)} />
        The phone joins the board's conversation
      </label>
      <label>
        Answers play
        <select value={switches.answer_on} disabled={saving || !switches.editable}
          onchange={(e) => save("phone.answer_on", e.currentTarget.value)}>
          <option value="where_said">where they were heard</option>
          <option value="board">on the board</option>
          <option value="phone">on the phone</option>
          <option value="both">on both</option>
        </select>
      </label>
      <label>
        The phone listens
        <select value={switches.listen} disabled={saving || !switches.editable}
          onchange={(e) => save("phone.listen", e.currentTarget.value)}>
          <option value="push_to_talk">while its button is held</option>
          <option value="always">all the time</option>
        </select>
      </label>
      <label>
        <input type="checkbox" checked={switches.from_devices} disabled={saving || !switches.editable}
          onchange={(e) => save("vision.from_devices", e.currentTarget.checked)} />
        Take pictures from boards
      </label>
    </div>
  {/if}
  {#if switchError}<p class="error">{switchError}</p>{/if}
</div>

<style>
  .card { display: flex; flex-direction: column; gap: 10px; }
  .wide { grid-column: 1 / -1; }
  h3 { margin: 0; }
  .device { display: flex; flex-direction: column; gap: 6px; padding-top: 8px; border-top: 1px solid var(--line); }
  .name { display: flex; gap: 8px; align-items: baseline; }
  .chain { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 6px; margin: 0; padding: 0; list-style: none; }
  .stage { display: flex; flex-direction: column; gap: 2px; padding: 6px 8px; border: 1px solid var(--line); border-radius: 8px; min-width: 0; }
  .label { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; }
  .detail { font-size: 13px; overflow-wrap: anywhere; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--muted); flex: none; display: inline-block; margin-right: 4px; }
  .ok .dot { background: var(--ok); }
  .warn .dot { background: var(--warn); }
  .fail .dot { background: var(--err); }
  .stage.ok { border-color: var(--ok); }
  .stage.warn { border-color: var(--warn); }
  .stage.fail { border-color: var(--err); }
  .stage.off { opacity: 0.6; }
  .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .result { margin: 0; padding: 0; list-style: none; font-size: 13px; display: flex; flex-direction: column; gap: 2px; }
  .result .ok strong { color: var(--ok); }
  .result .fail strong { color: var(--err); }
  .switches { display: flex; flex-wrap: wrap; gap: 8px 18px; padding-top: 8px; border-top: 1px solid var(--line); font-size: 14px; }
  .switches label { display: flex; align-items: center; gap: 6px; }
  .small { font-size: 12px; }
</style>
