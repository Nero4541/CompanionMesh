from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from companion import __version__
from companion.api import (
    admin,
    builtin,
    edge_admin,
    http,
    maintain,
    mcp,
    observe,
    people,
    realtime,
    timeline,
    updates,
    viewer,
)
from companion.core.config import CompanionConfig, ConfigSource
from companion.core.runtime import CompanionRuntime, Providers

CLIENT_DIR = Path(__file__).resolve().parents[2] / "client"
WEB_CLIENT_DIR = CLIENT_DIR / "web"
VIEWER_DIR = CLIENT_DIR / "viewer"  # the tailnet's read-only page (server.viewer)
# Built by `npm run build` in client/dashboard; committed, so no Node is needed.
DASHBOARD_DIR = CLIENT_DIR / "dashboard" / "dist"


class Revalidated(StaticFiles):
    """Static pages a browser checks again before each use (a 304 when unchanged).

    Without it, phones kept a page's scripts for hours by their own guess and ran an
    old script with a new page after an update.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def create_app(
    config: CompanionConfig,
    providers: Providers | None = None,
    *,
    source: ConfigSource | None = None,
    restart: Callable[[], None] | None = None,
) -> FastAPI:
    """``source`` lets the dashboard edit the config files; ``restart`` lets it
    restart the server (both optional: without them those features are off)."""
    runtime = CompanionRuntime(config, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await edge_admin.cancel_jobs(app)
            await runtime.stop()

    app = FastAPI(title="Companion Server", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.state.config_source = source
    app.state.request_restart = restart
    app.include_router(http.router)
    app.include_router(http.protected)
    app.include_router(realtime.router)
    app.include_router(admin.router)
    app.include_router(admin.events_router)
    app.include_router(builtin.router)
    app.include_router(people.router)
    app.include_router(timeline.router)
    app.include_router(mcp.router)
    app.include_router(edge_admin.router)
    app.include_router(observe.router)
    app.include_router(maintain.router)
    app.include_router(updates.router)
    app.include_router(viewer.router)

    if config.server.dashboard and DASHBOARD_DIR.is_dir():
        app.mount("/admin", Revalidated(directory=DASHBOARD_DIR, html=True), name="dashboard")

    # Always there (the page holds no data): server.viewer can be turned on while running.
    if VIEWER_DIR.is_dir():
        app.mount("/view", Revalidated(directory=VIEWER_DIR, html=True), name="viewer")

    if config.server.dev_client and WEB_CLIENT_DIR.is_dir():
        app.mount("/dev", Revalidated(directory=WEB_CLIENT_DIR, html=True), name="dev-client")

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse("/dev/")

    return app
