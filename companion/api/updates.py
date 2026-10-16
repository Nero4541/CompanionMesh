"""Admin API for the dashboard's Updates page (/v1/admin/updates).

The running version, whether a newer release exists (asked from GitHub, at
most every few minutes), the log of updates done with ``update-companion``
(``data/updates.jsonl``) and the changelog. Updating itself is done by the
update script while the server is stopped; the page says how.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from companion import __version__, updater
from companion.api.auth import require_admin

router = APIRouter(prefix="/v1/admin/updates", dependencies=[Depends(require_admin)])

CHANGELOG = updater.ROOT / "CHANGELOG.md"
CHECK_EVERY_S = 600.0


def _edge_version() -> str | None:
    from companion import edge_install

    try:
        return edge_install.edge_version()
    except edge_install.InstallError:
        return None


@router.get("")
async def updates(request: Request) -> dict[str, Any]:
    root: Path = updater.ROOT
    return {
        "version": __version__,
        "edge_version": _edge_version(),
        "method": "git" if (root / ".git").exists() else "release",
        "history": await asyncio.to_thread(updater.history, root),
        "changelog": CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.is_file() else "",
        "repo": updater.REPO,
    }


@router.get("/latest")
async def latest(request: Request, refresh: bool = False) -> dict[str, Any]:
    """The newest release on GitHub, compared with this version."""
    state = request.app.state
    cached = getattr(state, "latest_release", None)
    if cached and not refresh and time.time() - cached["checked"] < CHECK_EVERY_S:
        return cached
    try:
        release = await asyncio.to_thread(updater.latest_release)
    except updater.UpdateError as exc:
        return {"error": str(exc), "checked": time.time()}
    result = {
        "version": release.version,
        "tag": release.tag,
        "url": release.url,
        "published": release.published,
        "notes": release.notes,
        "newer": updater.is_newer(release.version, __version__),
        "checked": time.time(),
    }
    state.latest_release = result
    return result
