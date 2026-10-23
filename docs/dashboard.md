# Admin dashboard

`/admin/` shows the server's status, connected devices with their round-trip time and last reported status, latency per pipeline stage, and every setting, which you can change there.

- Each setting and group of settings says in plain words what it does, with units and examples where that helps; the same text is in the config's JSON schema (`GET /v1/admin/config`).
- Each setting is marked **applies now** or **needs restart**. Those that apply now (vad, attention, realtime, most of vision, `metrics.emit_turn`) do so as soon as you save, also to conversations already open; the rest wait for a restart, which the page offers to do. The server then stops, reads its config files again and starts in the same process; if the new settings cannot start (say the port is taken), it starts again with the previous ones.
- Values are checked before saving, and invalid ones are marked at the setting.
- Saving writes only what differs from `configs/companion.yaml` into `configs/companion.local.yaml`; `companion.yaml` is never changed. The version before each save is kept as `companion.previous.local.yaml`, and a file you wrote by hand (with your comments) as `companion.original.local.yaml`, once.
- `--host`/`--port` on the command line win over the file; such settings are marked.
- An override can add or change keys of a mapping such as `extra_body` but cannot remove one that `companion.yaml` sets; the page says so, and that change has to be made in `companion.yaml`.
- Secrets never appear: settings that name an environment variable show only "set in .env" or "not set". Edit `.env` itself to change them.

The **Personas** page lists the personas in `personas_dir` and edits their display name, language, TTS voice and system prompt. **Use this persona** switches at once (from the next reply) and saves `agent.persona`; editing the persona in use also applies from the next reply. The bundled `default` persona is not edited in place, so updates to the project cannot conflict with your changes: **Save a copy** makes your own, which git ignores. A persona in use, now or after a restart, cannot be deleted.

The **Devices** page lists the connected devices with their last reported status. Edge devices running companion-edge 1.1 or newer started with `--config` show their settings there, and can be changed, or set up from one of the [hardware profiles](edge-devices.md): the device checks the new settings, writes the changed lines into its TOML file (keeping the previous file as `edge.toml.bak`) and restarts with them in the same process, so no service manager is needed. `server`, `token_env`, `device_id` and `state_file` can only be changed on the device, as a mistake there would cut it off.

The Devices page also has, per device, its **Log**, **Camera**, **Diagnostics** and **Maintenance** (see [Looking after boards](edge-devices.md#looking-after-boards)), and lists boards that are not connected but left their log here. The **Boards** page sets boards up over USB and lists the boards this server set up (see [Setting up a board from the dashboard](edge-devices.md#setting-up-a-board-from-the-dashboard)).

The **Events** page shows what the server is doing as it happens: devices connecting and leaving, state changes, attention decisions with their reasons, camera observations, turns, errors and saved settings, with filters and a pause. What people say is not part of it unless `server.dashboard_conversation: true`: otherwise transcripts and replies show only their length.

The **Conversations** page shows what the companion heard and said, newest first and per conversation, while `server.dashboard_conversation` is on (from then on; nothing is kept while it is off). With `stt.keep_audio: true` each line you said has its recording next to it (**Listen**, with its length and how long recognition took), and utterances that never got an answer (dropped as the companion's own voice, or nothing recognized) show among the lines, greyed out: listen to them when the companion mishears. With the conversation off, only the recordings show. The page also says where phones open the read-only viewer (`server.viewer`, see [Reaching the server from outside](remote-access.md#following-on-your-phone)).

The **Vision** page installs and switches on the built-in vision model (Mage-VL); see [Vision](vision.md#built-in-vision-model).

The dashboard and its API (`/v1/admin/*`) have their own token, so a device's `COMPANION_TOKEN` cannot change the server:

- With `COMPANION_ADMIN_TOKEN` unset, they answer only this machine (open `http://127.0.0.1:8765/admin/` on the server itself).
- With `COMPANION_ADMIN_TOKEN=<a long random string>` in `.env`, any address works once you enter the token (or open the page once as `/admin/?token=…`).

`server.dashboard: false` turns the page off. The built page is committed in `client/dashboard/dist/`, so running the server needs no Node; to change it, see [Development](development.md).
