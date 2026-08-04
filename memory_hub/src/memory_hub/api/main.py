"""Memory Hub API process entry point."""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from memory_hub.config import load_settings
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.db.session import create_session_factory
from memory_hub.api.routes_context import router as context_router
from memory_hub.api.routes_events import router as events_router


def create_app() -> FastAPI:
    settings = load_settings()
    docs_url = None if settings.disable_docs else "/docs"
    openapi_url = None if settings.disable_docs else "/openapi.json"
    app = FastAPI(title="Memory Hub", docs_url=docs_url, redoc_url=None, openapi_url=openapi_url)
    app.state.settings = settings
    app.state.rate_limiter = TokenRateLimiter()

    @app.middleware("http")
    async def limit_request_body(request, call_next):
        if request.method == "POST":
            body = await request.body()
            if len(body) > 1_048_576:
                return JSONResponse(status_code=413, content={"detail": "payload too large"})
        return await call_next(request)
    if settings.database_url:
        app.state.session_factory = create_session_factory(settings.database_url)
        app.include_router(events_router)
        app.include_router(context_router)

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