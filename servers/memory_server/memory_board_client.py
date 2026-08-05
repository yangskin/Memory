from __future__ import annotations

from typing import Any

from .memory_config import MemoryConfig
from .memory_result import error_result, ok_result
from .memory_sync_client import MemoryHubClient


def _shared_config(config: MemoryConfig):
    return getattr(config, "shared_memory", None)


def _is_active(config: MemoryConfig) -> bool:
    shared = _shared_config(config)
    return bool(shared and getattr(shared, "active", False))


def _timeout_read_seconds(config: MemoryConfig) -> float:
    shared = _shared_config(config)
    return float(getattr(shared, "active_query_timeout_ms", 1200)) / 1000.0


def _timeout_write_seconds(config: MemoryConfig) -> float:
    shared = _shared_config(config)
    return float(getattr(shared, "upload_timeout_seconds", 5.0))


def _post(config: MemoryConfig, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    shared = _shared_config(config)
    if not _is_active(config):
        return error_result("shared_memory_disabled", "shared memory is not active")
    status, body = MemoryHubClient(shared).post(path, payload, timeout)
    if status == 200:
        return ok_result("remote board call succeeded", remote=body, http_status=status)
    if status == 0:
        return error_result("remote_unavailable", str(body.get("error") or "remote_unavailable"), http_status=0)
    return error_result("remote_http_error", str(body.get("error") or f"http_{status}"), http_status=status)


def remote_board_query(config: MemoryConfig, payload: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_config(config)
    if not bool(getattr(shared, "read_enabled", True)):
        return error_result("shared_memory_read_disabled", "shared memory read is disabled")
    project_id = str(getattr(shared, "project_id", "") or "")
    return _post(
        config,
        f"/v1/projects/{project_id}/board/query",
        payload,
        _timeout_read_seconds(config),
    )


def remote_board_post(config: MemoryConfig, payload: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_config(config)
    if not bool(getattr(shared, "upload_enabled", True)):
        return error_result("shared_memory_upload_disabled", "shared memory upload is disabled")
    project_id = str(getattr(shared, "project_id", "") or "")
    return _post(
        config,
        f"/v1/projects/{project_id}/board/post",
        payload,
        _timeout_write_seconds(config),
    )


def remote_board_reply(config: MemoryConfig, payload: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_config(config)
    if not bool(getattr(shared, "upload_enabled", True)):
        return error_result("shared_memory_upload_disabled", "shared memory upload is disabled")
    project_id = str(getattr(shared, "project_id", "") or "")
    return _post(
        config,
        f"/v1/projects/{project_id}/board/reply",
        payload,
        _timeout_write_seconds(config),
    )


def remote_board_resolve(config: MemoryConfig, payload: dict[str, Any]) -> dict[str, Any]:
    shared = _shared_config(config)
    if not bool(getattr(shared, "upload_enabled", True)):
        return error_result("shared_memory_upload_disabled", "shared memory upload is disabled")
    project_id = str(getattr(shared, "project_id", "") or "")
    return _post(
        config,
        f"/v1/projects/{project_id}/board/resolve",
        payload,
        _timeout_write_seconds(config),
    )
