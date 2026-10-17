# Edge devices

A small Linux board with a camera, microphone and speaker can be the companion's body, while all inference stays on the server. The edge client lives in `client/edge`: a separate, deliberately tiny uv project whose only Python dependency is `websockets`. Audio and video go through standard tools (ALSA `arecord`/`aplay`, `ffmpeg`, and `libopus` via ctypes), so nothing heavy has to be compiled on boards such as RISC-V SBCs with 256 MB of RAM.

```text
board: arecord -> Opus (libopus) ----\                     /-> STT -> agent -> TTS --\
       ffmpeg (V4L2) -> JPEG frames ---> WebSocket (token) --> camera frames            |
       aplay <-------------------------------------------------- speech audio <--------/
```

## Setting up a board from the dashboard

For a LicheeRV Nano (or another board whose image logs in as `root` over SSH and starts services from `/etc/init.d` or systemd, with Python 3.11+ and `websockets`), nothing has to be typed on the board:

1. Flash the SD card with the maker's image (Sipeed's Buildroot image for the LicheeRV Nano) and let the board boot once (about 30 s).
2. Plug the board into the computer running the server by USB (the LicheeRV Nano's USB-C port). Its image offers a USB network; the server finds the board on it.
3. In the dashboard, open **Boards**, give the board a name, pick the hardware profile, the address it should reach this server at, the camera, and the Wi-Fi networks it may use (home and your phone's hotspot), and press **Set up**. `server.host` must not be `127.0.0.1` (`uv run companion setup` takes care of that).
4. Unplug it and power it from anywhere with Wi-Fi; it connects by itself.

**Set up** logs in over SSH (the stock password, or one you give), puts this server's own SSH key on the board and replaces the stock root password with a random one (shown under **Boards**: the stock one is public and the board goes outdoors). It gives the board a **token of its own**, copies this server's edge client to it (the board needs no Internet), writes `edge.toml` from the profile (a board set up before keeps its settings; a reflashed one gets back the settings it last reported), adds the Wi-Fi networks with their keys hashed (also on the boot partition, where Sipeed's image reads them from), installs a service that starts at boot and restarts the client if it stops, checks the tools and starts it. The same from a terminal: `uv run companion edge install --name walker --wifi "Home" --wifi "Phone"` (`uv run companion edge discover` lists boards on USB).

To change a board that was set up before but cannot connect (a new Wi-Fi network, another server address) or to bring its software up to date without the network, plug it in again and press **Update over USB** under **Boards** (or `uv run companion edge update --name walker --wifi "New network"`): it keeps the board's token and password and only updates its software, settings and Wi-Fi networks.

A board's own token works only for its name. **Lock out** under **Boards** makes it stop working at once and closes the board's connection, for example when a board is lost; set the board up again to let it back in.

## Setting up a board by hand

**On the server:** listen on an address the board can reach and set a token:

```yaml
# configs/companion.local.yaml
server:
  host: 0.0.0.0          # or the server's Tailscale IP
```

```bash
# .env
COMPANION_TOKEN=<a long random string>
```

The server refuses to listen beyond localhost without a token. With a token set, every client must present it: the dev client as `/dev/?token=…` (opened once; the browser remembers it), `companion camera` and the edge client from `COMPANION_TOKEN` in their environment.

**On the board** (Debian/Ubuntu-based images). Without git, use `companion-edge-<version>.tar.gz` from the [Releases](https://github.com/Nero4541/CompanionMesh/releases) page instead of the clone; it unpacks to the same files as `client/edge`, profiles included.

```bash
sudo apt install alsa-utils ffmpeg libopus0
git clone https://github.com/Nero4541/CompanionMesh.git && cd CompanionMesh/client/edge
cp edge.example.toml edge.toml      # set server = "ws://<server>:8765/v1/realtime"
export COMPANION_TOKEN=…
uv run companion-edge --config edge.toml --check   # tools, libopus, camera
uv run companion-edge --config edge.toml
```

`client/edge/systemd/companion-edge.service` is an example unit for running it at boot.

**MIPI CSI cameras on CVITEK boards.** The LicheeRV Nano's camera module (GC4653) is not a V4L2 device on Sipeed's image, so ffmpeg cannot open it. `[camera] driver = "cvi"` keeps Sipeed's `sensor_test` running and asks it for a frame when needed: change detection reads a few rows of each 2560×1440 frame (about 0.3 s per look), and a JPEG is made only for a frame that is sent (under a second with ffmpeg). `rotate` turns pictures from a module mounted sideways. Do not run Sipeed's `sample_venc` or `sample_vio` against this camera: when they crash, the camera sends no frames again until the board is switched off and on.

**Hardware profiles.** `client/edge/profiles/` has ready-made configs to copy instead of `edge.example.toml`; each explains the hardware it assumes and what to check:

| Profile | For |
|---|---|
| `licheerv-nano.toml` | Sipeed LicheeRV Nano with Sipeed's Buildroot image: onboard microphone and speaker, Opus through ffmpeg (no libopus), half duplex, the MIPI CSI camera (`driver = "cvi"`) |
| `raspberry-pi.toml` | Raspberry Pi with a USB speakerphone (hardware echo cancellation, so you can interrupt) and a USB webcam |
| `bluetooth-speaker.toml` | Any board playing through a Bluetooth speaker with bluez-alsa (lead-in and a longer hold for the speaker's wake-up) |
| `speaker-only.toml` | A speaker without microphone or camera, e.g. for proactive remarks while a microphone is too noisy |
| `camera-only.toml` | A board with only a camera: it joins whichever conversation was started last and waits for the next one when that ends |

Network cameras and phones need no board: list them under `cameras:` on the server instead (see [IP cameras and phones](vision.md#ip-cameras-and-phones)).

**Your own device.** `client/template/device.py` is a minimal device in one file (only `websockets`) with the whole protocol a device needs and its hardware in four methods to replace. [`client/template/README.md`](../client/template/README.md) explains porting it, for example to a microcontroller or another language.

What the edge client does:

- **Microphone:** 16 kHz audio, Opus-encoded at ~24 kbps (about a tenth of raw PCM) and sent in batches of 20 ms packets. Without `libopus` it uses ffmpeg's Opus encoder, or falls back to PCM. Each batch carries its position in the microphone stream and its capture time. The mic keeps streaming while the companion speaks, so you can interrupt it (`full_duplex = true`). Set `aec = "device"` if your microphone cancels its own speaker's echo.
- **Speaker:** sentences of a reply are played back to back through one output stream, so there are no gaps between them. The client reports when each clip starts (for echo handling) and when the reply finished, and stops immediately when the server says so.
- **Camera:** ffmpeg reads the V4L2 camera once and produces both JPEG frames and a 64x48 greyscale copy. In `change` mode (default), the client checks the greyscale copy `analysis_fps` times a second for motion or a scene change, in plain Python, and sends a frame only when something changed, plus one every `heartbeat_s`. Frames are also sent on the server's capture requests (when you speak). `mode = "periodic"` uses adaptive sampling instead: every frame the server reports as a duplicate doubles the interval (up to `max_interval_s`), and a changed view resets it.
- **Network loss:** reconnects with exponential backoff, resumes the same session (the id is stored on disk), and keeps at most `buffer_s` seconds of microphone audio while offline. The server keeps the session for `server.session_linger_s` (60 s) after the last device drops and replays what the device missed, e.g. a reply that finished meanwhile.
- **Heartbeat:** `device.status` every `heartbeat_s` with CPU temperature, free memory, Wi-Fi signal, uptime and buffer level; `/v1/status` lists every connected device with its latest status.

**Bluetooth speakers** work through [bluez-alsa](https://github.com/arkq/bluez-alsa): pair the speaker with `bluetoothctl`, run `bluealsa -p a2dp-source`, and point `output_device` at an ALSA PCM for it, e.g. in `~/.asoundrc`:

```text
pcm.speaker {
    type plug
    slave.pcm "bluealsa:DEV=AA:BB:CC:DD:EE:FF,PROFILE=a2dp"
}
```

Then set `output_device = "speaker"` and `output_lead_in_ms = 300`: Bluetooth speakers chop the first fraction of a second after their stream starts.

The protocol has nothing board-specific: any client that speaks it (another SBC, a phone app) works the same way.

## Looking after boards

Everything below works wherever the board is, over its own connection (companion-edge 1.2):

- **Log** (Devices → Log): what the board logged, including what it logged while offline (sent when it is back), kept per board in `data/devices/<id>/edge.log` also after it disconnects.
- **Diagnostics**: versions, its own checks, memory, disk, network, Wi-Fi and kernel messages, from a fixed list of read-only commands (there is no remote shell).
- **Camera**: its newest picture, **Take a picture now**, and **Live**: pictures in a row while you watch (as fast as the board manages, at most 2 a second; about one a second on a LicheeRV Nano), which stop by themselves soon after you leave the page. Live pictures only update the view; they do not go to the agent.
- **Maintenance**: **Update** to the edge client this server has (downloaded from the server, checked, installed next to the old one; if the new version cannot connect after three starts, the board goes back to the old one by itself), restart the client or reboot the board, add and remove Wi-Fi networks (not the one it uses now), and speaker and microphone tests.

When a board cannot connect at all, plug it in by USB and run `uv run companion edge doctor`: it shows the board's version, whether the client runs, its checks, settings, log, Wi-Fi and clock. Set it up again to fix settings or Wi-Fi.
