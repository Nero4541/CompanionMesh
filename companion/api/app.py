from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from companion import __version__
from companion.api import http, realtime
from companion.core.config import CompanionConfig
from companion.core.runtime import CompanionRuntime, Providers

WEB_CLIENT_DIR = Path(__file__).resolve().parents[2] / "client" / "web"


def create_app(config: CompanionConfig, providers: Providers | None = None) -> FastAPI:
    runtime = CompanionRuntime(config, providers)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="Companion Server", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(http.router)
    app.include_router(realtime.router)

    if config.server.dev_client and WEB_CLIENT_DIR.is_dir():
        app.mount("/dev", StaticFiles(directory=WEB_CLIENT_DIR, html=True), name="dev-client")

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse("/dev/")

    return app
