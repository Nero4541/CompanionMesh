// Companion dev client: text + half-duplex voice over /v1/realtime.
const $ = (id) => document.getElementById(id);
const SESSION_KEY = "companion.session_id";
const DEVICE_ID = "web-dev";
const enc = new TextEncoder();
const dec = new TextDecoder();

let ws = null;
let sessionId = localStorage.getItem(SESSION_KEY) || null;
let audioCtx = null;
let mic = null;            // { stream, source, node }
let micOn = false;
let assistantEl = null;    // message element being streamed into
let toolEls = new Map();

// --- UI helpers ------------------------------------------------------------

function addMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  el.textContent = text;
  $("log").append(el);
  el.scrollIntoView({ block: "end" });
  return el;
}

function setConn(on) {
  $("conn").textContent = on ? "connected" : "disconnected";
  $("conn").className = `pill ${on ? "on" : "off"}`;
  $("connect").textContent = on ? "Disconnect" : "Connect";
  for (const id of ["text", "send", "mic-toggle", "cancel"]) $(id).disabled = !on;
}

// Show how long the agent has been working; local models can take a while.
let stateTimer = null;
function setState(state) {
  clearInterval(stateTimer);
  $("state").textContent = state;
  if (state === "thinking" || state === "transcribing") {
    const started = Date.now();
    stateTimer = setInterval(() => {
      $("state").textContent = `${state} ${Math.round((Date.now() - started) / 1000)}s`;
    }, 1000);
  }
}

// --- protocol --------------------------------------------------------------

function envelope(type, payload = {}) {
  return { type, session_id: sessionId, device_id: DEVICE_ID,
           timestamp: new Date().toISOString(), payload };
}

function send(type, payload) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(envelope(type, payload)));
}

function sendBinary(type, payload, bytes) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const header = enc.encode(JSON.stringify(envelope(type, payload)));
  const frame = new Uint8Array(4 + header.length + bytes.byteLength);
  new DataView(frame.buffer).setUint32(0, header.length, false);
  frame.set(header, 4);
  frame.set(new Uint8Array(bytes), 4 + header.length);
  ws.send(frame);
}

function decodeBinary(buf) {
  const view = new DataView(buf);
  const len = view.getUint32(0, false);
  const header = JSON.parse(dec.decode(new Uint8Array(buf, 4, len)));
  return [header, buf.slice(4 + len)];
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/v1/realtime`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setConn(true);
    send("session.start", { session_id: sessionId });
  };
  ws.onclose = (e) => {
    setConn(false);
    setState("idle");
    stopMic();
    addMsg("system", `disconnected (${e.code})`);
    ws = null;
  };
  ws.onmessage = (e) => {
    if (typeof e.data === "string") onEvent(JSON.parse(e.data));
    else onBinary(...decodeBinary(e.data));
  };
}

function onEvent(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "session.started":
      sessionId = p.session_id;
      localStorage.setItem(SESSION_KEY, sessionId);
      $("session").textContent = `session ${sessionId}${p.resumed ? " (resumed)" : ""} · persona ${p.persona}`;
      $("log").replaceChildren();
      for (const m of p.history || []) addMsg(m.role, m.content);
      if (!p.speech_input) $("mic-toggle").disabled = true;
      break;
    case "system.state":
      setState(p.state);
      break;
    case "system.error":
      addMsg("error", `${p.code}: ${p.message}`);
      break;
    case "audio.vad":
      $("meter-bar").style.background = p.state === "speech_start" ? "var(--ok)" : "";
      break;
    case "conversation.transcript":
      addMsg("user", p.text);
      break;
    case "conversation.response.start":
      assistantEl = addMsg("assistant", "");
      playback.begin(p.turn_id);
      break;
    case "conversation.response.delta":
      if (assistantEl) { assistantEl.textContent += p.text; assistantEl.scrollIntoView({ block: "end" }); }
      break;
    case "conversation.response.done":
      if (assistantEl) {
        if (!assistantEl.textContent) assistantEl.textContent = p.text || "…";
        if (p.cancelled) assistantEl.classList.add("cancelled");
      }
      assistantEl = null;
      toolEls = new Map();
      if (p.cancelled) playback.stop();
      break;
    case "agent.tool.progress": {
      const key = p.tool;
      if (p.status === "completed") { toolEls.get(key)?.remove(); break; }
      toolEls.set(key, addMsg("tool", `⚙ ${p.label || p.tool}`));
      break;
    }
    case "audio.output.done":
      playback.end(p.turn_id);
      break;
    default:
      break; // forward compatibility: ignore unknown events
  }
}

function onBinary(header, data) {
  if (header.type === "audio.output.chunk") playback.enqueue(header.payload, data);
}

// --- playback --------------------------------------------------------------

const playback = {
  turn: null, chain: Promise.resolve(), nextAt: 0, sources: [], ended: false, pending: 0,

  begin(turnId) { this.stop(); this.turn = turnId; this.ended = false; },

  enqueue(payload, data) {
    if (payload.turn_id !== this.turn) return;
    if (!$("tts").checked) return;
    const ctx = ensureAudio();
    this.pending++;
    // Decode in order, schedule back-to-back.
    this.chain = this.chain.then(async () => {
      try {
        const buf = await ctx.decodeAudioData(data);
        if (payload.turn_id !== this.turn) return;
        const src = ctx.createBufferSource();
        src.buffer = buf;
        src.connect(ctx.destination);
        const at = Math.max(ctx.currentTime + 0.02, this.nextAt);
        src.start(at);
        this.nextAt = at + buf.duration;
        this.sources.push(src);
        src.onended = () => {
          this.sources = this.sources.filter((s) => s !== src);
          this.maybeFinish();
        };
      } catch (err) {
        addMsg("error", `audio decode failed: ${err}`);
      } finally {
        this.pending--;
        this.maybeFinish();
      }
    });
  },

  end(turnId) {
    if (turnId !== this.turn) return;
    this.ended = true;
    this.maybeFinish();
  },

  maybeFinish() {
    if (this.ended && this.pending === 0 && this.sources.length === 0 && this.turn) {
      const turn = this.turn;
      this.turn = null;
      send("audio.output.played", { turn_id: turn });
    }
  },

  stop() {
    for (const s of this.sources) { try { s.stop(); } catch { /* already stopped */ } }
    this.sources = [];
    this.nextAt = 0;
  },
};

// --- microphone ------------------------------------------------------------

function ensureAudio() {
  if (!audioCtx) audioCtx = new AudioContext();
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}

async function listMics() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const select = $("mic");
  const current = select.value;
  select.replaceChildren(new Option("default", ""));
  for (const d of devices.filter((d) => d.kind === "audioinput")) {
    select.append(new Option(d.label || `mic ${select.length}`, d.deviceId));
  }
  select.value = current;
}

async function startMic() {
  const ctx = ensureAudio();
  const deviceId = $("mic").value;
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      deviceId: deviceId ? { exact: deviceId } : undefined,
      channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true,
    },
  });
  await ctx.audioWorklet.addModule("mic-worklet.js");
  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "mic-capture");
  node.port.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      sendBinary("audio.input.chunk", {}, e.data);
    } else if (e.data.level !== undefined) {
      $("meter-bar").style.width = `${Math.min(100, e.data.level * 400)}%`;
    }
  };
  source.connect(node);
  mic = { stream, source, node };
  micOn = true;
  send("audio.input.start", { sample_rate: 16000, encoding: "pcm_s16le", channels: 1 });
  $("mic-toggle").textContent = "Stop mic";
  $("mic-toggle").classList.add("active");
  await listMics(); // labels become available after permission is granted
}

function stopMic() {
  if (mic) {
    mic.node.port.onmessage = null;
    mic.source.disconnect();
    mic.node.disconnect();
    for (const t of mic.stream.getTracks()) t.stop();
    mic = null;
  }
  if (micOn) send("audio.input.stop", {});
  micOn = false;
  $("meter-bar").style.width = "0";
  $("mic-toggle").textContent = "Start mic";
  $("mic-toggle").classList.remove("active");
}

// --- wiring ----------------------------------------------------------------

$("connect").onclick = () => (ws ? ws.close() : connect());
$("new-session").onclick = () => {
  localStorage.removeItem(SESSION_KEY);
  sessionId = null;
  $("log").replaceChildren();
  $("session").textContent = "";
  if (ws) ws.close();
  connect();
};
$("mic-toggle").onclick = async () => {
  try {
    if (micOn) stopMic(); else await startMic();
  } catch (err) {
    addMsg("error", `microphone: ${err.message || err}`);
    stopMic();
  }
};
$("mic").onchange = async () => {
  if (micOn) { stopMic(); await startMic(); }
};
$("cancel").onclick = () => { playback.stop(); send("conversation.cancel", {}); };
$("composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  ensureAudio(); // user gesture: unlock audio playback
  addMsg("user", text);
  send("conversation.text", { text });
  $("text").value = "";
};

navigator.mediaDevices?.addEventListener?.("devicechange", listMics);
listMics().catch(() => {});
connect();
