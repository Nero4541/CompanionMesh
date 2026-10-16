from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from companion import __version__
from companion.api import admin, edge_admin, http, maintain, observe, realtime, updates
from companion.core.config import CompanionConfig, ConfigSource
from companion.core.runtime import CompanionRuntime, Providers

CLIENT_DIR = Path(__file__).resolve().parents[2] / "client"
WEB_CLIENT_DIR = CLIENT_DIR / "web"
# Built by `npm run build` in client/dashboard; committed, so no Node is needed.
DASHBOARD_DIR = CLIENT_DIR / "dashboard" / "dist"


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
    app.include_router(edge_admin.router)
    app.include_router(observe.router)
    app.include_router(maintain.router)
    app.include_router(updates.router)

    if config.server.dashboard and DASHBOARD_DIR.is_dir():
        app.mount("/admin", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")

    if config.server.dev_client and WEB_CLIENT_DIR.is_dir():
        app.mount("/dev", StaticFiles(directory=WEB_CLIENT_DIR, html=True), name="dev-client")

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse("/dev/")

    return app
