# People

The companion can know who is around: you (the **user**), the **acquaintances** you add by name, and everyone else (**strangers**). It recognises faces on its cameras and, for who said what, voices.

Everything here is off by default (`perception.enabled: false`) and runs on this machine. Face and voice samples are biometric data: they are kept only in `data/people/` and never sent to the agent or to any online service. The agent gets names and roles, nothing else.

## Turning it on

1. Open **People** in the dashboard and press **Install** (a few GB: PyTorch, onnxruntime and the models' runtime go into `builtin/perception/.venv`).
2. Switch it **On**. The perception engine starts as a process of its own (about 0.6 GB of graphics memory with an NVIDIA GPU, or on the processor without one) and stops at once when you switch it off.
3. Add yourself as the user, then the people you see often.

The engine uses two models, each a folder in `builtin/perception/models/`:

| Model | What for | Where it comes from |
|---|---|---|
| `buffalo_l` (InsightFace: SCRFD face detection, ArcFace recognition, 106 landmarks) | Faces | Downloaded there on the first start. For non-commercial use only. |
| `speaker-diarization-community-1` (pyannote, its WeSpeaker voice model) | Voices | Gated on Hugging Face, see below |

**The voice model is gated**: sign in to Hugging Face, open [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) and accept its conditions. Then either

- put your Hugging Face token in the server's `.env` (`HF_TOKEN=hf_…`, a "read" token from your Hugging Face settings); the engine downloads the model into its models folder on the next start, or
- download the repository yourself into `builtin/perception/models/speaker-diarization-community-1/` (it holds `config.yaml` and the folders `embedding`, `segmentation` and `plda`), for example with `huggingface-cli download pyannote/speaker-diarization-community-1 --local-dir builtin/perception/models/speaker-diarization-community-1`.

Without it, faces still work; the People page says that voices are not recognised and why. Set `perception.voice_model: none` to run faces only.

## Adding someone

Ask first: a person is only added when you tick **They agreed to be recognised**.

- **Face**: choose a camera and press **Add face**. The person looks at the camera for about ten seconds (`perception.enrol_seconds`), alone in view and with light on their face. The server keeps up to 40 samples (`people.max_face_samples`) from different angles; side views and small faces are left out.
- **Voice**: a recording of the person speaking alone for 30 to 60 seconds (reading something aloud is fine), as an audio file in any common format, or recorded in the browser with **Record voice** (browsers allow the microphone only on `https://` or on this machine). The server cuts it into clips of about six seconds, checks that they are all the same voice and leaves out those that are not. Clean recordings matter more than anything else for recognising voices.
- **From a stranger in view**: under **Here now**, **Add as a person…** takes the face samples from the stranger the cameras see right now.

**On a phone, away from home**: the phone page `/view/` (see [remote access](remote-access.md)) has a **People** tab that does the same. It needs the dashboard's admin token (`COMPANION_ADMIN_TOKEN` in `.env`): the page asks for it once and keeps it on the phone. **Scan the QR code** there takes a photo of the QR code under **Phone** on the dashboard's Overview page and signs in with it. **Voice** opens the phone's recorder.

**Forget** deletes a person and their samples at once. **Clear face** and **Clear voice** delete only those samples.

## What the companion sees

Every camera's pictures go through the engine: faces are found, followed from picture to picture (a *track*) and named when one clear picture of the track matches someone (`people.face_threshold` 0.45, and `people.face_margin` 0.10 ahead of the next person). A cut or a camera move starts new tracks, so a name is never carried over to someone else.

- **Cameras the server watches itself** (`cameras:`) are looked at `perception_fps` times a second (10 by default), straight from the camera's stream (`.../video`, `rtsp://`); a snapshot address (`.../shot.jpg`) is asked that often. With 15 or more pictures a second, lip movement tells who is talking.
- **Boards** send a picture every `camera.min_interval_s` (5 s by default) while perception is on, and every one of them is looked at; a still scene no longer slows them down (without perception they wait up to `camera.max_interval_s`). A LicheeRV Nano makes a 1280x720 picture in about 1.8 s and a 640x360 one faster; a face of 80 px or more is recognised well, the smallest looked at is `perception.min_face_px` (40). `companion camera` sends one when the view changes, or every `--interval` seconds (5) with `--mode periodic`. A board in [stream mode](edge-devices.md) streams its camera instead while perception is on, at `perception.stream_fps` (10) pictures a second.

A picture is skipped, never queued, while the one before it from the same camera is still being looked at.

**Posters and screens**: an unknown face that stays for a moment gets one check by the vision model (`vlm`, see [vision](vision.md)): the whole picture and a close-up of the face, "a real person or a picture?". Pictures are remembered and ignored from then on. Without a vision model, an unknown face that does not move at all for three minutes (`perception.static_picture_s`) is taken for a picture.

**Events**: arrivals and departures become events for the [attention engine](proactive.md): `person_arrived` (an acquaintance), `person_left`, `stranger_appeared`, `user_left` and `user_returned` (after being away longer than `attention.absence_s`). Someone counts as gone after `perception.left_after_s` (60 s) out of view. With perception on, `user_returned` replaces the camera's guess that "someone may have come back".

**Strangers** are remembered for `people.stranger_retention_h` (24 hours) so the same stranger is not announced again, then deleted.

The dashboard's **People** page shows who is here now, and **Who is this?** names the faces in a camera's newest picture.

## Who said what

With `perception.speakers: true` (off by default), every sentence heard gets a speaker, worked out while speech recognition runs. The rules, in order:

| What the server finds | Speaker | Confidence |
|---|---|---|
| The voice is someone known, and the lips of that person's face move | that person | high |
| The voice clearly is someone known (a second or longer; `people.voice_threshold`, `voice_margin`) | that person, on screen or not | high |
| Only one face's lips clearly move (needs 15 or more pictures a second) | that face's person, or a stranger | medium |
| Only one known person is around, and the voice does not say otherwise | that person | medium |
| A voice nobody knows, and nobody visibly talking | `media` (a TV, radio or video) | low |
| Anything else | unknown | low |

Voices shorter than a second are not trusted on their own. The speaker goes with the transcript (`conversation.transcript` has `speaker: {person_id, name, role, confidence, on_screen, how}`), shows on the dashboard's **Conversations** page and the phone page, and the agent gets the line as `「…」 (Yukari, the user)`, `(a stranger)` or `(the TV or another device)`. With speakers on, the TV still gets answered until the [routing gate](#answering-only-what-is-meant-for-the-companion) is on too.

When voice, face and lips all agree on someone for a sentence of 1.5 s or longer, the sentence is kept (in memory only) as a new voice sample for them; the People page offers to add these (**Add N heard voice samples**). Nothing is added by itself.

## Answering only what is meant for the companion

With `routing.enabled: true` (off by default; it needs `perception.speakers`), each sentence goes one of three ways once its speaker is known:

| Who said it | When | Route |
|---|---|---|
| The user or an acquaintance | the companion's name was said (`routing.wake_names`; empty = the persona's names), or the companion spoke less than `routing.followup_s` (20 s) ago, or the speaker's face is turned to the companion (on a camera in `routing.facing_cameras`) | answered |
| The user or an acquaintance | none of these, and two or more people are around | **overheard**: not answered; the next reply gets it as "Overheard (not said to you): …" |
| The user or an acquaintance | none of these, alone in the room | answered (`routing.alone_means_addressed`) |
| A stranger | | overheard, and a `stranger_spoke` event (`routing.answer_strangers: true` answers them) |
| The TV, or a voice nobody knows | | only logged |

`routing.answer_acquaintances: false` answers only the user. The Conversations page and the phone page mark lines that were overheard or not for the companion; `routing.debug: true` sends each decision with its reasons to connected clients (`routing.decision`), like `attention.debug`. Off, everything heard is answered, as before.

## Glances: who is doing what

With a vision model (built in, see [vision](vision.md#built-in-vision-model), or any other) and `perception.glance: true` (off by default), the vision model glances at the cameras: every `perception.glance_s` seconds (1 s with the built-in model, 10 s with another one, which may cost money), the newest picture of a camera, with each tracked face boxed and lettered, and a short question answered in compact JSON:

```json
{"A":{"activity":"gaming","posture":"sitting","looking_at":"screen"},"scene":"living room, TV on","change":"sat down on the sofa"}
```

Activities come from a fixed list: `desk_computer`, `phone`, `gaming`, `reading`, `eating`, `cooking`, `watching_tv`, `lying_down`, `sleeping`, `exercising`, `talking`, `other`. A glance is skipped, never queued, while the one before is still running, and a picture that looks the same as the last one glanced at is not asked about again. The answers keep a state of who is doing what since when, which the agent gets in proactive turns (with `attention.decisions`) and through its tools; the **Vision** page shows it, and **Look now** asks for a short description of a camera's picture.

Measured with the built-in Mage-VL (FP8, fused) on an RTX 4070 Ti while faces were tracked at 15 pictures a second: a glance at one or two people takes about 1 s; with five people in the picture, about 1.5 to 3 s (the answer is longer).

## Timeline

With `timeline.enabled: true` (off by default) the glances become a timeline per person: consecutive glances with the same activity make an episode ("gaming since 20:10, 95 min"); a different activity must last `timeline.min_switch_s` (2 minutes) before it ends one. Without glances, the timeline records only present and away (every `timeline.sample_s`). Episodes are kept in `data/timeline.db` for `timeline.retention_days` (7). The **Timeline** page shows them per person.

**Rules** in `timeline.rules` turn long activities into `long_activity` events for the [attention engine](proactive.md), with the rule's `salience` (0.7, enough to speak; the usual limits still apply). Each fires once per episode. The defaults:

| Rule | Fires when |
|---|---|
| `gaming-2h` | anyone has been gaming for 2 hours |
| `computer-90min` | at the computer for 90 minutes without a break |
| `phone-in-bed-late` | on the phone lying down, between 00:30 and 05:00 |
| `back-after-4h` | the user is back after being away 4 hours or more (`when: after`) |

A rule has `activity`, `min_minutes`, optionally `person` (`user` or a name), `posture`, `after`/`before` (times of day) and `when` (`during`, or `after` for when such an episode ends). The agent can add watches of its own through its tools; the Timeline page lists and removes them.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `perception.enabled` | `false` | Recognise people; starts the engine |
| `perception.speakers` | `false` | Tell who said each sentence, and tell the agent |
| `routing.enabled` | `false` | Answer only what is meant for the companion (needs `perception.speakers`) |
| `routing.wake_names`, `followup_s`, `alone_means_addressed` | `[]`, `20`, `true` | When a sentence counts as said to the companion |
| `routing.answer_acquaintances`, `answer_strangers` | `true`, `false` | Whom it answers |
| `perception.device` | `auto` | `cuda`, `cpu` or `auto` |
| `perception.face_model` / `voice_model` | `buffalo_l` / `speaker-diarization-community-1` | Model folders in `builtin/perception/models` (`none` for no voices) |
| `perception.picture_check` | `true` | Ask the vision model about unknown faces |
| `perception.left_after_s` | `60` | Out of view this long = gone |
| `cameras[].perception_fps` | `10` | Pictures a second looked at, per server camera (0 = only those sent to the conversation) |
| `people.face_threshold`, `face_margin` | `0.45`, `0.10` | How alike a face must be |
| `people.voice_threshold`, `voice_margin` | `0.3`, `0.05` | How alike a voice must be |
| `people.stranger_retention_h` | `24` | Hours strangers are remembered |
| `perception.glance`, `glance_s` | `false`, empty (1 s built in, 10 s otherwise) | Glances at the cameras by the vision model |
| `timeline.enabled`, `rules`, `min_switch_s`, `sample_s` | `false`, four rules, `120`, `60` | The timeline and its rules |

All of them apply at once.
