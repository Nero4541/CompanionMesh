"""Update this installation to the newest release (``update-companion.bat`` / ``.sh``).

    uv run --no-sync python -m companion.updater [--check] [--extras-file PATH]

For an installation unpacked from a release package: asks GitHub for the
newest release, downloads this system's package and checks it against
``SHA256SUMS``, keeps the files it is about to replace in a backup
(``data/updates/backup-<version>-<time>.zip``, the last three are kept),
writes the new files and removes those the new release no longer has (by the
packages' ``RELEASE_FILES`` lists). What is yours is never touched: ``.env``,
``*.local.yaml``, ``data/``, ``.venv/``, your own personas.

For a git clone it runs ``git pull --ff-only`` instead.

Installing the new dependencies (``uv sync``) is left to the calling script,
after this process has ended: on Windows a running Python keeps its files in
use. The extras installed now are written to ``--extras-file`` so that
``uv sync`` keeps them.

Every update is logged in ``data/updates.jsonl``; the dashboard shows it.

Exit codes: 0 updated (run ``uv sync``), 3 nothing to update, 1 failed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from companion import __version__

ROOT = Path(__file__).resolve().parents[1]
REPO = os.environ.get("COMPANION_UPDATE_REPO", "Nero4541/CompanionMesh")
# The "latest release" document (GitHub's API format); another URL serves forks
# and mirrors (and tests).
API = os.environ.get("COMPANION_UPDATE_API", f"https://api.github.com/repos/{REPO}/releases/latest")
MANIFEST = "RELEASE_FILES"
KEEP_BACKUPS = 3
UP_TO_DATE = 3

# Never written or removed by an update: they are the user's.
_PROTECTED = re.compile(
    r"^(\.env(\..*)?|\.venv/.*|data/.*|build/.*|.*\.local\.ya?ml|personas/(?!default\.yaml$).*)$"
)


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    url: str
    published: str
    notes: str
    assets: dict[str, str] = field(default_factory=dict)  # name -> download URL


def version_key(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text.split("-")[0])
    return tuple(int(n) for n in numbers[:3]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def _trust_system_certificates() -> None:
    with contextlib.suppress(ImportError):
        import truststore

        truststore.inject_into_ssl()


def _get(url: str, *, timeout: float = 30, accept: str | None = None) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"companion-updater/{__version__}"}
    )
    if accept:
        request.add_header("Accept", accept)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_release(api: str | None = None, *, timeout: float = 15) -> Release:
    """The newest published release (GitHub's API, or COMPANION_UPDATE_API)."""
    try:
        data = json.loads(_get(api or API, timeout=timeout, accept="application/vnd.github+json"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"cannot ask GitHub for the newest release: {exc}") from exc
    tag = str(data.get("tag_name") or "")
    return Release(
        version=tag.removeprefix("v"),
        tag=tag,
        url=str(data.get("html_url") or ""),
        published=str(data.get("published_at") or ""),
        notes=str(data.get("body") or ""),
        assets={a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
    )


def package_names(version: str, system: str | None = None) -> list[str]:
    """This system's server package in a release, under its name and the name before 1.2.0."""
    system = system or ("windows" if sys.platform == "win32" else "linux")
    suffix = "windows.zip" if system == "windows" else "linux.tar.gz"
    return [f"companion-mesh-{version}-{suffix}", f"companion-server-{version}-{suffix}"]


def package_name(version: str, system: str | None = None) -> str:
    return package_names(version, system)[0]


def read_package(name: str, data: bytes) -> dict[str, bytes]:
    """The files of a server package, without its top folder."""
    files: dict[str, bytes] = {}
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    files[info.filename] = zf.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.isfile():
                    source = tar.extractfile(member)
                    assert source is not None
                    files[member.name] = source.read()
    out: dict[str, bytes] = {}
    for path, content in files.items():
        parts = Path(path).parts
        if len(parts) < 2 or ".." in parts or Path(path).is_absolute():
            raise UpdateError(f"unexpected file in the package: {path}")
        out["/".join(parts[1:])] = content
    if "companion/__init__.py" not in out:
        raise UpdateError("the package holds no Companion Server")
    return out


def _executable(name: str) -> bool:
    return name.endswith(".sh")


def apply(root: Path, files: dict[str, bytes], *, backup: Path) -> tuple[list[str], list[str]]:
    """Write ``files`` into ``root``; remove what the old release had and this one has not.

    Returns (written, removed). The previous versions of both go into ``backup`` first.
    """
    new_names = {n for n in files if not _PROTECTED.match(n)}
    old_manifest = root / MANIFEST
    old_names: set[str] = set()
    if old_manifest.is_file():
        old_names = {
            line.strip()
            for line in old_manifest.read_text(encoding="utf-8").splitlines()
            if line.strip() and not _PROTECTED.match(line.strip())
        }
    removed = sorted(n for n in old_names - new_names if (root / n).is_file())
    changed = sorted(
        n for n in new_names if not (root / n).is_file() or (root / n).read_bytes() != files[n]
    )

    backup.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in [*changed, *removed]:
            if (root / name).is_file():
                zf.write(root / name, name)

    for name in changed:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".update-tmp")
        tmp.write_bytes(files[name])
        if _executable(name) and os.name != "nt":
            tmp.chmod(0o755)
        os.replace(tmp, target)
    for name in removed:
        (root / name).unlink(missing_ok=True)
    if MANIFEST not in files:  # an older package without a list: remember this one
        old_manifest.write_text("\n".join(sorted(new_names)) + "\n", encoding="utf-8")
    return changed, removed


def installed_extras(root: Path) -> list[str]:
    """The optional dependency groups installed in ``.venv``, for ``uv sync``."""
    venv = root / ".venv"
    site = [p for p in (venv / "Lib" / "site-packages", *venv.glob("lib/python*/site-packages"))]
    found = []
    for extra, package in (("opus", "av"),):
        if any((s / package).is_dir() for s in site):
            found.append(extra)
    return found


def data_dir(root: Path) -> Path:
    try:
        from companion.core.config import load_config

        return load_config(project_root=root).storage.data_dir
    except Exception:
        return root / "data"


def history(root: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    path = data_dir(root) / "updates.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out[-limit:][::-1]


def record(root: Path, entry: dict[str, Any]) -> None:
    path = data_dir(root) / "updates.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"time": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), **entry}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _mark_synced(root: Path, code: int) -> None:
    """Note the result of ``uv sync`` in the newest log entry."""
    path = data_dir(root) / "updates.jsonl"
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return
    entry = json.loads(lines[-1])
    entry["dependencies"] = "installed" if code == 0 else f"uv sync failed ({code})"
    if code != 0:
        entry["ok"] = False
    lines[-1] = json.dumps(entry, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def server_running(root: Path) -> str | None:
    """The address of a server of this installation that answers now, if any."""
    try:
        from companion.core.config import load_config

        server = load_config(project_root=root).server
        host = "127.0.0.1" if server.host in ("0.0.0.0", "::", "") else server.host
        url = f"http://{host}:{server.port}/health"
    except Exception:
        url = "http://127.0.0.1:8765/health"
    try:
        _get(url, timeout=2)
    except OSError:
        return None
    return url.removesuffix("/health")


def _prune_backups(folder: Path) -> None:
    backups = sorted(folder.glob("backup-*.zip"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)


def update_from_release(root: Path, release: Release, out=print) -> dict[str, Any]:
    name = next((n for n in package_names(release.version) if n in release.assets), None)
    if name is None or "SHA256SUMS" not in release.assets:
        wanted = package_name(release.version)
        raise UpdateError(f"release {release.tag} has no {wanted} (or no SHA256SUMS)")
    out(f"Downloading {name} ...")
    data = _get(release.assets[name], timeout=300)
    sums = _get(release.assets["SHA256SUMS"]).decode().replace("\r", "")
    expected = re.search(rf"^([0-9a-f]{{64}})\s+\*?{re.escape(name)}$", sums, re.M)
    if not expected or hashlib.sha256(data).hexdigest() != expected[1]:
        raise UpdateError("the download does not match SHA256SUMS; nothing was changed")
    out("Checksum ok.")
    files = read_package(name, data)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = data_dir(root) / "updates"
    backup = folder / f"backup-{__version__}-{stamp}.zip"
    written, removed = apply(root, files, backup=backup)
    _prune_backups(folder)
    return {"files_written": len(written), "files_removed": len(removed), "backup": backup.name}


def update_git(root: Path, out=print) -> dict[str, Any]:
    before = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    pull = subprocess.run(["git", "pull", "--ff-only"], cwd=root, capture_output=True, text=True)
    out((pull.stdout + pull.stderr).strip())
    if pull.returncode != 0:
        raise UpdateError("git pull --ff-only failed (local changes or another branch?)")
    after = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    return {"commit_from": before, "commit_to": after, "changed": before != after}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="companion.updater", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="only say whether there is a newer release"
    )
    parser.add_argument("--extras-file", type=Path, help="write the arguments for uv sync here")
    parser.add_argument("--synced", type=int, help=argparse.SUPPRESS)  # uv sync's exit code
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root: Path = args.root
    _trust_system_certificates()

    if args.synced is not None:
        _mark_synced(root, args.synced)
        return 0

    git = (root / ".git").exists()
    try:
        release = None if git else latest_release()
    except UpdateError as exc:
        print(f"error: {exc}")
        return 1
    if args.check:
        if git:
            print(f"This is a git clone at {__version__}; git pull updates it.")
        elif release and is_newer(release.version, __version__):
            print(f"{release.version} is available (this is {__version__}): {release.url}")
        else:
            print(
                f"Up to date: {__version__}"
                f" (newest release: {release.version if release else '?'})."
            )
        return 0

    running = server_running(root)
    if running:
        print(
            f"The server is running ({running}). Stop it first"
            " (close its window or Ctrl+C), then run this again."
        )
        return 1
    if not git and release is not None and not is_newer(release.version, __version__):
        print(f"Up to date: {__version__} (newest release: {release.version}).")
        return UP_TO_DATE

    extras = installed_extras(root)
    entry: dict[str, Any] = {"from": __version__, "method": "git" if git else "release"}
    try:
        if git:
            print("Updating the git clone ...")
            result = update_git(root)
            if not result["changed"]:
                print("Already up to date.")
                return UP_TO_DATE
            entry.update(result, to=_version_on_disk(root))
        else:
            assert release is not None
            print(f"Updating {__version__} -> {release.version}")
            entry.update(update_from_release(root, release), to=release.version, notes=release.url)
    except (UpdateError, OSError) as exc:
        entry.update(ok=False, error=str(exc))
        record(root, entry)
        print(f"error: {exc}")
        return 1
    entry["ok"] = True
    entry["extras"] = extras
    record(root, entry)
    if args.extras_file:
        uv_args = ["--frozen", *(f"--extra {e}" for e in extras)]
        args.extras_file.write_text(" ".join(uv_args) + "\n", encoding="utf-8")
    print(f"Files updated to {entry['to']}. Installing dependencies next (uv sync).")
    return 0


def _version_on_disk(root: Path) -> str:
    text = (root / "companion" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match[1] if match else "?"


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
    sys.exit(1)
