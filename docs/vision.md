# Vision

> **The LLM that powers your agent must be multimodal (accept images) for the camera to work in the default mode.** If it is not, set `vision.mode: vlm` and configure a vision model in the `vlm` section ([VLM mode](#vlm-mode)); the agent then receives text descriptions instead of images.

Vision is opt-in per session: the dev client's **Start camera** sends `vision.enable`, and **Stop camera** sends `vision.disable`, which stops all visual processing immediately, including an analysis in progress.

```text
camera frame (JPEG/PNG/WebP)
 -> validate type, size and pixel count
 -> rate limit (vision.min_interval_s; "manual" frames skip it)
 -> skip near-duplicates of the last accepted frame (difference hash)
 -> downscale to vlm.max_image_side
 -> vision.mode: agent  newest frame is attached as an image to your next message
    vision.mode: vlm    a vision model describes the newest frame -> observation text
```

Cameras send a frame every few seconds (configurable, or only on request). In addition, when you start speaking or send a message, the server asks every camera device for a fresh frame (`vision.capture.request`, controlled by `vision.capture_on_turn`) and waits up to `vision.wait_for_pending_s` for it, so a question like 「これ何？」 is answered with what the camera sees now.

## Agent mode (default)

`vision.mode: agent` hands the image straight to the agent: your next message is sent as text plus the newest frame (an OpenAI `image_url` part). The agent's own model does the seeing, and its memory records what it saw. With `vision.max_turn_images: N` (default 1) the message carries up to N frames that arrived since the last turn, oldest first; when more arrive, routine frames are dropped before ones a detector or the user asked for. Each frame is sent at most once and only if it is younger than `vision.observation_max_age_s`; the companion keeps no image after the turn.

This needs an agent backend and model that accept images:

- **Hermes / OpenClaw** pass `image_url` parts through. In OpenClaw, the model definition must list image input, e.g. `"input": ["text", "image"]`, or the image is not forwarded.
- **llama.cpp** must be started with the model's multimodal projector (`--mmproj …`).
- **`direct`**: `llm` must point at a model that accepts images.

Images add to the prompt the model has to process; on a slow GPU, a smaller `vlm.max_image_side` (e.g. 512) keeps turns faster.

## VLM mode

`vision.mode: vlm` uses a separate vision model (the `vlm` section) to describe each accepted frame. Only the newest waiting frame is analyzed; descriptions arrive as `vision.observation` events, are kept as episodic memory, and are passed to the agent as text. Use it when the agent's model cannot see, or to keep images away from the agent entirely.

New observations are passed with your next message (`vision.inject: user`), which keeps the agent's prompt cache intact; each is included once. `vision.inject: system` instead lists recent observations in the system prompt on every turn. A turn waits up to `vision.wait_for_pending_s` for a frame that is still being analyzed.

## IP cameras and phones

Any camera reachable over HTTP or RTSP can be the companion's eyes. `companion camera` runs a small device process that joins the active conversation with the `camera` role, switches vision on, and sends frames when the view changes and whenever the server asks:

```powershell
# A phone running an "IP camera" app on the same network (or on Tailscale)
uv run companion camera http://192.168.1.20:8080/shot.jpg      # HTTP snapshot
uv run companion camera http://192.168.1.20:8080/video         # MJPEG stream
uv run companion camera rtsp://192.168.1.20:8554/live          # RTSP, needs ffmpeg on PATH
```

The source type is detected from the URL and the response. By default (`--mode change`) the bridge looks at the camera itself about once a second, on a tiny greyscale copy, and sends a frame only when something moves or the scene changes, plus a routine frame every `--heartbeat 300` seconds. `--mode periodic --interval 5` sends a frame every 5 seconds instead (`0` = only on request). Other options: `--session <id>` to join a specific session instead of the most recently started one, `--device-id`, and `--no-enable` to leave vision off until someone enables it. The bridge reconnects automatically. If vision is switched off in the session (for example with the **vision on** button in the dev client), the camera pauses and stays paused until vision is switched on again.

If the camera asks for a login (Basic or Digest, e.g. the "IP Webcam" Android app with a password set), put it in `.env` rather than on the command line:

```bash
CAMERA_USERNAME=…
CAMERA_PASSWORD=…
```

`http://user:pass@host:8080/shot.jpg` also works. Credentials never appear in logs.

The bridge is meant to run for a long time. Start it whenever you like: it waits for the camera to come up (retrying every 5 s), waits for a conversation to join, and when a conversation ends it joins the next one.

**Cameras the server runs itself.** Instead of a separate `companion camera` process per camera, list them under `cameras:` (or add them on the dashboard's **Cameras** page, which can also test a camera and show its current view). The server runs the same bridge for each enabled camera, and starts, stops or replaces them as soon as the list changes:

```yaml
cameras:
  - id: livingroom                      # also its device id
    url: http://192.168.1.20:8080/shot.jpg
    username_env: CAMERA_USERNAME       # login from .env (the default names)
    password_env: CAMERA_PASSWORD
    mode: change                        # or periodic, with interval_s
    heartbeat_s: 300
    enabled: true
```

Each camera names its own login variables, so cameras with different logins work side by side. Credentials in a camera's `url` are refused here, because the URL is shown on the dashboard and in `/v1/config`.

**Visual events.** Frames sent because something changed carry what the camera's detector saw (`event: {kind, score, area}`). They are prioritized: frames you asked for come first, then big changes, then routine frames. Vetted change frames skip the duplicate check, and important ones skip the rate limit. In VLM mode, frames wait in a small priority queue, and stale routine frames are dropped when the model is busy. Changes become events for the [attention engine](proactive.md): in VLM mode with the model's description of the frame, in agent mode as "something moved", with the frame itself handed to the agent if it comes to a proactive turn. A clear change can lead to one; small movements stay context.

In both modes, clients that detect things themselves, such as an edge device with an accelerator, can send `vision.event` with a description; it becomes an observation without any model call.

### Built-in vision model

Without a vision model server of your own, the server installs and runs one itself, like the [built-in speech](speech.md#built-in-speech): open **Vision** in the dashboard, press **Install** and turn it on (`builtin.vlm.enabled`). While on, it is used instead of `vlm.provider`. It is [Mage-VL](https://huggingface.co/microsoft/Mage-VL), a 4-billion-parameter vision model; `builtin.vlm.model: auto` picks

- on GPUs that run FP8 (compute capability 8.9 and newer: RTX 40 series, Hopper, Blackwell) the FP8 version, [ajh-code/Mage-VL-FP8-W8A8-W8A16](https://huggingface.co/ajh-code/Mage-VL-FP8-W8A8-W8A16) (about 7 GB to download, ~6.4 GB of graphics memory);
- on older GPUs the original in BF16, or FP16 before compute capability 8.0 (~9 GB). The engine uses PyTorch's CUDA 13 builds, which no longer support Volta (V100) and older: there, run Mage-VL (or another model) on a server of your own and point `vlm` at it.

The model goes to `builtin/vlm/models/<name>` on the first start; a folder you put there yourself can be named instead. The FP8 version decodes about 2.7 times faster with three small fused CUDA kernels, which the engine builds for your GPU on its first start (about two minutes) and keeps in `builtin/vlm/models/.cache`. Building them needs the **CUDA toolkit** matching PyTorch's CUDA (13.0) and, on Windows, **Visual Studio's C++ build tools** (the free Build Tools or Community edition, "Desktop development with C++"); the engine finds both by itself. Without them it runs unfused: the same answers, slower (`builtin.vlm.fused: false` skips the build). Measured on an RTX 4070 Ti: about 0.25 s to read a 768-pixel picture and 23 ms per word piece of the answer (fused).

It serves the OpenAI chat API (with `image_url` parts) on `127.0.0.1` only, so the rest of the server talks to it like to any other vision model.

### Vision model settings

```yaml
vlm:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1   # llama.cpp with --mmproj, vLLM, LM Studio, Ollama, cloud…
  model: default
  max_image_side: 768                  # frames are downscaled before sending
```

Any chat endpoint that accepts `image_url` content parts works. The companion asks for a short JSON description (`description`, `tags`, `confidence`) in the persona's language and falls back to plain text if the model ignores the format. It instructs the model not to identify people. `uv run companion check` sends a small test image to verify the model.

A small VLM is enough: descriptions are short, and one frame is analyzed at a time. The VLM shares the GPU budget with everything else (see below); on a busy GPU, raise `vision.min_interval_s`.
