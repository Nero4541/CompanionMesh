"""Updating the edge client from its server (``device.update``).

Only for a client set up by the server's installer, whose files are laid out as

    <dir>/current -> releases/<version>      what the service runs
    <dir>/releases/<version>/companion_edge  one folder per version

An update downloads the new version from the server it is connected to (with
its own token, over the same TLS trust), checks its SHA-256, unpacks it next
to the others, points ``current`` at it and exits, so the service starts the
new version. ``update.pending`` remembers the version before; the new version
removes it once it is connected. If it cannot connect (or does not even start)
a few times in a row, the service goes back to the version before by itself
(see the installer's ``service.sh``).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import ssl
import tarfile
import urllib.request
from pathlib import Path

PENDING = "update.pending"  # holds the version to go back to
TRIES = "update.tries"
VERSION = re.compile(r"^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$")


class UpdateError(Exception):
    pass


def layout(package_dir: Path | None = None) -> tuple[Path, Path] | None:
    """(the install folder, this release's folder), or None if not installed by the server."""
    release = (package_dir or Path(__file__).resolve().parent).parent
    if release.parent.name != "releases":
        return None
    base = release.parent.parent
    current = base / "current"
    if not current.is_symlink():
        return None
    return base, release


def package_url(realtime_url: str, version: str) -> str:
    """The server's package URL, from the realtime URL the client is connected to."""
    match = re.match(r"^(ws|wss)://([^/]+)/", realtime_url)
    if not match:
        raise UpdateError(f"cannot tell the server's address from {realtime_url!r}")
    scheme = "https" if match[1] == "wss" else "http"
    return f"{scheme}://{match[2]}/v1/edge/package/{version}.tar.gz"


def download(url: str, token: str | None, context: ssl.SSLContext | None) -> bytes:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60, context=context) as response:
        return response.read()


def install(data: bytes, sha256: str, version: str, base: Path) -> Path:
    """Unpack a checked package as ``releases/<version>``; returns that folder."""
    if not VERSION.match(version):
        raise UpdateError(f"not a version: {version!r}")
    if hashlib.sha256(data).hexdigest() != sha256.lower():
        raise UpdateError("the download does not match its checksum")
    target = base / "releases" / version
    staging = base / "releases" / f".{version}.new"
    if staging.exists():
        _remove(staging)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        for member in tar.getmembers():
            name = Path(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or name.parts[0] != "companion_edge"
            ):
                raise UpdateError(f"unexpected file in the package: {member.name}")
            out = staging / name
            out.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            assert source is not None
            out.write_bytes(source.read())
    if not (staging / "companion_edge" / "__main__.py").is_file():
        raise UpdateError("the package holds no edge client")
    if target.exists():
        _remove(target)
    staging.rename(target)
    return target


def switch(base: Path, version: str, previous: str) -> None:
    """Point ``current`` at ``version`` and remember ``previous`` for going back."""
    (base / PENDING).write_text(previous + "\n", encoding="utf-8")
    (base / TRIES).unlink(missing_ok=True)
    link = base / "current.new"
    link.unlink(missing_ok=True)
    link.symlink_to(Path("releases") / version)
    os.replace(link, base / "current")


def confirm(base: Path) -> str | None:
    """Called once connected: the update worked. Returns the version it came from."""
    pending = base / PENDING
    if not pending.is_file():
        return None
    previous = pending.read_text(encoding="utf-8").strip()
    pending.unlink(missing_ok=True)
    (base / TRIES).unlink(missing_ok=True)
    return previous


def pending(base: Path) -> bool:
    return (base / PENDING).is_file()


def _remove(path: Path) -> None:
    for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    path.rmdir()
