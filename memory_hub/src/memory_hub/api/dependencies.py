"""FastAPI dependencies for bearer authentication and scope checks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session, sessionmaker

from memory_hub.auth.permissions import Principal
from memory_hub.auth.tokens import parse_token, verify_secret
from memory_hub.db.models import TokenDevice
from memory_hub.db.repositories import active_token

# last_used_at 只用于判断凭据是否仍在使用，精度到分钟级即可；避免每个读请求都额外写库。
LAST_USED_RESOLUTION = timedelta(minutes=5)


def effective_user_id(request: Request, principal: Principal) -> str:
    """Return token identity unless explicit delegation is authorized."""
    user_id = request.headers.get("X-Memory-User-ID", "").strip()
    if not user_id or "identity:delegate" not in principal.scopes:
        return principal.user_id
    if len(user_id) > 256 or any(ord(character) < 32 for character in user_id):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid user id")
    return user_id


def require_principal(required_scope: str) -> Callable:
    def dependency(request: Request) -> Principal:
        factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
        header = request.headers.get("Authorization", "")
        raw = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        parsed = parse_token(raw)
        if factory is None or parsed is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid authentication")
        with factory() as session:
            token = active_token(session, parsed.token_id)
            if token is None or not verify_secret(parsed.secret, token.token_secret_hash):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid authentication")
            if required_scope not in token.scopes:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient scope")
            binding = session.get(TokenDevice, token.token_id)
            device_name = (binding.device_name if binding is not None and binding.project_id == token.project_id
                           and "identity:delegate" not in token.scopes else None)
            principal = Principal(token.token_id, token.user_id, token.project_id, frozenset(token.scopes), device_name)
            now = datetime.now(UTC)
            last_used = token.last_used_at
            if last_used is not None and last_used.tzinfo is None:
                last_used = last_used.replace(tzinfo=UTC)
            if last_used is None or now - last_used >= LAST_USED_RESOLUTION:
                token.last_used_at = now
                session.commit()
            return principal
    return dependency