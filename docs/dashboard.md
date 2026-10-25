# Admin dashboard

`/admin/` shows the server's status, connected devices with their round-trip time and last reported status, latency per pipeline stage, and every setting, which you can change there.

- Each setting and group of settings says in plain words what it does, with units and examples where that helps; the same text is in the config's JSON schema (`GET /v1/admin/config`).
- Each setting is marked **applies now** or **needs restart**. Those that apply now (vad, attention, realtime, most of vision, `metrics.emit_turn`) do so as soon as you save, also to conversations already open; the rest wait for a restart, which the page offers to do. The server then stops, reads its config files again and starts in the same process; if the new settings cannot start (say the port is taken), it starts again with the previous ones.
- Values are checked before saving, and invalid ones are marked at the setting.
- Saving writes only what differs from `configs/companion.yaml` into `configs/companion.local.yaml`; `companion.yaml` is never changed. The version before each save is kept as `companion.previous.local.yaml`, and a file you wrote by hand (with your comments) as `companion.original.local.yaml`, once.
- `--host`/`--port` on the command line win over the file; such settings are marked.
- An override can add or change keys of a mapping such as `extra_body` but cannot remove one that `companion.yaml` sets; the page says so, and that change has to be made in `companion.yaml`.
- Secrets never appear: settings that name an environment variable show only "set in .env" or "not set". Edit `.env` itself to change them.

The **Voice pipeline** card on the Overview page follows every connected microphone and speaker stage by stage: **Microphone** (audio arriving, and how loud), **Speech** (a sentence found in it), **Words** (speech recognition), **Answer** (the agent), **Voice** (text to speech) and **Speaker** (the device said it played the answer). Each stage shows green, amber or red with what it did last and when; one that failed before shows that when you point at it. **Test voice input** checks the way in: say a short sentence near the device within 15 s (on the phone with push to talk: hold to talk), and it says whether audio arrived and how loud, whether speech was found, and what was recognised; the sentence is not answered. **Test the answer** checks the way out: it makes the companion's voice say a test sentence on that device and waits for the device to say it played it. Below are the switches for the phone (`phone`, see [Reaching the server from outside](remote-access.md#talking-from-the-phone)) and **Take pictures from boards** (`vision.from_devices`).

The **Personas** page lists the personas in `personas_dir` and edits their display name, language, TTS voice and system prompt. **Use this persona** switches at once (from the next reply) and saves `agent.persona`; editing the persona in use also applies from the next reply. The bundled `default` persona is not edited in place, so updates to the project cannot conflict with your changes: **Save a copy** makes your own, which git ignores. A persona in use, now or after a restart, cannot be deleted.

The **Devices** page lists the connected devices with their last reported status. Edge devices running companion-edge 1.1 or newer started with `--config` show their settings there, and can be changed, or set up from one of the [hardware profiles](edge-devices.md): the device checks the new settings, writes the changed lines into its TOML file (keeping the previous file as `edge.toml.bak`) and restarts with them in the same process, so no service manager is needed. `server`, `token_env`, `device_id` and `state_file` can only be changed on the device, as a mistake there would cut it off.

The Devices page also has, per device, its **Log**, **Camera**, **Diagnostics** and **Maintenance** (see [Looking after boards](edge-devices.md#looking-after-boards)), and lists boards that are not connected but left their log here. The **Boards** page sets boards up over USB and lists the boards this server set up (see [Setting up a board from the dashboard](edge-devices.md#setting-up-a-board-from-the-dashboard)).

The **People** page installs and switches on the perception engine and manages the people the companion knows: add someone (with their agreement), their face from a camera and their voice from a recording, add the stranger in view, see who is here now, forget someone. See [People](people.md).

The **Vision** page installs and switches on the built-in vision model (Mage-VL), switches glances and the timeline on and off, shows who is doing what, and **Look now** describes a camera's picture. The **Timeline** page shows what each person has been doing over the last hours, with the rules and the watches the agent added. See [People](people.md#glances-who-is-doing-what).

The **Events** page shows what the server is doing as it happens: devices connecting and leaving, state changes, attention decisions with their reasons, camera observations, turns, errors and saved settings, with filters and a pause. What people say is not part of it unless `server.dashboard_conversation: true`: otherwise transcripts and replies show only their length.

The **Conversations** page shows what the companion heard and said, newest first and per conversation, while `server.dashboard_conversation` is on (from then on; nothing is kept while it is off). With `stt.keep_audio: true` each line you said has its recording next to it (**Listen**, with its length and how long recognition took), and utterances that never got an answer (dropped as the companion's own voice, or nothing recognized) show among the lines, greyed out: listen to them when the companion mishears. With the conversation off, only the recordings show. The page also says where phones open the read-only viewer (`server.viewer`, see [Reaching the server from outside](remote-access.md#following-on-your-phone)).

The **Vision** page installs and switches on the built-in vision model (Mage-VL); see [Vision](vision.md#built-in-vision-model).

The dashboard and its API (`/v1/admin/*`) have their own token, so a device's `COMPANION_TOKEN` cannot change the server:

- This machine never needs the token: open `http://127.0.0.1:8765/admin/` on the server itself.
- Other machines need `COMPANION_ADMIN_TOKEN` from `.env`. Without it set, the dashboard answers only this machine.
- **A phone**: the **Phone** card on the Overview page makes the token if there is none (saved in `.env`, used at once) and shows a QR code to scan with the phone's camera, for this machine's Tailscale address or one on this network; the dashboard opens on the phone and stays logged in. If the phone's camera app does not open links, open the dashboard on the phone and press **Scan the QR code** on its sign-in page: it takes a photo of the code and reads it in the page (a photo, because browsers open the camera themselves only on https or this machine). **Copy the link** gives the same address to send yourself. Whoever has the code or the link can use the dashboard: **New token** makes another one and logs every phone and browser out.
- Any other browser: enter the token when the page asks, or open it once as `/admin/?token=…`.

`server.dashboard: false` turns the page off. The built page is committed in `client/dashboard/dist/`, so running the server needs no Node; to change it, see [Development](development.md).
