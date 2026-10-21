// The read-only viewer (server.viewer): the conversation, newest first, and the
// connected devices with what their cameras see. Polls the server; changes nothing
// except asking a camera for live pictures while someone watches.
"use strict";

const KEEP = 400; // conversation lines kept on the page
const $ = (id) => document.getElementById(id);
let tab = "conversation";
let after = 0;
let lines = [];
let stopped = null; // why the server refuses (shown instead of the content)
let cameras = true;
const cards = new Map(); // device_id -> {el, img, live, liveUntil}

async function api(path, options) {
  const res = await fetch(`/v1/view${path}`, options);
  if (res.status === 403 || res.status === 404) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {}
    if (path === "/state" || path.startsWith("/conversation")) refuse(detail);
    throw new Error(detail);
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.status === 202 ? null : res.json();
}

function refuse(detail) {
  stopped = detail;
  $("notice").textContent = detail;
  $("notice").hidden = false;
  $("state").textContent = "off";
  $("state").className = "pill";
}

function show(name) {
  tab = name;
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("active", b.dataset.tab === name);
  $("conversation").hidden = name !== "conversation";
  $("devices").hidden = name !== "devices";
  $("people").hidden = name !== "people";
  if (name === "people" && window.peopleTab) window.peopleTab.open();
  refreshPictures();
}

const time = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const day = (t) => new Date(t * 1000).toLocaleDateString();

function renderLines() {
  const list = $("lines");
  list.replaceChildren();
  let session = null;
  for (const line of [...lines].reverse()) {
    if (line.session_id !== session) {
      session = line.session_id;
      const head = document.createElement("li");
      head.className = "session";
      head.textContent = `conversation ${String(session).slice(0, 8)} · ${day(line.t)}`;
      list.append(head);
    }
    const li = document.createElement("li");
    li.className = `line ${line.role === "user" ? "user" : "assistant"}`;
    const meta = document.createElement("span");
    meta.className = "meta";
    const SPEAKER = { stranger: "a stranger", media: "TV / media", unknown: "someone" };
    const sp = line.speaker;
    const heard = sp ? sp.name ?? SPEAKER[sp.role] ?? "heard" : "heard";
    const who = line.role === "user" ? (line.typed ? "typed" : heard) : "companion";
    meta.textContent = [time(line.t), who, line.device_id, line.cancelled ? "cut off" : null]
      .filter(Boolean)
      .join(" · ");
    li.append(meta, document.createTextNode(line.text ?? ""));
    list.append(li);
  }
  $("no-lines").hidden = lines.length > 0;
}

async function loadConversation() {
  const { lines: fresh } = await api(`/conversation?after=${after}`);
  if (!fresh.length) return;
  after = fresh.at(-1).id;
  lines = [...lines, ...fresh].slice(-KEEP);
  renderLines();
}

function describe(device) {
  const parts = [(device.roles ?? []).join(", ") || "device"];
  const s = device.status ?? {};
  if (s.wifi_signal_dbm != null) parts.push(`Wi-Fi ${s.wifi_signal_dbm} dBm`);
  if (s.cpu_temp_c != null) parts.push(`${Math.round(s.cpu_temp_c)} °C`);
  if (device.session_id === null) parts.push("not connected: its last picture");
  return parts.join(" · ");
}

function card(device) {
  let c = cards.get(device.device_id);
  if (!c) {
    const el = document.createElement("article");
    el.className = "device";
    const h = document.createElement("h2");
    const info = document.createElement("span");
    info.className = "muted";
    const img = document.createElement("img");
    img.alt = `what ${device.device_id} sees`;
    img.hidden = true;
    const row = document.createElement("div");
    row.className = "row";
    const live = document.createElement("button");
    live.textContent = "Live";
    const age = document.createElement("span");
    age.className = "muted";
    row.append(live, age);
    el.append(h, info, img, row);
    c = { el, h, info, img, row, live, age, liveUntil: 0, frameAge: null, camera: false };
    live.onclick = () => toggleLive(device.device_id);
    cards.set(device.device_id, c);
  }
  c.h.textContent = device.device_id;
  c.info.textContent = describe(device);
  c.camera = cameras && device.frame_age_s != null;
  c.connected = device.session_id !== null;
  c.frameAge = device.frame_age_s;
  c.img.hidden = !c.camera;
  c.row.hidden = !c.camera;
  c.live.hidden = !c.connected || !(device.roles ?? []).includes("camera");
  c.age.textContent = c.camera ? `picture from ${Math.round(device.frame_age_s)} s ago` : "";
  return c;
}

function stateText(sessions) {
  if (!sessions.length) return ["no conversation", "pill"];
  const s = sessions[0].state;
  return [s, s === "idle" ? "pill on" : "pill busy"];
}

async function loadState() {
  const st = await api("/state");
  cameras = st.cameras;
  const [text, cls] = stateText(st.sessions);
  $("state").textContent = text;
  $("state").className = cls;
  const seen = new Set();
  const list = $("device-list");
  for (const device of st.devices) {
    if (!device.device_id || seen.has(device.device_id)) continue;
    seen.add(device.device_id);
    list.append(card(device).el); // append moves it: the order follows the server's
  }
  for (const [id, c] of cards) {
    if (!seen.has(id)) {
      c.el.remove();
      cards.delete(id);
    }
  }
  $("no-devices").hidden = seen.size > 0;
  refreshPictures();
}

function refreshPictures() {
  if (tab !== "devices" || document.hidden) return;
  const now = Date.now();
  for (const [id, c] of cards) {
    if (!c.camera) continue;
    const live = c.liveUntil > now;
    const due = live ? 900 : 4000;
    if (!c.loadedAt || now - c.loadedAt >= due) {
      c.loadedAt = now;
      c.img.src = `/v1/view/devices/${encodeURIComponent(id)}/frame?t=${now}`;
    }
    c.live.classList.toggle("active", live);
  }
}

async function toggleLive(id) {
  const c = cards.get(id);
  if (!c) return;
  const on = c.liveUntil <= Date.now();
  c.liveUntil = on ? Date.now() + 5 * 60 * 1000 : 0; // at most 5 minutes in a row
  try {
    await askLive(id, on ? 15 : 0);
  } catch (err) {
    c.liveUntil = 0;
    c.age.textContent = err.message;
  }
  refreshPictures();
}

async function askLive(id, seconds) {
  await api(`/devices/${encodeURIComponent(id)}/live`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seconds }),
  });
}

// Keep live pictures coming while someone looks (each request lasts 15 s).
setInterval(() => {
  if (document.hidden || tab !== "devices") return;
  for (const [id, c] of cards) if (c.liveUntil > Date.now()) askLive(id, 15).catch(() => {});
}, 10000);

async function tick() {
  try {
    await loadState();
    await loadConversation();
    if (stopped) {
      stopped = null;
      $("notice").hidden = true;
    }
  } catch (err) {
    if (!stopped) {
      $("state").textContent = "unreachable";
      $("state").className = "pill";
    }
  }
}

for (const b of document.querySelectorAll("nav button"))
  b.onclick = () => {
    history.replaceState(null, "", `#${b.dataset.tab}`); // a bookmark opens the same tab
    show(b.dataset.tab);
  };
if (location.hash === "#devices") show("devices");
if (location.hash === "#people") addEventListener("load", () => show("people"));
document.addEventListener("visibilitychange", () => !document.hidden && tick());
setInterval(() => !document.hidden && tick(), 2500);
setInterval(refreshPictures, 1000);
tick();
