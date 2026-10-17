# Voice models

Each folder here is one Irodori-TTS checkpoint for the built-in voice: a
`model.safetensors`, and its `tokenizer/` folder if it comes with one.

`builtin.tts.checkpoint` names the folder to use; **Speech** in the dashboard
lists the folders and switches between them. A Hugging Face repository
(`owner/name`) that is not here yet is downloaded here first, as a folder of
that name (`Aratako/Irodori-TTS-v4.1-Small-MF` becomes
`Irodori-TTS-v4.1-Small-MF/`).

`.cache/` holds what else Irodori downloads: its codec (Semantic-DACVAE) and the
watermarker. Nothing in this folder is part of the repository, and updates leave
it alone.

Voices (reference clips and speaker embeddings) go in `../voices`.
