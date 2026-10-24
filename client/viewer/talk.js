// Push to talk on the phone: hold the button, speak, let go. The sentence goes to the companion
// as said by the user (it needs the admin token, as the People tab does) and is answered through
// the device the companion talks with. Browsers open the microphone only on https: open this
// page at the server's tailnet name (tailscale serve), not at its 100.x address.
"use strict";

(() => {
  const TOKEN_KEY = "companion.admin.token";
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

  function token() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return "";
    }
  }

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
      context ??= new AudioContext();
      await context.resume();
    } catch (err) {
      say(`No microphone: ${err.message}`);
      return;
    }
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
    $("talk-button").classList.remove("on");
    if ((performance.now() - startedAt) / 1000 < MIN_S) {
      say("Hold the button while you speak.");
      return;
    }
    const wav = toWav(merge(chunks), context.sampleRate);
    say("Sending…");
    try {
      const res = await fetch("/v1/view/talk", {
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

  // The microphone is given back while the page is not in view.
  function release() {
    if (recording) stop();
    if (stream) stream.getTracks().forEach((t) => t.stop());
    stream = null;
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
