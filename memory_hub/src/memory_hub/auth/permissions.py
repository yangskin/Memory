"""Token-derived request identity and scope checks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    token_id: str
    user_id: str
    project_id: str
    scopes: frozenset[str]


def allows(principal: Principal, scope: str) -> bool:
    return scope in principal.scopes