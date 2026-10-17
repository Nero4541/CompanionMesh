# Speech recognition models

Each folder here is one model for the built-in speech recognition, in
[CTranslate2](https://github.com/OpenNMT/CTranslate2) format (faster-whisper's):
`model.bin`, `config.json` and the tokenizer files.

`builtin.stt.model` names the folder to use; **Speech** in the dashboard lists
the folders and switches between them. A Whisper size (`large-v3-turbo`,
`small`, ...) or a Hugging Face repository (`owner/name`) that is not here yet
is downloaded here first, as a folder of that name.

To use a model of your own, for example one fine-tuned for Cantonese, put its
folder here. A model in the usual Transformers format has to be converted once:

```
ct2-transformers-converter --model <its repository or folder> --output_dir <here>/<name> --copy_files tokenizer.json preprocessor_config.json --quantization float16
```

`.cache/` holds what else the engine downloads. Nothing in this folder is part
of the repository, and updates leave it alone.
