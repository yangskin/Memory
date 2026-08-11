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