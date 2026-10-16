"""Build the release packages from a git tag (or any commit).

    uv run python scripts/package_release.py v1.1.0

Writes to build/release/:

    companion-server-<version>-windows.zip     the server, start/update-companion.bat
    companion-server-<version>-linux.tar.gz    the server, start/update-companion.sh
    companion-edge-<version>.tar.gz            the edge client for Linux boards
    SHA256SUMS

The packages hold the committed files as of the tag (line endings as
.gitattributes says, e.g. CRLF for .bat), without what only development needs:
CI settings and the dashboard's sources (its built files are included).
Timestamps are the commit's, so the same tag gives the same packages.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import subprocess
import sys
import tarfile
import time
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "release"

DEV_ONLY = (".github/", ".gitattributes", ".gitignore")
DASHBOARD = "client/dashboard/"
DASHBOARD_BUILD = "client/dashboard/dist/"
EDGE = "client/edge/"
MANIFEST = "RELEASE_FILES"  # see companion/updater.py


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def version(ref: str, pyproject: str) -> str:
    return tomllib.loads(git("show", f"{ref}:{pyproject}").decode())["project"]["version"]


def members(ref: str) -> tuple[list[tuple[tarfile.TarInfo, bytes]], int]:
    """Every committed file at ``ref`` (as git archive exports it) and the commit time."""
    archive = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", ref)))
    files = []
    for info in archive.getmembers():
        if info.isfile():
            data = archive.extractfile(info).read()  # type: ignore[union-attr]
            files.append((info, data))
    stamp = int(git("log", "-1", "--format=%ct", ref).decode().strip())
    return files, stamp


def server_file(name: str, system: str) -> bool:
    if name.startswith(DEV_ONLY):
        return False
    if name.startswith(DASHBOARD) and not name.startswith(DASHBOARD_BUILD):
        return False
    if "/" not in name and name.endswith((".bat", ".sh")):  # start and update scripts
        return name.endswith(".bat" if system == "windows" else ".sh")
    return True


def executable(name: str) -> bool:
    return name.endswith(".sh")


def write_tar_gz(path: Path, prefix: str, files: list[tuple[str, bytes]], stamp: int) -> None:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in files:
            info = tarfile.TarInfo(f"{prefix}/{name}")
            info.size = len(data)
            info.mtime = stamp
            info.mode = 0o755 if executable(name) else 0o644
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    with path.open("wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=stamp) as gz:
        gz.write(raw.getvalue())


def write_zip(path: Path, prefix: str, files: list[tuple[str, bytes]], stamp: int) -> None:
    date = time.gmtime(stamp)[:6]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name, data in files:
            info = zipfile.ZipInfo(f"{prefix}/{name}", date_time=date)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if executable(name) else 0o644) << 16
            zf.writestr(info, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ref", help="tag or commit to package, e.g. v1.1.0")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    server_version = version(args.ref, "pyproject.toml")
    edge_version = version(args.ref, "client/edge/pyproject.toml")
    files, stamp = members(args.ref)
    names = sorted((info.name, data) for info, data in files)
    args.out.mkdir(parents=True, exist_ok=True)

    built: list[Path] = []
    server = f"companion-server-{server_version}"
    for system in ("windows", "linux"):
        chosen = [(n, d) for n, d in names if server_file(n, system)]
        # The package's own file list: the updater removes what a newer one no longer has.
        listing = "\n".join([*(n for n, _ in chosen), MANIFEST]) + "\n"
        chosen = sorted([*chosen, (MANIFEST, listing.encode())])
        if system == "windows":
            path = args.out / f"{server}-windows.zip"
            write_zip(path, server, chosen, stamp)
        else:
            path = args.out / f"{server}-linux.tar.gz"
            write_tar_gz(path, server, chosen, stamp)
        built.append(path)

    edge = f"companion-edge-{edge_version}"
    edge_files = [(n[len(EDGE) :], d) for n, d in names if n.startswith(EDGE)]
    path = args.out / f"{edge}.tar.gz"
    write_tar_gz(path, edge, edge_files, stamp)
    built.append(path)

    sums = args.out / "SHA256SUMS"
    sums.write_text(
        "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in built),
        encoding="utf-8",
        newline="\n",
    )
    for p in [*built, sums]:
        print(f"{p.stat().st_size:>10,}  {p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
