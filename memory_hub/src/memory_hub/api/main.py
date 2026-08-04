"""Memory Hub API process entry point."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from memory_hub.config import load_settings
from memory_hub.db.session import create_session_factory
from memory_hub.api.routes_context import router as context_router
from memory_hub.api.routes_events import router as events_router


def create_app() -> FastAPI:
    settings = load_settings()
    docs_url = None if settings.disable_docs else "/docs"
    openapi_url = None if settings.disable_docs else "/openapi.json"
    app = FastAPI(title="Memory Hub", docs_url=docs_url, redoc_url=None, openapi_url=openapi_url)
    if settings.database_url:
        app.state.session_factory = create_session_factory(settings.database_url)
        app.include_router(events_router)
        app.include_router(context_router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "database": "not_configured" if not settings.database_url else "pending"}

    return app


app = create_app()


def main() -> None:
    uvicorn.run("memory_hub.api.main:app", host="0.0.0.0", port=8000)