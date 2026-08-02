from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_governance import memory_publish_candidate, memory_validate_candidate
from servers.memory_server.memory_record_index import memory_rebuild_index, memory_search_records
from servers.memory_server.memory_records import memory_write_record, parse_record_markdown


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _base_config() -> dict:
    return {
        "allowed_roots": [".ai-context", "memory-bank"],
        "excluded_dirs": [],
        "events_file": ".ai-memory/events.jsonl",
        "backups_dir": ".ai-memory/backups",
        "temp_dir": ".ai-memory/temp",
        "governance": {
            "min_confidence": 0.7,
            "require_source_refs_for": ["rule_candidate", "skill_candidate"],
            "publish_owners": ["owner"],
            "reviewers": ["lead"],
        },
        "tag_schema": {
            "allowed_tags": ["mcp", "validation", "needs_validation", "skill_possible"],
            "version": "v-test",
        },
        "guard": {"targets": []},
    }


def test_validate_candidate_rejects_missing_source_refs_under_governance_policy(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    candidate = memory_write_record(
        config,
        content_markdown="# Governed Rule\n\nNeeds evidence.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
    )

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    assert result["ok"] is False
    assert result["error"] == "validation_failed"
    assert "source_refs" in result["validation_errors"][0]


def test_validate_candidate_rejects_low_confidence(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    candidate = memory_write_record(
        config,
        content_markdown="# Low Confidence\n\nNot enough confidence.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.2,
        source_refs=["evt_1"],
    )

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    assert result["ok"] is False
    assert result["error"] == "validation_failed"
    assert "confidence" in result["validation_errors"][0]


def test_validate_candidate_rejects_duplicate_candidate_title(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Duplicate Rule\n\nExisting evidence.\n",
        record_kind="rule_candidate",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_1"],
        validated_by="lead",
    )
    candidate = memory_write_record(
        config,
        content_markdown="# Duplicate Rule\n\nExisting evidence.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="bob",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_2"],
    )

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    assert result["ok"] is False
    assert result["error"] == "validation_failed"
    assert any("duplicate" in error for error in result["validation_errors"])


def test_publish_candidate_rejects_conflicting_system_rule(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Texture Rule\n\nAlways keep full resolution.\n",
        record_kind="system_rule",
        scope="shared",
        author="lead",
        tags=["mcp"],
    )
    candidate = memory_write_record(
        config,
        content_markdown="# Texture Rule\n\nAlways downscale previews.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_1"],
    )
    validated = memory_validate_candidate(config, candidate["id"], validated_by="lead")
    assert validated["ok"] is True

    result = memory_publish_candidate(config, candidate["id"], published_by="owner")

    assert result["ok"] is False
    assert result["error"] == "conflict_detected"
    assert "Texture Rule" in result["message"]


def test_publish_candidate_requires_configured_owner(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    candidate = memory_write_record(
        config,
        content_markdown="# Owner Rule\n\nOnly configured owner can publish.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_1"],
    )
    assert memory_validate_candidate(config, candidate["id"], validated_by="lead")["ok"] is True

    result = memory_publish_candidate(config, candidate["id"], published_by="not_owner")

    assert result["ok"] is False
    assert result["error"] == "permission_denied"


def test_validated_record_writes_tag_schema_version_from_config(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    candidate = memory_write_record(
        config,
        content_markdown="# Tag Schema\n\nUse configured tag schema version.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_1"],
    )

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    metadata, _body = parse_record_markdown((tmp_path / result["path"]).read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert metadata["tag_schema_version"] == "v-test"


def test_publish_candidate_updates_existing_search_index(tmp_path: Path) -> None:
    _write_json(tmp_path / ".ai-memory/config.json", _base_config())
    config = load_config(tmp_path)
    candidate = memory_write_record(
        config,
        content_markdown="# Indexed Publish\n\nPublished path should be searchable after governance move.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
        confidence=0.9,
        source_refs=["evt_1"],
    )
    memory_rebuild_index(config)
    assert memory_validate_candidate(config, candidate["id"], validated_by="lead")["ok"] is True

    published = memory_publish_candidate(config, candidate["id"], published_by="owner")
    search = memory_search_records(config, query="Indexed Publish")

    assert published["ok"] is True
    assert search["ok"] is True
    assert search["results"][0]["path"] == published["path"]
