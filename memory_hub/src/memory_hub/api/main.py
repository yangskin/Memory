"""Memory Hub API process entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import text
from starlette.requests import ClientDisconnect

from memory_hub.config import load_settings
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.api.routes_enrollment import router as enrollment_router
from memory_hub.api.routes_board import router as board_router
from memory_hub.api.routes_device_status import router as device_status_router
from memory_hub.db.session import create_session_factory
from memory_hub.api.routes_context import router as context_router
from memory_hub.api.routes_events import router as events_router
from memory_hub.api.routes_tasks import router as tasks_router
from memory_hub.services.context_usage import ContextUsageBuffer

_LOGGER = logging.getLogger("memory_hub.api")

# Directory that contains the shared dashboard and its vendored browser assets.
# The package is installed via `pip install .` into site-packages, so __file__
# based lookup cannot locate the repo root; production sets MEMORY_HUB_WEB_DIR
# explicitly (Dockerfile COPYs web/ to /app/web). Local source runs fall back
# to the repository layout (src/memory_hub/api/main.py -> parents[3]).
_WEB_DIR = (
    Path(os.environ["MEMORY_HUB_WEB_DIR"]) / "web"
    if os.environ.get("MEMORY_HUB_WEB_DIR")
    else Path(__file__).resolve().parents[3] / "web"
)
_WEB_SHARED_PAGE = _WEB_DIR / "shared.html"
_CYTOSCAPE_SCRIPT = _WEB_DIR / "vendor" / "cytoscape.min.js"


def create_app() -> FastAPI:
    settings = load_settings()
    docs_url = None if settings.disable_docs else "/docs"
    openapi_url = None if settings.disable_docs else "/openapi.json"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        buffer = getattr(app.state, "context_usage", None)
        factory = getattr(app.state, "session_factory", None)
        if buffer is not None and factory is not None:
            try:
                with factory() as session:
                    buffer.flush(session)
            except Exception:
                _LOGGER.warning("context usage flush on shutdown failed", exc_info=True)

    app = FastAPI(title="Memory Hub", docs_url=docs_url, redoc_url=None, openapi_url=openapi_url, lifespan=lifespan)
    app.state.settings = settings
    app.state.rate_limiter = TokenRateLimiter()
    app.state.context_usage = ContextUsageBuffer()

    # 请求体上限由前置 Caddy 的 request_body max_size 统一执行（compose 中 api 不对外发布端口），
    # 这里不再预读整个请求体重复检查。客户端中途断开属于正常网络现象，不记录错误堆栈。
    @app.exception_handler(ClientDisconnect)
    async def client_disconnected(request, exc):
        _LOGGER.debug("client disconnected before the request body was received: %s", request.url.path)
        return Response(status_code=400)

    if settings.database_url:
        app.state.session_factory = create_session_factory(settings.database_url)
        app.include_router(events_router)
        app.include_router(context_router)
        app.include_router(board_router)
        app.include_router(device_status_router)
        app.include_router(tasks_router)
        app.include_router(enrollment_router)

    @app.get("/", include_in_schema=False)
    def dashboard_entry() -> RedirectResponse:
        return RedirectResponse("/shared")

    @app.get("/shared", include_in_schema=False)
    def shared_page() -> FileResponse:
        if not _WEB_SHARED_PAGE.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "shared page not found")
        return FileResponse(
            _WEB_SHARED_PAGE,
            media_type="text/html",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/assets/cytoscape.min.js", include_in_schema=False)
    def cytoscape_script() -> FileResponse:
        if not _CYTOSCAPE_SCRIPT.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "graph renderer not found")
        return FileResponse(_CYTOSCAPE_SCRIPT, media_type="text/javascript")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        if not settings.database_url:
            return {"status": "ok", "database": "not_configured"}
        try:
            with app.state.session_factory() as session:
                session.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable")
        return {"status": "ok", "database": "ready"}

    return app


app = create_app()


def main() -> None:
    uvicorn.run("memory_hub.api.main:app", host="0.0.0.0", port=8000)