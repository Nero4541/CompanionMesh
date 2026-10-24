// Push to talk on the phone: hold the button, speak, let go. The sentence goes to the companion
// as said by the user (it needs the admin token, as the People tab does) and is answered through
// the board, or, with no board on (or if asked), on this phone itself: the page then joins the
// companion as a speaker of its own and plays the answer where the phone plays sound (Bluetooth
// earphones or a speaker paired with it too). Browsers open the microphone only on https: open
// this page at the server's tailnet name (tailscale serve), not at its 100.x address.
"use strict";

(() => {
  const TOKEN_KEY = "companion.admin.token";
  const OUT_KEY = "companion.talk.out";
  const SINK_KEY = "companion.talk.sink";
  const SESSION_KEY = "companion.phone.session";
  const DEVICE_ID = "phone";
  const RATE = 16000; // what the server's speech recognition takes
  const MAX_S = 30;
  const MIN_S = 0.3; // shorter: a tap, not a sentence
  const $ = (id) => document.getElementById(id);

  let stream = null;
  let context = null;
  let source = null;
  let node = null;
  let chunks = [];
  let recording = false;
  let wanted = false; // the finger is still on the button
  let startedAt = 0;
  let timer = null;

  function stored(key) {
    try {
      return localStorage.getItem(key) || "";
    } catch {
      return "";
    }
  }

  function keep(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {}
  }

  const token = () => stored(TOKEN_KEY);
  const say = (text) => ($("talk-state").textContent = text);

  async function start() {
    wanted = true;
    if (recording) return;
    if (!token()) {
      say("Sign in under People first (scan the QR code on the dashboard).");
      return;
    }
    try {
      stream ??= await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      context ??= new AudioContext(); // made while the finger is down: the answer may play through it
      await context.resume();
    } catch (err) {
      say(`No microphone: ${err.message}`);
      return;
    }
    outputs();
    if (!wanted) return; // let go while the browser asked for the microphone
    source = context.createMediaStreamSource(stream);
    node = context.createScriptProcessor(4096, 1, 1);
    chunks = [];
    node.onaudioprocess = (ev) => {
      if (recording) chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
    };
    source.connect(node);
    node.connect(context.destination);
    recording = true;
    startedAt = performance.now();
    $("talk-button").classList.add("on");
    say("Listening… let go to send");
    timer = setTimeout(stop, MAX_S * 1000);
  }

  async function stop() {
    wanted = false;
    if (!recording) return;
    recording = false;
    clearTimeout(timer);
    source.disconnect();
    node.disconnect();
    // An open microphone keeps a phone in call mode, which plays through the earpiece and
    // Bluetooth's call profile: given back after each sentence, the answer plays as media.
    closeMic();
    $("talk-button").classList.remove("on");
    if ((performance.now() - startedAt) / 1000 < MIN_S) {
      say("Hold the button while you speak.");
      return;
    }
    const wav = toWav(merge(chunks), context.sampleRate);
    say("Sending…");
    try {
      const session = await destination();
      const res = await fetch(`/v1/view/talk?session=${encodeURIComponent(session)}`, {
        method: "POST",
        headers: { "Content-Type": "audio/wav", Authorization: `Bearer ${token()}` },
        body: wav,
      });
      let body = {};
      try {
        body = await res.json();
      } catch {}
      if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
      const where = session === phone.session ? "on this phone" : "on the board";
      say(`Sent (${body.seconds} s); the companion answers ${where}.`);
    } catch (err) {
      say(err.message);
    }
  }

  function closeMic() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  // The microphone is given back while the page is not in view.
  function release() {
    if (recording) stop();
    closeMic();
  }

  // The conversation the sentence goes to: the board's, if it is to answer, else this phone's.
  async function destination() {
    if ($("talk-out").value === "auto") {
      try {
        const res = await fetch("/v1/view/state");
        const state = await res.json();
        const board = (state.devices || []).find(
          (d) => d.session_id && d.device_id !== DEVICE_ID && (d.roles || []).includes("speaker"),
        );
        if (board) {
          phone.hangUp(); // the board answers: this phone is no speaker meanwhile
          return board.session_id;
        }
      } catch {}
    }
    return phone.connect();
  }

  // --- this phone as the companion's speaker ---------------------------------------------

  const dec = new TextDecoder();

  const phone = {
    ws: null,
    ready: null, // the phone's session id, once the server has started it
    session: null,

    send(type, payload = {}) {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      const envelope = { type, session_id: this.session, device_id: DEVICE_ID, timestamp: new Date().toISOString(), payload };
      this.ws.send(JSON.stringify(envelope));
    },

    connect() {
      if (this.ready) return this.ready;
      this.ready = new Promise((resolve, reject) => {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(`${proto}://${location.host}/v1/view/realtime?token=${encodeURIComponent(token())}`);
        ws.binaryType = "arraybuffer";
        this.ws = ws;
        ws.onopen = () => this.send("session.start", { session_id: stored(SESSION_KEY) || undefined, roles: ["speaker"] });
        ws.onclose = (ev) => {
          if (this.ws === ws) {
            this.ws = null;
            this.ready = null;
            playback.stop();
          }
          reject(new Error(ev.code === 1008 ? "The server did not let this phone answer (sign in again under People)." : `The connection closed (${ev.code}).`));
        };
        ws.onmessage = (ev) => {
          if (typeof ev.data === "string") this.event(JSON.parse(ev.data), resolve);
          else this.binary(ev.data);
        };
      });
      this.ready.catch(() => {});
      return this.ready;
    },

    hangUp() {
      const ws = this.ws;
      this.ws = null;
      this.ready = null;
      playback.stop();
      ws?.close();
    },

    event(msg, started) {
      const p = msg.payload || {};
      switch (msg.type) {
        case "session.started":
          this.session = p.session_id;
          keep(SESSION_KEY, p.session_id);
          started(p.session_id);
          break;
        case "system.ping": // clock sync: answered with this phone's clock
          this.send("system.pong", { ...p, t_device: Date.now() });
          break;
        case "conversation.response.start":
          playback.begin(p.turn_id);
          break;
        case "conversation.response.done":
          if (p.cancelled) playback.stop();
          break;
        case "audio.output.done":
          playback.end(p.turn_id);
          break;
        case "audio.output.stop": // cut in, or cancelled: silence now, drop the rest
          playback.stopTurn(p.turn_id);
          this.send("audio.output.stopped", { turn_id: p.turn_id, reason: p.reason });
          break;
        case "system.error":
          say(`${p.code}: ${p.message}`);
          break;
        default:
          break; // what a speaker need not know
      }
    },

    binary(buf) {
      const view = new DataView(buf);
      const len = view.getUint32(0, false);
      const header = JSON.parse(dec.decode(new Uint8Array(buf, 4, len)));
      if (header.type === "audio.output.chunk") playback.enqueue(header.payload, buf.slice(4 + len));
    },
  };

  // Clips decoded in order and played back to back, as the dev client does.
  const playback = {
    turn: null,
    chain: Promise.resolve(),
    nextAt: 0,
    sources: [],
    ended: false,
    pending: 0,
    stopped: new Set(), // turns the server told us to stop; their late audio is dropped

    begin(turnId) {
      this.stop();
      this.turn = turnId;
      this.ended = false;
    },

    stopTurn(turnId) {
      this.stopped.add(turnId);
      if (turnId === this.turn) {
        this.stop();
        this.turn = null;
      }
    },

    enqueue(payload, data) {
      if (!context || payload.turn_id !== this.turn || this.stopped.has(payload.turn_id)) return;
      this.pending++;
      this.chain = this.chain.then(async () => {
        try {
          const buf = await context.decodeAudioData(data);
          if (payload.turn_id !== this.turn) return;
          const src = context.createBufferSource();
          src.buffer = buf;
          src.connect(context.destination);
          const at = Math.max(context.currentTime + 0.02, this.nextAt);
          src.start(at);
          this.nextAt = at + buf.duration;
          phone.send("audio.output.progress", { turn_id: payload.turn_id, seq: payload.seq, state: "started" });
          this.sources.push(src);
          src.onended = () => {
            this.sources = this.sources.filter((s) => s !== src);
            this.finish();
          };
        } catch (err) {
          say(`Cannot play the answer: ${err.message || err}`);
        } finally {
          this.pending--;
          this.finish();
        }
      });
    },

    end(turnId) {
      if (turnId !== this.turn) return;
      this.ended = true;
      this.finish();
    },

    finish() {
      if (this.ended && this.pending === 0 && this.sources.length === 0 && this.turn) {
        const turn = this.turn;
        this.turn = null;
        phone.send("audio.output.played", { turn_id: turn });
      }
    },

    stop() {
      for (const s of this.sources) {
        try {
          s.stop();
        } catch {}
      }
      this.sources = [];
      this.nextAt = 0;
    },
  };

  // Where on the phone the answer plays, where the browser lets a page choose (desktop Chrome
  // and Edge do; phones mostly play where the system does, Bluetooth included).
  async function outputs() {
    if (!context || typeof context.setSinkId !== "function") return;
    let found = [];
    try {
      found = (await navigator.mediaDevices.enumerateDevices()).filter(
        (d) => d.kind === "audiooutput" && d.deviceId && d.deviceId !== "default",
      );
    } catch {}
    if (!found.length) return;
    const select = $("talk-sink");
    const chosen = stored(SINK_KEY);
    select.replaceChildren(new Option("the phone's own choice", ""));
    for (const d of found) select.append(new Option(d.label || "speaker", d.deviceId));
    select.value = found.some((d) => d.deviceId === chosen) ? chosen : "";
    $("talk-sink-row").hidden = false;
    $("talk-sink-hint").hidden = true;
    await sink();
  }

  async function sink() {
    const id = $("talk-sink").value;
    keep(SINK_KEY, id);
    try {
      await context?.setSinkId?.(id);
    } catch (err) {
      say(`Cannot play there: ${err.message}`);
    }
  }

  function merge(parts) {
    const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const p of parts) {
      out.set(p, at);
      at += p.length;
    }
    return out;
  }

  // 16 kHz, 16-bit mono WAV (linear resampling is enough for speech recognition).
  function toWav(samples, rate) {
    const n = Math.floor((samples.length * RATE) / rate);
    const pcm = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      const x = (i * rate) / RATE;
      const j = Math.floor(x);
      const a = samples[j] ?? 0;
      const b = samples[j + 1] ?? a;
      const v = Math.max(-1, Math.min(1, a + (b - a) * (x - j)));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }
    const buf = new ArrayBuffer(44 + pcm.byteLength);
    const d = new DataView(buf);
    const text = (at, s) => [...s].forEach((c, k) => d.setUint8(at + k, c.charCodeAt(0)));
    text(0, "RIFF");
    d.setUint32(4, 36 + pcm.byteLength, true);
    text(8, "WAVEfmt ");
    d.setUint32(16, 16, true);
    d.setUint16(20, 1, true); // PCM
    d.setUint16(22, 1, true); // mono
    d.setUint32(24, RATE, true);
    d.setUint32(28, RATE * 2, true);
    d.setUint16(32, 2, true);
    d.setUint16(34, 16, true);
    text(36, "data");
    d.setUint32(40, pcm.byteLength, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: "audio/wav" });
  }

  if (!(window.isSecureContext && navigator.mediaDevices?.getUserMedia)) {
    $("talk-hint").hidden = false;
    return;
  }
  $("talk").hidden = false;
  const out = $("talk-out");
  out.value = stored(OUT_KEY) === "phone" ? "phone" : "auto";
  out.addEventListener("change", () => {
    keep(OUT_KEY, out.value);
    if (out.value === "phone" && token()) phone.connect().catch((err) => say(err.message));
  });
  if (out.value === "phone" && token()) phone.connect().catch((err) => say(err.message));
  $("talk-sink").addEventListener("change", sink);
  const button = $("talk-button");
  button.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    button.setPointerCapture?.(ev.pointerId);
    start();
  });
  button.addEventListener("pointerup", stop);
  button.addEventListener("pointercancel", stop);
  button.addEventListener("contextmenu", (ev) => ev.preventDefault());
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) release();
  });
})();
