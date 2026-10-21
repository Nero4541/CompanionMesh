<script>
  import { api, ApiError, getToken } from "../lib/api.js";
  import { poll } from "../lib/poll.svelte.js";

  let { onAuthError } = $props();

  const { state: info, refresh } = poll("/people", 2000, (err) => onAuthError?.(err));
  const { state: engines, refresh: refreshEngines } = poll("/builtin", 4000, (err) =>
    onAuthError?.(err),
  );

  let message = $state(null);
  let busy = $state({});
  let logOpen = $state(null);
  let draft = $state({ name: "", role: "acquaintance", notes: "", consent: false });
  let promote = $state(null); // {track_id, name, role, consent}
  let camera = $state("");
  let editing = $state(null); // {id, name, role, notes}
  let identifyResult = $state(null);
  let recorder = $state(null); // {id, media, chunks, started}
  const canRecord = typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia &&
    typeof MediaRecorder !== "undefined" && window.isSecureContext;

  const STATES = {
    off: ["Off", ""],
    not_installed: ["Not installed", "warn"],
    installing: ["Installing…", "live"],
    starting: ["Starting…", "live"],
    running: ["Running", "live"],
    failed: ["Failed", "warn"],
  };
  const ROLE = { user: "the user", acquaintance: "acquaintance", stranger: "stranger" };

  const cameras = $derived(info.data?.cameras ?? []);
  $effect(() => {
    if (!camera && cameras.length) camera = cameras[0];
  });

  async function act(key, run, ok) {
    busy = { ...busy, [key]: true };
    message = null;
    try {
      const result = await run();
      if (ok) message = { kind: "ok", text: typeof ok === "function" ? ok(result) : ok };
    } catch (err) {
      if (err.isAuth) onAuthError?.(err);
      message = { kind: "error", text: err.message };
    } finally {
      busy = { ...busy, [key]: false };
      refresh();
      refreshEngines();
    }
  }

  const json = (method, body) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  function toggleEngine(enabled) {
    act("engine", () => api("/builtin/perception", json("PUT", { enabled })),
      enabled ? "Perception is on: the engine is starting." : "Perception is off; its memory is free.");
  }

  function installEngine() {
    act("engine", () => api("/builtin/perception/install", { method: "POST" }),
      "Installing the perception engine (a few GB); it starts by itself when perception is on.");
  }

  function addPerson(event) {
    event.preventDefault();
    act("add", async () => {
      const person = await api("/people", json("POST", draft));
      draft = { name: "", role: "acquaintance", notes: "", consent: false };
      return person;
    }, (p) => `${p.name} added. Now add their face (and voice).`);
  }

  function addFace(person) {
    act(`face:${person.id}`, () => api(`/people/${person.id}/face`, json("POST", { camera })),
      (r) => `${person.name}: ${r.found} face samples found, ${r.face_samples} kept.`);
  }

  function savePromote(event) {
    event.preventDefault();
    act("promote", async () => {
      const person = await api("/people/promote", json("POST", promote));
      promote = null;
      return person;
    }, (p) => `${p.name} added from the face in view.`);
  }

  async function upload(person, blob) {
    await act(`voice:${person.id}`, async () => {
      const headers = {};
      const token = getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
      const res = await fetch(`/v1/admin/people/${person.id}/voice`, { method: "POST", headers, body: blob });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new ApiError(res.status, body.detail ?? res.statusText);
      return body;
    }, (r) => `${person.name}: ${r.seconds} s of speech, ${r.kept} clips kept` +
      (r.left_out ? `, ${r.left_out} left out (another voice?)` : "") + `; ${r.voice_samples} in all.`);
  }

  function pickVoice(person, event) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file) upload(person, file);
  }

  async function record(person) {
    if (recorder) {
      recorder.media.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const media = new MediaRecorder(stream);
      const chunks = [];
      media.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      media.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        recorder = null;
        upload(person, new Blob(chunks, { type: media.mimeType }));
      };
      media.start();
      recorder = { id: person.id, media, started: Date.now() };
    } catch (err) {
      message = { kind: "error", text: `cannot use the microphone: ${err.message}` };
    }
  }

  function clear(person, kind) {
    if (!confirm(`Delete all ${kind} samples of ${person.name}?`)) return;
    act(`${kind}:${person.id}`, () => api(`/people/${person.id}/${kind}`, { method: "DELETE" }));
  }

  function remove(person) {
    if (!confirm(`Forget ${person.name}? Their face and voice samples are deleted at once.`)) return;
    act(`del:${person.id}`, () => api(`/people/${person.id}`, { method: "DELETE" }), `${person.name} forgotten.`);
  }

  function saveEdit(event) {
    event.preventDefault();
    const { id, ...change } = editing;
    act(`edit:${id}`, async () => {
      await api(`/people/${id}`, json("PATCH", change));
      editing = null;
    });
  }

  function identify() {
    act("identify", async () => {
      identifyResult = await api(`/people/identify?camera=${encodeURIComponent(camera)}`, { method: "POST" });
    });
  }

  const ago = (s) => (s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
  const since = (t) => new Date(t * 1000).toLocaleTimeString();
</script>

<div class="head">
  <h2>People</h2>
  <span class="muted">
    Who the companion knows by face and voice: you (the user) and acquaintances. Everyone else is a
    stranger. Face and voice samples stay on this machine, in <code>data/people</code>; the agent only
    ever gets names.
  </span>
</div>

{#if message}
  <p class={message.kind === "error" ? "error" : "banner"} role="status">{message.text}</p>
{/if}
{#if info.error && !info.data}<p class="error">{info.error.message}</p>{/if}

{#if info.data}
  {@const eng = engines.data?.engines?.perception}
  {@const [label, tone] = STATES[info.data.engine.state] ?? [info.data.engine.state, ""]}
  <section class="card">
    <div class="row top">
      <h3>Perception engine <span class="muted">· faces (InsightFace) and voices (pyannote)</span></h3>
      <span class="pill {tone}">{label}</span>
      <label class="switch">
        <input
          type="checkbox"
          checked={info.data.perception.enabled}
          disabled={busy.engine || info.data.engine.state === "installing" || !engines.data?.editable}
          onchange={(ev) => toggleEngine(ev.currentTarget.checked)}
        />
        {info.data.perception.enabled ? "On" : "Off"}
      </label>
    </div>
    {#if info.data.engine.error}<p class="error">{info.data.engine.error}</p>{/if}
    {#if info.data.perception.ready}
      <p class="muted small">
        {info.data.perception.analysed} pictures looked at{#if info.data.perception.ms_per_frame}, {info.data.perception.ms_per_frame} ms each{/if}.
        {#each Object.entries(info.data.perception.cameras) as [cid, c] (cid)}
          <br /><code>{cid}</code>: {c.fps} pictures a second{c.pulled ? " (pulled from the camera)" : ""}, {c.tracks} faces now
        {/each}
      </p>
      {#if info.data.engine.voice === false}
        <p class="warn-box">
          Voices are not recognised: {info.data.engine.voice_error}. The voice model
          (pyannote/speaker-diarization-community-1) is gated: accept its terms on Hugging Face and put
          <code>HF_TOKEN=…</code> in the server's <code>.env</code>, or download it into
          <code>builtin/perception/models/speaker-diarization-community-1</code>; then turn perception off and on.
        </p>
      {/if}
    {:else if info.data.perception.enabled && info.data.engine.state === "not_installed"}
      <p class="muted">It is on, but not installed yet: install it and it starts by itself.</p>
    {:else if !info.data.perception.enabled}
      <p class="muted">Off: nobody is recognised. Turn it on to see who is around.</p>
    {/if}
    <div class="row">
      <button
        class={info.data.engine.installed ? "small" : "primary"}
        disabled={busy.engine || info.data.engine.state === "installing"}
        onclick={installEngine}
      >
        {info.data.engine.state === "installing" ? "Installing…" : info.data.engine.installed ? "Install again" : "Install (several GB)"}
      </button>
      <span class="muted small">Settings: <code>perception</code> and <code>people</code> in Configuration</span>
    </div>
    {#if eng?.log?.length}
      <details
        open={logOpen ?? (eng.state === "installing" || eng.state === "failed")}
        ontoggle={(ev) => (logOpen = ev.currentTarget.open)}
      >
        <summary>{eng.state === "installing" ? "Install log" : "Log"}</summary>
        <pre>{eng.log.join("\n")}</pre>
      </details>
    {/if}
  </section>

  <section class="card">
    <h3>Here now</h3>
    {#if !info.data.here.length}
      <p class="muted">Nobody in view{info.data.perception.ready ? "" : " (perception is not running)"}.</p>
    {:else}
      <ul class="here">
        {#each info.data.here as p (p.person_id ?? p.track_id)}
          <li>
            <strong>{p.name ?? "A stranger"}</strong>
            <span class="muted">{ROLE[p.role]} · {p.camera} · since {since(p.since)} · seen {ago(p.last_seen_s)} ago</span>
            {#if p.role === "stranger"}
              <button class="small" onclick={() => (promote = { track_id: p.track_id, name: "", role: "acquaintance", consent: false })}>
                Add as a person…
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
    {#if promote}
      <form class="row" onsubmit={savePromote}>
        <input placeholder="Their name" bind:value={promote.name} aria-label="Name" />
        <select bind:value={promote.role} aria-label="Role">
          <option value="acquaintance">acquaintance</option>
          <option value="user">the user</option>
        </select>
        <label><input type="checkbox" bind:checked={promote.consent} /> They agreed to be recognised</label>
        <button class="primary" disabled={!promote.name.trim() || !promote.consent || busy.promote}>Add</button>
        <button type="button" class="small" onclick={() => (promote = null)}>Cancel</button>
      </form>
    {/if}
  </section>

  <section class="card">
    <h3>Known people</h3>
    {#if cameras.length}
      <p class="row muted small">
        Camera for faces:
        <select bind:value={camera} aria-label="Camera">
          {#each cameras as c (c)}<option value={c}>{c}</option>{/each}
        </select>
        <button class="small" disabled={busy.identify || !info.data.perception.ready} onclick={identify}>Who is this?</button>
      </p>
    {/if}
    {#if identifyResult}
      <p class="muted small">
        {#if !identifyResult.faces.length}No face in the newest picture.{/if}
        {#each identifyResult.faces as f, i (i)}
          {f.picture ? "A picture" : f.name ? `${f.name} (${ROLE[f.role]})` : `A stranger (closest: ${f.closest ?? "nobody"})`}
          · similarity {f.score}{i < identifyResult.faces.length - 1 ? "; " : ""}
        {/each}
      </p>
    {/if}
    {#if !info.data.people.length}
      <p class="muted">Nobody yet. Add yourself first, as the user.</p>
    {:else}
      <table>
        <thead>
          <tr><th>Name</th><th>Role</th><th class="num">Face</th><th class="num">Voice</th><th></th></tr>
        </thead>
        <tbody>
          {#each info.data.people as p (p.id)}
            <tr>
              <td>
                {#if editing?.id === p.id}
                  <form class="row" onsubmit={saveEdit}>
                    <input bind:value={editing.name} aria-label="Name" />
                    <select bind:value={editing.role} aria-label="Role">
                      <option value="acquaintance">acquaintance</option>
                      <option value="user">the user</option>
                    </select>
                    <input bind:value={editing.notes} placeholder="Notes" aria-label="Notes" />
                    <button class="small primary">Save</button>
                    <button type="button" class="small" onclick={() => (editing = null)}>Cancel</button>
                  </form>
                {:else}
                  <strong>{p.name}</strong>
                  {#if p.here}<span class="pill live">here ({p.camera})</span>{/if}
                  {#if p.notes}<br /><span class="muted small">{p.notes}</span>{/if}
                {/if}
              </td>
              <td>{ROLE[p.role]}</td>
              <td class="num">{p.face_samples}</td>
              <td class="num">{p.voice_clips} clips</td>
              <td class="actions">
                <button
                  class="small"
                  disabled={busy[`face:${p.id}`] || !info.data.perception.ready || !camera}
                  title="Look straight at the camera for about ten seconds, alone in view"
                  onclick={() => addFace(p)}
                >
                  {busy[`face:${p.id}`] ? "Look at the camera…" : "Add face"}
                </button>
                <label class="button small" title="A recording of only this person speaking, 30 to 60 s">
                  {busy[`voice:${p.id}`] ? "Checking…" : "Voice file…"}
                  <input type="file" accept="audio/*" capture hidden onchange={(ev) => pickVoice(p, ev)} />
                </label>
                {#if canRecord}
                  <button class="small" disabled={recorder && recorder.id !== p.id} onclick={() => record(p)}>
                    {recorder?.id === p.id ? "Stop and save" : "Record voice"}
                  </button>
                {/if}
                <button class="small" onclick={() => (editing = { id: p.id, name: p.name, role: p.role, notes: p.notes })}>Edit</button>
                {#if p.face_samples}<button class="small" onclick={() => clear(p, "face")}>Clear face</button>{/if}
                {#if p.voice_clips}<button class="small" onclick={() => clear(p, "voice")}>Clear voice</button>{/if}
                <button class="small" onclick={() => remove(p)}>Forget</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if recorder}
        <p class="muted">Recording… read something aloud for 30 to 60 seconds, alone, then press Stop and save.</p>
      {/if}
    {/if}
  </section>

  <section class="card">
    <h3>Add a person</h3>
    <form class="row" onsubmit={addPerson}>
      <input placeholder="Name" bind:value={draft.name} aria-label="Name" />
      <select bind:value={draft.role} aria-label="Role">
        <option value="acquaintance">acquaintance</option>
        <option value="user">the user (you)</option>
      </select>
      <input placeholder="Notes (optional)" bind:value={draft.notes} aria-label="Notes" />
      <label><input type="checkbox" bind:checked={draft.consent} /> They agreed to be recognised</label>
      <button class="primary" disabled={!draft.name.trim() || !draft.consent || busy.add}>Add</button>
    </form>
    <p class="muted small">
      Then: <strong>Add face</strong> (the person looks at the chosen camera for about ten seconds, alone
      in view) and a voice recording of them speaking alone for 30 to 60 seconds (<strong>Voice file…</strong>,
      any common format, or <strong>Record voice</strong> where the browser allows the microphone). A
      phone on the tailnet can do the same at <code>/view/</code> with the admin token.
    </p>
  </section>
{:else if !info.error}
  <p class="muted">Loading…</p>
{/if}

<style>
  section {
    margin-bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  h3 {
    margin: 0;
  }
  p {
    margin: 0;
  }
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .top h3 {
    flex: 1;
  }
  .switch {
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
  }
  .here {
    margin: 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
  }
  label.button {
    display: inline-flex;
    align-items: center;
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 2px 8px;
    cursor: pointer;
    font-size: 13px;
  }
  .small {
    font-size: 13px;
  }
  .banner {
    border: 1px solid var(--ok);
    border-radius: 8px;
    padding: 6px 10px;
  }
  .warn-box {
    border: 1px solid var(--warn, #c80);
    border-radius: 8px;
    padding: 6px 10px;
  }
  pre {
    max-height: 240px;
    overflow: auto;
    font-size: 12px;
  }
</style>
