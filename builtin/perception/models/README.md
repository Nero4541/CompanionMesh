# Perception models

Each folder here is one model for the built-in perception engine:

- `buffalo_l/`: InsightFace's face models (`det_10g.onnx`, `w600k_r50.onnx`,
  `2d106det.onnx`). Downloaded here on the first start when missing. For
  non-commercial use only.
- `speaker-diarization-community-1/`: pyannote's pipeline, of which the engine
  uses the WeSpeaker voice model. It is gated on Hugging Face: accept its
  conditions there, then put `HF_TOKEN=<your token>` in the server's `.env`
  (it is downloaded here on the next start), or download the repository into
  this folder yourself. See docs/people.md.

`perception.face_model` and `perception.voice_model` name the folders to use.
`.cache/` holds what else the engine downloads. Nothing in this folder is part
of the repository, and updates leave it alone.
