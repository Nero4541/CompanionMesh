// The phone as a microphone and speaker of the companion. The server's phone settings decide
// how: push to talk (hold the button, speak, let go) or listening all the time like a board
// (tap to start), in the board's conversation or one of its own, and where answers play
// (phone.answer_on). The page joins the companion as the device "phone" and plays the answers
// sent to it where the phone plays sound (Bluetooth earphones or a speaker paired with it too).
// It needs the admin token, as the People tab does, and https: browsers open the microphone only
// there, so open this page at the server's tailnet name (tailscale serve), not at its 100.x
// address.
"use strict";

(() => {
  const TOKEN_KEY = "companion.admin.token";
  const SINK_KEY = "companion.talk.sink";
  const SESSION_KEY = "companion.phone.session"; // the phone's own conversation
  const DEVICE_ID = "phone";
  const RATE = 16000; // what the server's speech recognition takes
  const MAX_S = 30;
  const MIN_S = 0.3; // shorter: a tap, not a sentence
  const REFRESH_MS = 5000; // how often the settings and the board's conversation are looked at
  const $ = (id) => document.getElementById(id);

  let settings = { share_session: true, answer_on: "where_said", listen: "push_to_talk" };
  let boardSession = null; // the conversation of a connected board, if any
  let context = null;

  // Push to talk.
  let stream = null;
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
  const always = () => settings.listen === "always";

  // Made while a finger is on the screen: phones play sound only from an audio context started so.
  async function audio() {
    context ??= new AudioContext();
    await context.resume();
    return context;
  }

  // --- push to talk -------------------------------------------------------------------------

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
      await audio();
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
      await phone.connect().catch(() => null); // to hear the answer here, when it plays here
      const query = phone.session ? `?session=${encodeURIComponent(phone.session)}` : "";
      const res = await fetch(`/v1/view/talk${query}`, {
        method: "POST",
        headers: { "Content-Type": "audio/wav", Authorization: `Bearer ${token()}` },
        body: wav,
      });
      let body = {};
      try {
        body = await res.json();
      } catch {}
      if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
      say(`Sent (${body.seconds} s); the companion answers.`);
    } catch (err) {
      say(err.message);
    }
  }

  function closeMic() {
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
  }

  // --- listening all the time (phone.listen: always) ---------------------------------------------

  const listening = {
    on: false,
    stream: null,
    source: null,
    node: null,
    pos: 0, // samples sent since the microphone opened

    async start() {
      if (!token()) {
        say("Sign in under People first (scan the QR code on the dashboard).");
        return;
      }
      try {
        const ctx = await audio();
        this.stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
        });
        await ctx.audioWorklet.addModule("mic-worklet.js?v=1");
        this.source = ctx.createMediaStreamSource(this.stream);
        this.node = new AudioWorkletNode(ctx, "mic-capture");
        this.node.port.onmessage = (e) => {
          if (!(e.data instanceof ArrayBuffer)) return;
          const samples = e.data.byteLength / 2;
          // Position in the microphone stream, and when its first sample was heard.
          const meta = { pos: this.pos, t: Math.round(Date.now() - (samples / RATE) * 1000) };
          this.pos += samples;
          phone.sendBinary("audio.input.chunk", meta, e.data);
        };
        this.source.connect(this.node);
      } catch (err) {
        this.stop();
        say(`No microphone: ${err.message}`);
        return;
      }
      this.on = true;
      outputs();
      $("talk-button").classList.add("on");
      $("talk-button").textContent = "Listening — tap to stop";
      say("Listening: just talk.");
      try {
        await phone.connect();
        this.announce();
      } catch (err) {
        say(err.message);
      }
    },

    // Tell the server the microphone is on (again, on every new connection).
    announce() {
      if (!this.on) return;
      this.pos = 0;
      // The browser cancels its own speaker's echo, so the phone keeps listening while it plays.
      phone.send("audio.input.start", {
        sample_rate: RATE,
        encoding: "pcm_s16le",
        channels: 1,
        full_duplex: true,
        aec: "device",
      });
    },

    stop() {
      if (this.node) this.node.port.onmessage = null;
      this.source?.disconnect();
      this.node?.disconnect();
      this.stream?.getTracks().forEach((t) => t.stop());
      if (this.on) phone.send("audio.input.stop", {});
      this.on = false;
      this.stream = this.source = this.node = null;
      $("talk-button").classList.remove("on");
      $("talk-button").textContent = always() ? "Tap to listen" : "Hold to talk";
    },
  };

  // --- this phone as a device of the companion -------------------------------------------------

  const enc = new TextEncoder();
  const dec = new TextDecoder();

  const phone = {
    ws: null,
    ready: null, // the session id, once the server has started it
    session: null,
    joined: undefined, // what it connected for: the board's conversation, or null (its own)
    roles: null,

    envelope(type, payload) {
      return { type, session_id: this.session, device_id: DEVICE_ID, timestamp: new Date().toISOString(), payload };
    },

    send(type, payload = {}) {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(this.envelope(type, payload)));
    },

    sendBinary(type, payload, bytes) {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      const header = enc.encode(JSON.stringify(this.envelope(type, payload)));
      const frame = new Uint8Array(4 + header.length + bytes.byteLength);
      new DataView(frame.buffer).setUint32(0, header.length, false);
      frame.set(header, 4);
      frame.set(new Uint8Array(bytes), 4 + header.length);
      this.ws.send(frame);
    },

    // Where it should be now: the board's conversation (phone.share_session) or its own.
    wanted() {
      return {
        session: settings.share_session && boardSession ? boardSession : null,
        roles: always() ? ["mic", "speaker"] : ["speaker"],
      };
    },

    connect() {
      if (this.ready) return this.ready;
      if (!token()) return Promise.reject(new Error("Sign in under People first."));
      const want = this.wanted();
      this.joined = want.session;
      this.roles = want.roles.join();
      this.ready = new Promise((resolve, reject) => {
        const proto = location.protocol === "https:" ? "wss" : "ws";
        const ws = new WebSocket(`${proto}://${location.host}/v1/view/realtime?token=${encodeURIComponent(token())}`);
        ws.binaryType = "arraybuffer";
        this.ws = ws;
        ws.onopen = () =>
          this.send("session.start", { session_id: want.session || stored(SESSION_KEY) || undefined, roles: want.roles });
        ws.onclose = (ev) => {
          if (this.ws === ws) {
            this.ws = null;
            this.ready = null;
            this.session = null;
            playback.stop();
          }
          reject(new Error(ev.code === 1008 ? "The server did not let this phone in (sign in again under People)." : `The connection closed (${ev.code}).`));
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
      this.session = null;
      playback.stop();
      ws?.close();
    },

    event(msg, started) {
      const p = msg.payload || {};
      switch (msg.type) {
        case "session.started":
          this.session = p.session_id;
          if (!this.joined) keep(SESSION_KEY, p.session_id);
          started(p.session_id);
          listening.announce();
          showMode();
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
          break; // what this phone need not know
      }
    },

    binary(buf) {
      const view = new DataView(buf);
      const len = view.getUint32(0, false);
      const header = JSON.parse(dec.decode(new Uint8Array(buf, 4, len)));
      if (header.type !== "audio.output.chunk") return;
      const p = header.payload || {};
      // A speaker test from the dashboard comes without a reply around it.
      if (String(p.turn_id).startsWith("test-") && p.turn_id !== playback.turn) playback.begin(p.turn_id);
      playback.enqueue(p, buf.slice(4 + len));
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
      if (payload.turn_id !== this.turn || this.stopped.has(payload.turn_id)) return;
      if (!context) {
        say("An answer came: tap the button once so this phone may play sound.");
        return;
      }
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
          const progress = { turn_id: payload.turn_id, seq: payload.seq, state: "started" };
          if (listening.on) progress.pos = Math.max(0, Math.round(listening.pos + (at - context.currentTime) * RATE));
          phone.send("audio.output.progress", progress);
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

  // --- following the server's settings ---------------------------------------------------------

  async function refresh() {
    let state;
    try {
      state = await (await fetch("/v1/view/state")).json();
    } catch {
      return;
    }
    const before = settings.listen;
    settings = { ...settings, ...(state.phone || {}) };
    const board = (state.devices || []).find(
      (d) => d.session_id && d.device_id !== DEVICE_ID && (d.roles || []).some((r) => r === "mic" || r === "speaker"),
    );
    boardSession = board ? board.session_id : null;
    if (before !== settings.listen) {
      if (listening.on) listening.stop();
      $("talk-button").textContent = always() ? "Tap to listen" : "Hold to talk";
    }
    // In the wrong conversation, or with the wrong roles: connect again.
    const want = phone.wanted();
    if (phone.ws && (phone.joined !== want.session || phone.roles !== want.roles.join())) phone.hangUp();
    if (!phone.ws && token() && !document.hidden) phone.connect().catch((err) => say(err.message));
    showMode();
  }

  function showMode() {
    const where = phone.session && phone.session === boardSession ? "in the board's conversation" : "in a conversation of its own";
    const answers = {
      where_said: "answers play where they were heard",
      board: "answers play on the board",
      phone: "answers play on this phone",
      both: "answers play on the board and this phone",
    }[settings.answer_on];
    const how = always() ? "listens all the time (tap to start)" : "push to talk";
    $("talk-mode").textContent = `This phone: ${how}, ${where}; ${answers}. (Configuration → phone)`;
  }

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
  $("talk-sink").addEventListener("change", sink);
  const button = $("talk-button");
  button.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    if (always()) {
      if (listening.on) listening.stop();
      else listening.start();
      return;
    }
    button.setPointerCapture?.(ev.pointerId);
    start();
  });
  button.addEventListener("pointerup", () => always() || stop());
  button.addEventListener("pointercancel", () => always() || stop());
  button.addEventListener("contextmenu", (ev) => ev.preventDefault());
  // The microphone is given back while the page is not in view.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (recording) stop();
      closeMic();
      listening.stop();
    } else {
      refresh();
    }
  });
  refresh();
  setInterval(() => document.hidden || refresh(), REFRESH_MS);
})();
