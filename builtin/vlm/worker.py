"""A vision model for the Companion Server: the built-in VLM engine (Mage-VL by default).

Serves the OpenAI ``POST /v1/chat/completions`` API (text and ``image_url``
parts with data: URIs) on 127.0.0.1, so the server talks to it like to any
self-hosted or cloud vision model. The server starts this with the Python of
builtin/vlm/.venv and its own process id: when the server ends, however it
ends, this process exits at once.

Model: ``auto`` picks Mage-VL in FP8 (ajh-code/Mage-VL-FP8-W8A8-W8A16) on GPUs
that run FP8 (compute capability 8.9 and up: Ada, Hopper, Blackwell), and the
original microsoft/Mage-VL in BF16 (8.0 and up) or FP16 on older ones. A folder
in models/ or a Hugging Face repository can be named instead; one that is not
in models/ yet is downloaded there first.

FP8 decodes about 2.7 times faster with three small fused CUDA kernels, which
are compiled once for this GPU (a few minutes) and kept in models/.cache. They
need the CUDA toolkit matching PyTorch's CUDA and, on Windows, Visual Studio's
C++ build tools; without them the model runs unfused, which is slower but the
same.
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("builtin-vlm")

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
FP8_REPO = "ajh-code/Mage-VL-FP8-W8A8-W8A16"
BASE_REPO = "microsoft/Mage-VL"
FUSED_FLAGS = ("MAGE_VL_SMALLM_BACKEND", "MAGE_VL_FP8_FUSED_GATE_UP", "MAGE_VL_FP8_FUSED_QKV")
NATIVE_FEATURES = ("smallm_fp8_gemv", "fused_fp8_gate_up", "fused_fp8_qkv")


def exit_with_parent(pid: int) -> None:
    """Exit as soon as process ``pid`` (the server) ends."""

    def watch() -> None:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if handle:
                kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        else:
            while os.getppid() == pid:
                time.sleep(1)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def use_system_certificates() -> None:
    try:
        import truststore
    except ImportError:
        return
    truststore.inject_into_ssl()


def pick_model(name: str, device: str, precision: str) -> str:
    """The repository or folder to load: ``name``, or for ``auto`` the one for this GPU."""
    import torch

    if name != "auto":
        return name
    cuda = device != "cpu" and torch.cuda.is_available()
    cap = torch.cuda.get_device_capability() if cuda else (0, 0)
    if cuda and precision in ("auto", "fp8") and cap >= (8, 9):
        return FP8_REPO
    return BASE_REPO


def pick_dtype(folder: Path, device: str, precision: str) -> str:
    """fp8 for a checkpoint stored in FP8 (it needs compute capability 8.9), else bf16/fp16/fp32."""
    import json

    import torch

    cuda = device != "cpu" and torch.cuda.is_available()
    cap = torch.cuda.get_device_capability() if cuda else (0, 0)
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if config.get("mage_vl_quantization"):
        if not cuda or cap < (8, 9):
            raise SystemExit(
                f"{folder.name} is stored in FP8, which needs an NVIDIA GPU of compute capability "
                f"8.9 or newer (this one: {cap[0]}.{cap[1]}); use microsoft/Mage-VL instead"
            )
        return "fp8"
    if not cuda:
        return "fp32"
    if precision in ("bf16", "fp16"):
        return precision
    return "bf16" if cap >= (8, 0) else "fp16"


def model_folder(name: str) -> Path:
    """models/<name>, downloaded there first when it is not there yet."""
    given = Path(name).expanduser()
    if given.is_absolute():
        return given
    folder = MODELS / name.split("/")[-1]
    if not (folder / "config.json").is_file():
        if "/" not in name:
            raise SystemExit(f"no model in {folder}: put one there, or name owner/repository")
        from huggingface_hub import snapshot_download

        log.info("downloading %s to %s (several GB)", name, folder)
        snapshot_download(name, local_dir=str(folder), token=os.environ.get("HF_TOKEN") or None)
    return folder


def _msvc_environment() -> dict[str, str] | None:
    """The environment of Visual Studio's x64 build tools (Windows), or None."""
    if shutil.which("cl"):
        return dict(os.environ)
    root = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = Path(root) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    candidates: list[Path] = []
    if vswhere.is_file():
        out = subprocess.run(
            [str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if out:
            candidates.append(Path(out))
    for edition in ("Community", "Professional", "Enterprise", "BuildTools"):
        for year in ("2022", "2019"):
            base = os.environ.get("ProgramFiles", r"C:\Program Files")
            candidates.append(Path(base) / "Microsoft Visual Studio" / year / edition)
    for install in candidates:
        bat = install / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
        if bat.is_file():
            out = subprocess.run(
                f'"{bat}" >nul && set', shell=True, capture_output=True, text=True, check=False
            ).stdout
            env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if env:
                return env
    return None


def _cuda_home(version: str) -> Path | None:
    """The CUDA toolkit matching PyTorch's CUDA ``version`` (e.g. 13.0)."""
    major_minor = "_".join(version.split(".")[:2])
    for key in (f"CUDA_PATH_V{major_minor}", "CUDA_HOME", "CUDA_PATH"):
        value = os.environ.get(key)
        if value and (Path(value) / "bin").is_dir():
            nvcc = Path(value) / "bin" / ("nvcc.exe" if sys.platform == "win32" else "nvcc")
            if nvcc.is_file() and (key.startswith("CUDA_PATH_V") or version in str(value)):
                return Path(value)
    if sys.platform == "win32":
        default = Path(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA") / f"v{version}"
    else:
        default = Path(f"/usr/local/cuda-{version}")
    return default if (default / "bin").is_dir() else None


def prepare_fused_kernels(folder: Path, wanted: bool) -> str:
    """Build the fused FP8 kernels for this GPU if possible; how the model will decode."""
    import torch

    def plain(reason: str) -> str:
        for flag in FUSED_FLAGS:
            os.environ[flag] = "off" if flag.endswith("BACKEND") else "0"
        return f"unfused ({reason})"

    if not wanted:
        return plain("builtin.vlm.fused is off")
    if not (folder / "fp8_decode_runtime.py").is_file():
        return plain("this model has no fused kernels")
    cuda = torch.version.cuda
    if not cuda:
        return plain("PyTorch has no CUDA")
    home = _cuda_home(cuda)
    if home is None:
        return plain(f"CUDA toolkit {cuda} not found (set CUDA_HOME)")
    if sys.platform == "win32":
        env = _msvc_environment()
        if env is None:
            return plain("Visual Studio C++ build tools not found")
        os.environ.update(env)
    major, minor = torch.cuda.get_device_capability()
    os.environ["CUDA_HOME"] = str(home)
    os.environ["PATH"] = os.pathsep.join(
        [str(home / "bin"), str(Path(sys.executable).parent), os.environ.get("PATH", "")]
    )
    os.environ["TORCH_CUDA_ARCH_LIST"] = f"{major}.{minor}"
    os.environ.setdefault("MAX_JOBS", "8")
    build = MODELS / ".cache" / "native" / f"sm{major}{minor}"
    os.environ["MAGE_VL_NATIVE_BUILD_DIR"] = str(build)
    import importlib.util

    spec = importlib.util.spec_from_file_location("mage_vl_fp8", folder / "fp8_decode_runtime.py")
    assert spec is not None and spec.loader is not None
    runtime = importlib.util.module_from_spec(spec)
    started = time.monotonic()
    try:
        spec.loader.exec_module(runtime)
        runtime.configure_fp8_decode_sources(str(folder))
        for feature in NATIVE_FEATURES:
            runtime._load_extension(feature)
    except Exception as exc:  # a compiler error: run unfused rather than not at all
        log.warning("fused kernels not built: %s", exc)
        return plain(f"building the kernels failed: {str(exc)[:200]}")
    return f"fused (sm {major}.{minor}, ready in {time.monotonic() - started:.0f} s)"


def decode_image(url: str) -> Any:
    from PIL import Image

    if not url.startswith("data:"):
        raise ValueError("only data: URIs are accepted for images")
    payload = url.split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
        return image.convert("RGB")


class Model:
    def __init__(self, folder: Path, dtype: str, device: str, max_side: int) -> None:
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        self.torch = torch
        self.max_side = max_side
        self.processor = AutoProcessor.from_pretrained(str(folder), trust_remote_code=True)
        torch_dtype: Any = {
            "fp8": "auto",
            "bf16": torch.bfloat16,
            "fp16": torch.float16,
            "fp32": torch.float32,
        }[dtype]
        on = "cuda" if device != "cpu" and torch.cuda.is_available() else "cpu"
        self.model = AutoModelForCausalLM.from_pretrained(
            str(folder), trust_remote_code=True, dtype=torch_dtype, device_map=on
        ).eval()
        self.device = on
        self.lock = threading.Lock()

    def _shrink(self, image: Any) -> Any:
        side = max(image.size)
        if side <= self.max_side:
            return image
        scale = self.max_side / side
        return image.resize((round(image.width * scale), round(image.height * scale)))

    def chat(
        self, messages: list[dict[str, Any]], max_tokens: int, temperature: float
    ) -> dict[str, Any]:
        torch = self.torch
        convo, images = [], []
        for message in messages:
            content = message.get("content")
            parts: list[dict[str, Any]] = []
            if isinstance(content, str):
                parts.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        parts.append({"type": "text", "text": str(part.get("text") or "")})
                    elif part.get("type") == "image_url":
                        url = (part.get("image_url") or {}).get("url") or ""
                        images.append(self._shrink(decode_image(url)))
                        parts.append({"type": "image"})
            convo.append({"role": str(message.get("role") or "user"), "content": parts})
        text = self.processor.apply_chat_template(convo, tokenize=False, add_generation_prompt=True)
        kwargs: dict[str, Any] = {"text": [text], "return_tensors": "pt", "padding": True}
        if images:
            kwargs["images"] = images
        inputs = self.processor(**kwargs)
        inputs = {
            k: (v.to(self.model.device) if hasattr(v, "to") else v) for k, v in inputs.items()
        }
        for key in ("pixel_values", "pixel_values_videos"):
            if key in inputs:
                inputs[key] = inputs[key].to(self.model.dtype)
        prompt_tokens = int(inputs["input_ids"].shape[1])
        sampling: dict[str, Any] = {"do_sample": False}
        if temperature > 0:
            sampling = {"do_sample": True, "temperature": temperature}
        started = time.perf_counter()
        with self.lock, torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_tokens, **sampling)
        seconds = time.perf_counter() - started
        new = out[0, prompt_tokens:]
        answer = self.processor.tokenizer.decode(new, skip_special_tokens=True).strip()
        return {
            "text": answer,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": int(new.shape[0]),
            "seconds": seconds,
        }


def create_app(model: Model, name: str, info: dict[str, Any]) -> FastAPI:
    app = FastAPI(title="Built-in vision model")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "model": name, **info}

    @app.get("/v1/models")
    def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": name, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> Any:
        body = await request.json()
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="messages are needed")
        max_tokens = int(body.get("max_tokens") or body.get("max_completion_tokens") or 256)
        temperature = float(body.get("temperature") or 0.0)
        try:
            import asyncio

            result = await asyncio.to_thread(model.chat, messages, max_tokens, temperature)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ident, created = f"chatcmpl-{uuid.uuid4().hex[:12]}", int(time.time())
        if body.get("stream"):
            # Streaming clients get the whole answer as one chunk (it is generated first).
            import json

            from fastapi.responses import StreamingResponse

            def chunk(delta: dict[str, Any], finish: str | None) -> str:
                data = {
                    "id": ident,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": name,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            parts = [
                chunk({"role": "assistant", "content": result["text"]}, None),
                chunk({}, "stop"),
                "data: [DONE]\n\n",
            ]
            return StreamingResponse(iter(parts), media_type="text/event-stream")
        return {
            "id": ident,
            "object": "chat.completion",
            "created": created,
            "model": name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result["text"]},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": result["prompt_tokens"],
                "completion_tokens": result["completion_tokens"],
                "total_tokens": result["prompt_tokens"] + result["completion_tokens"],
            },
            "timing": {"seconds": round(result["seconds"], 3)},
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--model", default="auto")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--precision", choices=["auto", "fp8", "bf16", "fp16"], default="auto")
    parser.add_argument("--fused", choices=["on", "off"], default="on")
    parser.add_argument("--max-side", type=int, default=1280)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    exit_with_parent(args.parent_pid)
    use_system_certificates()
    folder = model_folder(pick_model(args.model, args.device, args.precision))
    dtype = pick_dtype(folder, args.device, args.precision)
    decode = "unfused"
    if dtype == "fp8":
        decode = prepare_fused_kernels(folder, args.fused == "on")
        log.info("decoding %s", decode)
    started = time.monotonic()
    model = Model(folder, dtype, args.device, args.max_side)
    log.info(
        "loaded %s (%s) on %s in %.1f s",
        folder.name,
        dtype,
        model.device,
        time.monotonic() - started,
    )
    # One small answer first, so a server that answers /health is ready.
    model.chat([{"role": "user", "content": "Say OK."}], 4, 0.0)
    info = {"folder": folder.name, "dtype": dtype, "device": model.device, "decode": decode}
    uvicorn.run(create_app(model, folder.name, info), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
