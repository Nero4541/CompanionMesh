"""Tailscale's static builds for boards without a package manager (pkgs.tailscale.com).

The server downloads the newest build for a board's architecture, checks it
against the SHA-256 Tailscale publishes next to it and keeps it in
``data/tailscale/`` (the newest one per architecture), from where boards get
it: over USB from the installer, or over their own connection
(``GET /v1/edge/tailscale/<name>``, device token). Without Internet the newest
one downloaded before is used.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

from companion.core.logging import kv

log = logging.getLogger(__name__)

INDEX_URL = "https://pkgs.tailscale.com/stable/?mode=json"
BASE_URL = "https://pkgs.tailscale.com/stable/"
PACKAGE = re.compile(r"^tailscale_([0-9][0-9.]*)_([a-z0-9]+)\.tgz$")
TIMEOUT_S = 120.0


class PackageError(Exception):
    """No Tailscale package for the board; the message says why."""


@dataclass
class Package:
    name: str
    path: Path
    sha256: str
    version: str


def folder(data_dir: Path) -> Path:
    return data_dir / "tailscale"


def _version(name: str) -> tuple[int, ...]:
    match = PACKAGE.match(name)
    return tuple(int(p) for p in match[1].split(".") if p.isdigit()) if match else ()


def cached(data_dir: Path, name: str) -> Package | None:
    """A package downloaded and checked before, by its file name."""
    match = PACKAGE.match(name)
    path = folder(data_dir) / name
    sums = path.with_name(name + ".sha256")
    if match is None or not path.is_file() or not sums.is_file():
        return None
    return Package(name, path, sums.read_text(encoding="utf-8").strip(), match[1])


def newest_cached(data_dir: Path, arch: str) -> Package | None:
    names = [p.name for p in folder(data_dir).glob(f"tailscale_*_{arch}.tgz")]
    for name in sorted(names, key=_version, reverse=True):
        found = cached(data_dir, name)
        if found is not None:
            return found
    return None


async def package(
    arch: str, data_dir: Path, *, transport: httpx.AsyncBaseTransport | None = None
) -> Package:
    """The newest Tailscale for ``arch`` (pkgs.tailscale.com's name, e.g. riscv64)."""
    async with httpx.AsyncClient(
        transport=transport, timeout=TIMEOUT_S, follow_redirects=True
    ) as client:
        try:
            index = (await client.get(INDEX_URL)).raise_for_status().json()
        except (httpx.HTTPError, ValueError) as exc:
            found = newest_cached(data_dir, arch)
            if found is not None:
                log.info(
                    "tailscale index unreachable; using the one downloaded before",
                    extra=kv(error=str(exc), package=found.name),
                )
                return found
            raise PackageError(f"cannot reach pkgs.tailscale.com: {exc}") from exc
        name = (index.get("Tarballs") or {}).get(arch)
        if not isinstance(name, str) or not PACKAGE.match(name):
            raise PackageError(f"Tailscale has no build for {arch!r} boards")
        found = cached(data_dir, name)
        if found is not None:
            return found
        try:
            sums = (await client.get(BASE_URL + name + ".sha256")).raise_for_status().text
            expected = sums.split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise PackageError(f"{name}.sha256 does not hold a SHA-256")
            target = folder(data_dir)
            target.mkdir(parents=True, exist_ok=True)
            part = target / (name + ".part")
            digest = hashlib.sha256()
            log.info("downloading tailscale", extra=kv(package=name))
            async with client.stream("GET", BASE_URL + name) as response:
                response.raise_for_status()
                with part.open("wb") as out:
                    async for block in response.aiter_bytes(1 << 16):
                        digest.update(block)
                        out.write(block)
        except httpx.HTTPError as exc:
            raise PackageError(f"downloading {name} failed: {exc}") from exc
    if digest.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise PackageError(f"{name} does not match the checksum Tailscale publishes")
    path = target / name
    part.replace(path)
    path.with_name(name + ".sha256").write_text(expected + "\n", encoding="utf-8")
    for old in target.glob(f"tailscale_*_{arch}.tgz"):
        if old.name != name:  # only the newest is kept
            old.unlink(missing_ok=True)
            old.with_name(old.name + ".sha256").unlink(missing_ok=True)
    return Package(name, path, expected, PACKAGE.match(name)[1])  # type: ignore[index]
