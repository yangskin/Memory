from __future__ import annotations

from starlette.requests import Request

from memory_hub.api.dependencies import effective_user_id
from memory_hub.auth.permissions import Principal


def _request(user_id: str) -> Request:
    return Request({"type": "http", "headers": [(b"x-memory-user-id", user_id.encode("utf-8"))]})


def test_effective_user_id_ignores_header_without_delegation_scope() -> None:
    principal = Principal("token", "alice", "project", frozenset({"events:write", "context:read"}))

    assert effective_user_id(_request("bob"), principal) == "alice"


def test_effective_user_id_accepts_header_with_delegation_scope() -> None:
    principal = Principal("token", "deployment", "project", frozenset({"identity:delegate"}))

    assert effective_user_id(_request("bob"), principal) == "bob"


def test_authentication_updates_last_used_at_at_most_every_few_minutes() -> None:
    from datetime import UTC, datetime, timedelta

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import JSON, create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from memory_hub.api.dependencies import LAST_USED_RESOLUTION, require_principal
    from memory_hub.auth.tokens import create_token
    from memory_hub.db.models import AccessToken, TokenDevice

    original = AccessToken.__table__.c.scopes.type
    AccessToken.__table__.c.scopes.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        AccessToken.__table__.create(engine)
        TokenDevice.__table__.create(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        token_id, raw, hashed = create_token()
        with factory() as session:
            session.add(AccessToken(token_id=token_id, token_secret_hash=hashed, token_prefix="t",
                                    user_id="alice", project_id="p", scopes=["context:read"]))
            session.commit()
        app = FastAPI()
        app.state.session_factory = factory

        @app.get("/probe")
        def probe(principal: Principal = Depends(require_principal("context:read"))) -> dict[str, str]:
            return {"user": principal.user_id}

        updates = []
        event.listen(engine, "before_cursor_execute",
                     lambda conn, cursor, statement, *args: updates.append(statement) if statement.startswith("UPDATE") else None)
        client = TestClient(app)
        headers = {"Authorization": "Bearer " + raw}
        for _ in range(20):
            assert client.get("/probe", headers=headers).json() == {"user": "alice"}
        # 20 次读请求只在第一次写入 last_used_at。
        assert len(updates) == 1
        with factory() as session:
            token = session.get(AccessToken, token_id)
            token.last_used_at = datetime.now(UTC) - LAST_USED_RESOLUTION - timedelta(seconds=1)
            session.commit()
        updates.clear()
        client.get("/probe", headers=headers)
        assert len(updates) == 1
    finally:
        engine.dispose()
        AccessToken.__table__.c.scopes.type = original