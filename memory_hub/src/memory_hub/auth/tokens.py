"""Opaque Memory Hub token encoding and constant-time secret verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass


TOKEN_VERSION = "mem_v1"


@dataclass(frozen=True)
class ParsedToken:
    token_id: str
    secret: str


def secret_hash(secret: str) -> str:
    """Return the only secret representation permitted in storage."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_token() -> tuple[str, str, str]:
    """Generate a token ID, raw bearer token, and storage-safe secret hash."""
    token_id = f"tok_{secrets.token_urlsafe(18)}"
    secret = secrets.token_urlsafe(43)
    token = f"{TOKEN_VERSION}.{token_id}.{secret}"
    return token_id, token, secret_hash(secret)


def parse_token(token: str) -> ParsedToken | None:
    """Parse the public token envelope without logging or retaining it."""
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return None
    token_id, secret = parts[1], parts[2]
    if not token_id.startswith("tok_") or not secret:
        return None
    return ParsedToken(token_id=token_id, secret=secret)


def verify_secret(candidate: str, expected_hash: str) -> bool:
    """Compare the candidate only through its hash and in constant time."""
    return hmac.compare_digest(secret_hash(candidate), expected_hash)