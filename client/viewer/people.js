// The People tab of the phone page: add people, their faces and voices, away from home.
// Everything here goes to the admin API (/v1/admin/people) with the dashboard's admin
// token, which this phone keeps (the dashboard on the same address shares it).
"use strict";

(() => {
  const TOKEN_KEY = "companion.admin.token";
  const $ = (id) => document.getElementById(id);
  const ROLE = { user: "the user", acquaintance: "acquaintance", stranger: "stranger" };
  let timer = null;
  let busy = false;

  function token() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch {
      return "";
    }
  }

  function setToken(value) {
    try {
      if (value) localStorage.setItem(TOKEN_KEY, value);
      else localStorage.removeItem(TOKEN_KEY);
    } catch {}
  }

  async function admin(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (token()) headers.Authorization = `Bearer ${token()}`;
    const res = await fetch(`/v1/admin/people${path}`, { ...options, headers });
    let body = {};
    try {
      body = await res.json();
    } catch {}
    if (res.status === 401 || res.status === 403) {
      login(body.detail || res.statusText);
      throw new Error(body.detail || res.statusText);
    }
    if (!res.ok) throw new Error(body.detail || `${res.status} ${res.statusText}`);
    return body;
  }

  const json = (method, data) => ({
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

  function login(reason) {
    $("people-login").hidden = false;
    $("people-main").hidden = true;
    $("people-refused").textContent = reason || "";
    $("people-refused").hidden = !reason;
    clearInterval(timer);
    timer = null;
  }

  function say(text, ok) {
    const el = $("people-message");
    el.textContent = text;
    el.style.color = ok ? "var(--ok)" : "";
    el.style.borderColor = ok ? "var(--ok)" : "";
    el.hidden = !text;
  }

  async function run(work, done) {
    if (busy) return;
    busy = true;
    say(done ? "Working…" : "", true);
    try {
      const result = await work();
      say(done ? done(result) : "", true);
    } catch (err) {
      say(err.message, false);
    } finally {
      busy = false;
      load().catch(() => {});
    }
  }

  function button(text, onclick) {
    const b = document.createElement("button");
    b.className = "small";
    b.textContent = text;
    b.onclick = onclick;
    return b;
  }

  function voicePicker(person) {
    const label = document.createElement("label");
    label.className = "button small";
    label.textContent = "Voice";
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "audio/*";
    input.setAttribute("capture", "");
    input.hidden = true;
    input.onchange = () => {
      const file = input.files?.[0];
      input.value = "";
      if (!file) return;
      run(
        () => admin(`/${person.id}/voice`, { method: "POST", body: file }),
        (r) => `${person.name}: ${r.kept} voice clips kept` + (r.left_out ? `, ${r.left_out} left out` : "") + ".",
      );
    };
    label.append(input);
    return label;
  }

  function render(data) {
    const select = $("people-camera");
    const chosen = select.value;
    select.replaceChildren(
      ...data.cameras.map((c) => Object.assign(document.createElement("option"), { value: c, textContent: c })),
    );
    if (data.cameras.includes(chosen)) select.value = chosen;

    const here = $("people-here");
    here.replaceChildren();
    if (!data.perception.ready) here.append(Object.assign(document.createElement("li"), {
      className: "muted", textContent: "Perception is off (turn it on in the dashboard's People page).",
    }));
    else if (!data.here.length) here.append(Object.assign(document.createElement("li"), {
      className: "muted", textContent: "Nobody in view.",
    }));
    for (const p of data.here) {
      const li = document.createElement("li");
      li.textContent = `${p.name ?? "A stranger"} · ${ROLE[p.role]} · ${p.camera} `;
      if (p.role === "stranger") li.append(button("Add as a person", () => promote(p.track_id)));
      here.append(li);
    }

    const list = $("people-list");
    list.replaceChildren();
    if (!data.people.length) list.append(Object.assign(document.createElement("p"), {
      className: "muted", textContent: "Nobody yet. Add yourself first, as the user.",
    }));
    for (const person of data.people) {
      const card = document.createElement("article");
      card.className = "device";
      const h = document.createElement("h2");
      h.textContent = `${person.name}`;
      const info = document.createElement("span");
      info.className = "muted";
      info.textContent = `${ROLE[person.role]} · ${person.face_samples} face samples · ${person.voice_clips} voice clips` +
        (person.here ? ` · here (${person.camera})` : "");
      const row = document.createElement("div");
      row.className = "row";
      row.append(
        button("Face", () => run(
          () => admin(`/${person.id}/face`, json("POST", { camera: select.value || null })),
          (r) => `${person.name}: ${r.found} face samples found, ${r.face_samples} kept.`,
        )),
        voicePicker(person),
        button("Forget", () => {
          if (confirm(`Forget ${person.name}? Their samples are deleted at once.`))
            run(() => admin(`/${person.id}`, { method: "DELETE" }), () => `${person.name} forgotten.`);
        }),
      );
      card.append(h, info, row);
      list.append(card);
    }
  }

  function promote(trackId) {
    const name = prompt("Their name?");
    if (!name) return;
    if (!confirm(`Did ${name} agree to be recognised?`)) return;
    run(
      () => admin("/promote", json("POST", { track_id: trackId, name, role: "acquaintance", consent: true })),
      (p) => `${p.name} added from the face in view.`,
    );
  }

  async function load() {
    const data = await admin("");
    $("people-login").hidden = true;
    $("people-main").hidden = false;
    render(data);
  }

  $("people-login").onsubmit = (event) => {
    event.preventDefault();
    setToken($("people-token").value.trim());
    $("people-token").value = "";
    window.peopleTab.open();
  };

  // Signing in with the dashboard's login QR code, from a photo: browsers open the camera
  // itself only on https or localhost, and this page is plain http on the tailnet.
  let jsQRLoaded = null;
  function loadJsQR() {
    jsQRLoaded ??= new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "jsqr.min.js";
      script.onload = () => resolve(window.jsQR);
      script.onerror = () => {
        jsQRLoaded = null;
        reject(new Error("cannot load the QR code reader"));
      };
      document.head.append(script);
    });
    return jsQRLoaded;
  }

  // The photo as something a canvas can draw: an ImageBitmap, or an <img> where there is none.
  async function picture(file) {
    if (window.createImageBitmap) {
      try {
        return await createImageBitmap(file);
      } catch {}
    }
    const img = new Image();
    img.src = URL.createObjectURL(file);
    try {
      await img.decode();
    } catch {
      throw new Error("This browser cannot read the photo.");
    }
    return img;
  }

  function decode(jsQR, bitmap, longest) {
    const scale = Math.min(1, longest / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * scale);
    canvas.height = Math.round(bitmap.height * scale);
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
    return jsQR(data, width, height, { inversionAttempts: "attemptBoth" });
  }

  async function scan(file) {
    const error = $("people-scan-error");
    error.hidden = true;
    $("people-scan-photo").disabled = true;
    $("people-scan-text").textContent = "Reading…";
    try {
      const jsQR = await loadJsQR();
      const bitmap = await picture(file);
      // Several sizes: a smaller one smooths away the screen's moiré, a larger one finds a
      // code that is only a small part of the photo.
      let code = null;
      for (const longest of [1200, 800, 1800, 2600]) {
        code = decode(jsQR, bitmap, longest);
        if (code && code.data) break;
      }
      if (bitmap.close) bitmap.close();
      else URL.revokeObjectURL(bitmap.src);
      if (!code || !code.data) throw new Error("No QR code found in the picture: take it again, closer and straight on.");
      let found = null;
      try {
        found = new URL(code.data).searchParams.get("token");
      } catch {}
      if (!found) throw new Error("That QR code is not a dashboard login (Phone, on the Overview page).");
      setToken(found);
      window.peopleTab.open();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      $("people-scan-photo").disabled = false;
      $("people-scan-text").textContent = "Scan the QR code";
      $("people-scan-photo").value = "";
    }
  }

  $("people-scan-photo").onchange = (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) scan(file);
  };

  $("people-add").onsubmit = (event) => {
    event.preventDefault();
    const name = $("add-name").value.trim();
    if (!name) return say("Type a name first.", false);
    if (!$("add-consent").checked) return say("Ask the person first: they must agree to be recognised.", false);
    run(async () => {
      const person = await admin("", json("POST", { name, role: $("add-role").value, consent: true }));
      $("add-name").value = "";
      $("add-consent").checked = false;
      return person;
    }, (p) => `${p.name} added. Now their face and voice.`);
  };

  $("people-logout").onclick = () => {
    setToken("");
    login("");
  };

  window.peopleTab = {
    open() {
      load().catch(() => {});
      if (!timer) timer = setInterval(() => {
        if (!document.hidden && !$("people").hidden && !busy) load().catch(() => {});
      }, 3000);
    },
  };
})();
