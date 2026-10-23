# Vision models

Each folder here is one model for the built-in vision engine, as its Hugging
Face repository holds it (`config.json`, the weights, the processor and the
model's own code).

`builtin.vlm.model: auto` uses `Mage-VL-FP8-W8A8-W8A16/` on GPUs that run FP8
(compute capability 8.9 and newer) and `Mage-VL/` (microsoft/Mage-VL) on older
ones; a repository that is not here yet is downloaded here first. Name a folder
to use a model of your own. **Vision** in the dashboard lists the folders and
switches between them.

`.cache/` holds what else the engine needs: the model's code as Transformers
loads it, and `native/sm<NN>/`, the fused FP8 kernels built for your GPU on the
first start. Nothing in this folder is part of the repository, and updates
leave it alone.
