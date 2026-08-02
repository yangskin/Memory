"""Candidate → validated → published governance pipeline (legacy compat).

============================================================================
⚠️  DEPRECATED — ATTIC-ONLY  (DesignDoc §10 / §15.4 slim-down decision)
----------------------------------------------------------------------------
The `candidate → validated → published` link is *no longer* the default
write path; new code MUST go through the raw + distilled flow described
in DesignDoc §2.1.

  * MCP exposure : not registered; no config option can enable this pipeline
                   through the MCP tool surface.
  * CLI exposure : kept for **historical-data migration** + cross-team
                   consensus publishing (cli.py validate/publish/archive).
  * Test coverage: kept to prevent rot, NOT to encourage new callers.

Do NOT extend this module with new features.  Bug fixes that keep the
existing atomic-write contract intact are still welcome; anything that
adds new pipeline stages, new caller hooks, or new candidate sub-states
MUST be rejected and redirected to the raw + distilled path.
============================================================================
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_paths import PathSecurityError
from .memory_record_io import (
    find_record_by_id as _find_record,
    iter_parsed_records,
    refresh_index_if_exists as _refresh_index_if_exists,
    write_record_to_target as _write_record_to_target,
)
from .memory_result import error_result, ok_result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip().lower()
    return ""


def _display_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _body_fingerprint(body: str) -> str:
    return " ".join(body.lower().split())


def _other_records(config: MemoryConfig, record_id: str) -> list[tuple[str, dict[str, Any], str]]:
    parsed, _stats = iter_parsed_records(config)
    return [
        (record.rel_path, record.metadata, record.body)
        for record in parsed
        if str(record.metadata.get("id")) != record_id
    ]


def _validation_errors(config: MemoryConfig, record_id: str, metadata: dict[str, Any], body: str) -> list[str]:
    errors: list[str] = []
    record_kind = str(metadata.get("record_kind", ""))
    source_refs = metadata.get("source_refs") if isinstance(metadata.get("source_refs"), list) else []
    confidence = metadata.get("confidence")

    if record_kind in (config.governance_require_source_refs_for or []) and not source_refs:
        errors.append("source_refs are required for this candidate type")
    if isinstance(confidence, (int, float)) and confidence < config.governance_min_confidence:
        errors.append(f"confidence {confidence} is below minimum {config.governance_min_confidence}")

    title = _first_heading(body)
    fingerprint = _body_fingerprint(body)
    for _path, other_metadata, other_body in _other_records(config, record_id):
        if other_metadata.get("status") not in {"validated", "published"}:
            continue
        if _first_heading(other_body) == title and _body_fingerprint(other_body) == fingerprint:
            errors.append(f"duplicate record title/body: {title}")
            break
    return errors


def _publish_conflict(config: MemoryConfig, record_id: str, body: str) -> str | None:
    title = _first_heading(body)
    fingerprint = _body_fingerprint(body)
    for _path, other_metadata, other_body in _other_records(config, record_id):
        if other_metadata.get("status") != "published" or other_metadata.get("scope") != "shared":
            continue
        if _first_heading(other_body) == title and _body_fingerprint(other_body) != fingerprint:
            return _display_heading(other_body) or title
    return None


def memory_validate_candidate(
    config: MemoryConfig,
    record_id: str,
    *,
    validated_by: str | None = None,
) -> dict[str, Any]:
    found = _find_record(config, record_id)
    if isinstance(found, dict):
        return found
    old_abs_path, old_rel_path, metadata, body = found

    if str(metadata.get("status")) not in {"candidate", "raw"}:
        return error_result("invalid_state", "record must be candidate or raw before validation", record_id=record_id)
    if not str(metadata.get("record_kind", "")).endswith("_candidate"):
        return error_result("invalid_state", "record_kind must be a candidate type before validation", record_id=record_id)
    if config.governance_reviewers and validated_by not in config.governance_reviewers:
        return error_result("permission_denied", "validated_by must be one of configured reviewers", record_id=record_id)

    validation_errors = _validation_errors(config, record_id, metadata, body)
    if validation_errors:
        return error_result(
            "validation_failed",
            "candidate failed validation rules",
            record_id=record_id,
            validation_errors=validation_errors,
        )

    metadata["status"] = "validated"
    metadata["validated_by"] = validated_by or get_current_user(config.repo_root)
    metadata["updated_at"] = _now()
    if metadata.get("scope") in {None, "local"}:
        metadata["scope"] = "personal"

    result = _write_record_to_target(
        config,
        old_abs_path=old_abs_path,
        old_rel_path=old_rel_path,
        metadata=metadata,
        body=body,
    )
    if result.get("ok"):
        _refresh_index_if_exists(config, result["path"])
        append_event(
            config,
            "memory_validate_candidate",
            {
                "id": record_id,
                "previous_path": old_rel_path,
                "path": result["path"],
                "validated_by": metadata.get("validated_by"),
            },
        )
    return result


def memory_publish_candidate(
    config: MemoryConfig,
    record_id: str,
    *,
    published_by: str | None = None,
) -> dict[str, Any]:
    found = _find_record(config, record_id)
    if isinstance(found, dict):
        return found
    old_abs_path, old_rel_path, metadata, body = found

    if metadata.get("status") != "validated":
        return error_result("invalid_state", "record must be validated before publishing", record_id=record_id)
    if not metadata.get("validated_by"):
        return error_result("invalid_state", "record must have validated_by before publishing", record_id=record_id)
    if config.governance_publish_owners and published_by not in config.governance_publish_owners:
        return error_result("permission_denied", "published_by must be one of configured publish owners", record_id=record_id)
    conflict_title = _publish_conflict(config, record_id, body)
    if conflict_title:
        return error_result("conflict_detected", f"candidate conflicts with published system rule: {conflict_title}", record_id=record_id)

    metadata["status"] = "published"
    metadata["scope"] = "shared"
    if str(metadata.get("record_kind", "")).endswith("_candidate"):
        metadata["record_kind"] = "system_rule"
    metadata["published_by"] = published_by or get_current_user(config.repo_root)
    metadata["published_at"] = _now()
    metadata["updated_at"] = metadata["published_at"]

    result = _write_record_to_target(
        config,
        old_abs_path=old_abs_path,
        old_rel_path=old_rel_path,
        metadata=metadata,
        body=body,
    )
    if result.get("ok"):
        _refresh_index_if_exists(config, result["path"])
        append_event(
            config,
            "memory_publish_candidate",
            {
                "id": record_id,
                "previous_path": old_rel_path,
                "path": result["path"],
                "published_by": metadata.get("published_by"),
            },
        )
    return result


def memory_archive_record(
    config: MemoryConfig,
    record_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    found = _find_record(config, record_id)
    if isinstance(found, dict):
        return found
    old_abs_path, old_rel_path, metadata, body = found

    metadata["status"] = "archived"
    metadata["scope"] = "archive"
    metadata["archive_reason"] = reason
    metadata["archived_at"] = _now()
    metadata["updated_at"] = metadata["archived_at"]

    result = _write_record_to_target(
        config,
        old_abs_path=old_abs_path,
        old_rel_path=old_rel_path,
        metadata=metadata,
        body=body,
    )
    if result.get("ok"):
        _refresh_index_if_exists(config, result["path"])
        append_event(
            config,
            "memory_archive_record",
            {
                "id": record_id,
                "previous_path": old_rel_path,
                "path": result["path"],
                "reason": reason,
            },
        )
    return result
