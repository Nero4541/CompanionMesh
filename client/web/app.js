// Companion dev client: text, half-duplex voice and opt-in camera over /v1/realtime.
const $ = (id) => document.getElementById(id);
const SESSION_KEY = "companion.session_id";
const TOKEN_KEY = "companion.token";
// Open the page once as /dev/?token=... when the server requires a token;
// it is remembered in this browser and removed from the address bar.
(() => {
  const url = new URL(location.href);
  const token = url.searchParams.get("token");
  if (token) {
    localStorage.setItem(TOKEN_KEY, token);
    url.searchParams.delete("token");
    history.replaceState(null, "", url);
  }
})();
const DEVICE_ID = "web-dev";
const enc = new TextEncoder();
const dec = new TextDecoder();

let ws = null;
let sessionId = localStorage.getItem(SESSION_KEY) || null;
let audioCtx = null;
let mic = null;            // { stream, source, node }
let micOn = false;
let assistantEl = null;    // message element being streamed into
let cam = null;            // { stream, timer }
let visionAvailable = false;
let visionOn = false;
let quietOn = false;
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
  $("cam-toggle").disabled = !on || !visionAvailable;
  $("vision-state").disabled = !on || !visionAvailable;
  $("quiet").disabled = !on;
}

function setQuiet(enabled) {
  quietOn = enabled;
  $("quiet").textContent = enabled ? "quiet on" : "quiet off";
  $("quiet").className = `pill ${enabled ? "on" : "off"}`;
}

function showDevices(devices) {
  const others = (devices || []).filter((d) => d.device_id !== DEVICE_ID);
  $("devices").textContent = others.length
    ? `also connected: ${others.map((d) => `${d.device_id || "device"} (${d.roles.join(", ")})`).join("; ")}`
    : "";
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
  const token = localStorage.getItem(TOKEN_KEY);
  const query = token ? `?token=${encodeURIComponent(token)}` : "";
  ws = new WebSocket(`${proto}://${location.host}/v1/realtime${query}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => {
    setConn(true);
    send("session.start", { session_id: sessionId, roles: ["mic", "speaker", "camera"] });
  };
  ws.onclose = (e) => {
    setConn(false);
    setState("idle");
    stopMic();
    stopCamera(false);
    addMsg("system", e.code === 1008
      ? "disconnected: the server needs a token. Open this page as /dev/?token=…"
      : `disconnected (${e.code})`);
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
      visionAvailable = Boolean(p.vision);
      $("cam-toggle").disabled = !visionAvailable;
      $("vision-state").disabled = !visionAvailable;
      setVision(Boolean(p.vision_enabled));
      setQuiet(Boolean(p.quiet));
      showDevices(p.devices);
      $("cam-toggle").title = visionAvailable ? "Opt in to vision for this session"
        : "No vision model configured on the server";
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
    case "session.devices":
      showDevices(p.devices);
      break;
    case "vision.state":
      setVision(p.enabled);
      if (!p.enabled && cam) stopCamera(false);
      break;
    case "attention.state":
      setQuiet(p.quiet);
      break;
    case "attention.decision":
      addMsg("attention", `🧭 ${p.decision}: ${p.kind} (${p.reasons.join("; ")})`);
      break;
    case "vision.capture.request":
      // The server wants a fresh look (you started talking); answer with our camera.
      if (cam) captureFrame("manual");
      break;
    case "vision.observation":
      addMsg("vision", `👁 ${p.description}${p.tags?.length ? `  [${p.tags.join(", ")}]` : ""}`);
      break;
    case "vision.frame.status":
      if (p.status === "rejected") addMsg("error", `frame rejected: ${p.detail}`);
      break;
    case "vision.frame.used":
      addMsg("vision", "📷 camera image sent with this message");
      break;
    case "conversation.transcript":
      addMsg("user", p.text);
      break;
    case "conversation.response.start":
      assistantEl = addMsg(p.proactive ? "assistant proactive" : "assistant", "");
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

// --- camera ----------------------------------------------------------------

const MAX_FRAME_SIDE = 768;

function setVision(enabled) {
  visionOn = enabled;
  $("vision-state").textContent = enabled ? "vision on" : "vision off";
  $("vision-state").className = `pill ${enabled ? "on" : "off"}`;
}

async function listCameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const select = $("cam");
  const current = select.value;
  select.replaceChildren(new Option("default", ""));
  for (const d of devices.filter((d) => d.kind === "videoinput")) {
    select.append(new Option(d.label || `camera ${select.length}`, d.deviceId));
  }
  select.value = current;
}

// Grab the current video frame, downscale and send it as a JPEG vision.frame.
async function captureFrame(reason) {
  if (!cam) return;
  const video = $("preview");
  if (!video.videoWidth) return;
  const scale = Math.min(1, MAX_FRAME_SIDE / Math.max(video.videoWidth, video.videoHeight));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
  if (!blob || !cam) return;
  sendBinary("vision.frame", { mime: "image/jpeg", reason, frame_id: crypto.randomUUID() },
             await blob.arrayBuffer());
}

function scheduleCapture() {
  if (!cam) return;
  clearInterval(cam.timer);
  const seconds = Number($("cam-interval").value);
  cam.timer = seconds > 0 ? setInterval(() => captureFrame("periodic"), seconds * 1000) : null;
}

async function startCamera() {
  const deviceId = $("cam").value;
  const stream = await navigator.mediaDevices.getUserMedia({
    video: { deviceId: deviceId ? { exact: deviceId } : undefined,
             width: { ideal: 1280 }, height: { ideal: 720 } },
  });
  const video = $("preview");
  video.srcObject = stream;
  video.hidden = false;
  cam = { stream, timer: null };
  send("vision.enable", {});
  scheduleCapture();
  $("snap").disabled = false;
  $("cam-toggle").textContent = "Stop camera";
  $("cam-toggle").classList.add("active");
  await listCameras();
}

function stopCamera(notify = true) {
  if (cam) {
    clearInterval(cam.timer);
    for (const t of cam.stream.getTracks()) t.stop();
    cam = null;
    if (notify) send("vision.disable", {});
  }
  const video = $("preview");
  video.srcObject = null;
  video.hidden = true;
  $("snap").disabled = true;
  $("cam-toggle").textContent = "Start camera";
  $("cam-toggle").classList.remove("active");
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
$("composer").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text) return;
  ensureAudio(); // user gesture: unlock audio playback
  $("text").value = "";
  addMsg("user", text);
  send("conversation.text", { text });
};
$("cam-toggle").onclick = async () => {
  try {
    if (cam) stopCamera(); else await startCamera();
  } catch (err) {
    addMsg("error", `camera: ${err.message || err}`);
    stopCamera();
  }
};
$("cam").onchange = async () => {
  if (cam) { stopCamera(); await startCamera(); }
};
$("cam-interval").onchange = scheduleCapture;
$("snap").onclick = () => captureFrame("manual");
$("vision-state").onclick = () => send(visionOn ? "vision.disable" : "vision.enable", {});
$("quiet").onclick = () => send("attention.quiet", { enabled: !quietOn });

navigator.mediaDevices?.addEventListener?.("devicechange", () => {
  listMics().catch(() => {});
  listCameras().catch(() => {});
});
listMics().catch(() => {});
listCameras().catch(() => {});
connect();
