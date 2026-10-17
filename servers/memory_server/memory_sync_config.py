"""Configuration adapter for optional Memory Hub synchronization."""

from __future__ import annotations

import os
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any


# 默认拒绝已泄露凭据；本机用户明确授权的精确哈希例外只从本机 canonical 配置读取。
COMPROMISED_TOKEN_SHA256 = frozenset({"8e05155739fdf6aad8ea228b47e443f536fa99f4a54a1e6987bd23c8bbb03e56"})


def token_is_compromised(value: str) -> bool:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest() in COMPROMISED_TOKEN_SHA256


@dataclass(frozen=True)
class SharedMemoryConfig:
    enabled: bool = False
    server_url: str = ""
    project_id: str = ""
    user_id: str = ""
    token_env: str = "MEMORY_HUB_TOKEN"
    local_token: str = ""
    legacy_token_allow_sha256: str = ""
    upload_enabled: bool = True
    upload_interval_seconds: int = 30
    upload_batch_size: int = 20
    upload_timeout_seconds: float = 5.0
    task_command_timeout_seconds: float = 2.0
    upload_retry_max_seconds: int = 300
    read_enabled: bool = True
    background_refresh_seconds: int = 60
    task_context_timeout_ms: int = 600
    active_query_timeout_ms: int = 1200
    fresh_cache_seconds: int = 90
    usable_cache_seconds: int = 600
    recent_window_hours: int = 24
    max_items: int = 20
    max_injected_tokens: int = 1000
    sync_scopes: tuple[str, ...] = ("personal", "session", "user_private", "shared", "project_shared", "org_shared")

    @property
    def token(self) -> str | None:
        value = os.getenv(self.token_env, "").strip()
        selected = value or self.local_token.strip()
        if not selected:
            return None
        if token_is_compromised(selected):
            # 用户明确要求恢复本机旧凭据；只放行授权的这个值，仍须服务端实际鉴权。
            fingerprint = hashlib.sha256(selected.encode("utf-8")).hexdigest()
            allowed = self.legacy_token_allow_sha256
            if len(allowed) != 64 or any(c not in "0123456789abcdef" for c in allowed) or not hmac.compare_digest(fingerprint, allowed):
                return None
        return selected

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.server_url) and bool(self.project_id) and bool(self.token)


def parse_shared_memory_config(raw: Any) -> SharedMemoryConfig:
    raw = raw if isinstance(raw, dict) else {}
    def integer(name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(raw.get(name, default)))
        except (TypeError, ValueError):
            return default
    def decimal(name: str, default: float, minimum: float) -> float:
        try:
            return max(minimum, float(raw.get(name, default)))
        except (TypeError, ValueError):
            return default
    scopes = raw.get("sync_scopes")
    if not isinstance(scopes, list):
        scopes = list(SharedMemoryConfig.sync_scopes)
    return SharedMemoryConfig(
        enabled=bool(raw.get("enabled", False)),
        server_url=str(raw.get("server_url") or "").rstrip("/"),
        project_id=str(raw.get("project_id") or ""),
        user_id=str(raw.get("user_id") or "").strip(),
        token_env=str(raw.get("token_env") or "MEMORY_HUB_TOKEN"),
        local_token=str(raw.get("token") or ""),
        legacy_token_allow_sha256=str(raw.get("legacy_token_allow_sha256") or ""),
        upload_enabled=bool(raw.get("upload_enabled", True)),
        upload_interval_seconds=integer("upload_interval_seconds", 30),
        upload_batch_size=min(20, integer("upload_batch_size", 20)),
        upload_timeout_seconds=decimal("upload_timeout_seconds", 5.0, 0.1),
        task_command_timeout_seconds=decimal("task_command_timeout_seconds", 2.0, 0.1),
        upload_retry_max_seconds=integer("upload_retry_max_seconds", 300),
        read_enabled=bool(raw.get("read_enabled", True)),
        background_refresh_seconds=integer("background_refresh_seconds", 60),
        task_context_timeout_ms=integer("task_context_timeout_ms", 600),
        active_query_timeout_ms=integer("active_query_timeout_ms", 1200),
        fresh_cache_seconds=integer("fresh_cache_seconds", 90),
        usable_cache_seconds=integer("usable_cache_seconds", 600),
        recent_window_hours=integer("recent_window_hours", 24),
        max_items=integer("max_items", 20),
        max_injected_tokens=min(1500, integer("max_injected_tokens", 1000)),
        sync_scopes=tuple(str(scope) for scope in scopes if str(scope).strip()),
    )