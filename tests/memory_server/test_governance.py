from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_compiler import memory_compile
from servers.memory_server.memory_governance import (
    memory_archive_record,
    memory_publish_candidate,
    memory_validate_candidate,
)
from servers.memory_server.memory_records import memory_write_record, parse_record_markdown
from servers.memory_server.memory_config import load_config
from servers.memory_server.server import _build_tools, _dispatch_tool


def _read_record(repo: Path, rel_path: str) -> tuple[dict, str]:
    return parse_record_markdown((repo / rel_path).read_text(encoding="utf-8"))


def test_validate_candidate_updates_metadata_and_moves_to_people(repo: Path) -> None:
    config = load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Candidate Rule\n\nValidate me before publishing.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp", "needs_validation"],
        task_id="task_governance",
    )

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    assert result["ok"] is True
    assert result["path"] == f"memory-bank/people/alice/{candidate['id']}.md"
    assert not (repo / candidate["path"]).exists()
    metadata, body = _read_record(repo, result["path"])
    assert metadata["status"] == "validated"
    assert metadata["validated_by"] == "lead"
    assert metadata["record_kind"] == "rule_candidate"
    assert body.startswith("# Candidate Rule")


def test_publish_candidate_creates_shared_system_rule(repo: Path) -> None:
    config = load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Shared Memory Rule\n\nPublished memory should enter shared layer.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp", "validation"],
        task_id="task_governance",
    )
    validated = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    result = memory_publish_candidate(config, candidate["id"], published_by="owner")

    assert result["ok"] is True
    assert result["path"] == f"memory-bank/shared/{candidate['id']}.md"
    assert not (repo / validated["path"]).exists()
    metadata, body = _read_record(repo, result["path"])
    assert metadata["status"] == "published"
    assert metadata["scope"] == "shared"
    assert metadata["record_kind"] == "system_rule"
    assert metadata["validated_by"] == "lead"
    assert metadata["published_by"] == "owner"
    assert "Published memory should enter shared layer." in body


def test_publish_candidate_rejects_unvalidated_candidate(repo: Path) -> None:
    config = load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Draft\n\nShould be validated first.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    result = memory_publish_candidate(config, candidate["id"], published_by="owner")

    assert result["ok"] is False
    assert result["error"] == "invalid_state"
    assert "validated" in result["message"]


def test_archive_record_moves_record_to_archive(repo: Path) -> None:
    config = load_config(repo)
    record = memory_write_record(
        config,
        content_markdown="# Old Note\n\nArchive this record.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["archive_candidate", "mcp"],
    )

    result = memory_archive_record(config, record["id"], reason="stale")

    assert result["ok"] is True
    assert result["path"] == f"memory-bank/archive/{record['id']}.md"
    assert not (repo / record["path"]).exists()
    metadata, body = _read_record(repo, result["path"])
    assert metadata["status"] == "archived"
    assert metadata["scope"] == "archive"
    assert metadata["archive_reason"] == "stale"
    assert body.startswith("# Old Note")


def test_compile_publish_queue_lists_candidate_records(repo: Path) -> None:
    config = load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Needs Review\n\nThis candidate should appear in publish queue.\n",
        record_kind="skill_candidate",
        scope="personal",
        author="alice",
        tags=["skill_possible", "needs_validation"],
        task_id="task_governance",
    )

    result = memory_compile(config, target="publish_queue", include_statuses=["candidate"])

    assert result["ok"] is True
    assert result["path"] == "memory-bank/compiled/publish/publish-queue.md"
    assert result["included_record_ids"] == [candidate["id"]]
    assert "# Publish Queue" in result["content"]
    assert "Needs Review" in result["content"]


def test_compile_system_digest_lists_published_shared_rules(repo: Path) -> None:
    config = load_config(repo)
    published = memory_write_record(
        config,
        content_markdown="# Published Rule\n\nSystem digest should include this.\n",
        record_kind="system_rule",
        scope="shared",
        author="lead",
        tags=["mcp"],
    )

    result = memory_compile(config, target="system_digest")

    assert result["ok"] is True
    assert result["path"] == "memory-bank/compiled/runtime/system-digest.md"
    assert result["included_record_ids"] == [published["id"]]
    assert "# System Digest" in result["content"]
    assert "System digest should include this." in result["content"]


def test_governance_tools_are_internal_cli_only(repo: Path) -> None:
    config = load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Dispatch Candidate\n\nGovernance is CLI/internal only.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    rejected = _dispatch_tool(
        config,
        "memory_validate_candidate",
        {"record_id": candidate["id"], "validated_by": "lead"},
    )
    validated = memory_validate_candidate(config, candidate["id"], validated_by="lead")
    published = memory_publish_candidate(config, candidate["id"], published_by="owner")
    archived = memory_archive_record(config, candidate["id"], reason="superseded")

    assert rejected["ok"] is False
    assert rejected["error"] == "unknown_tool"
    assert validated["ok"] is True
    assert published["ok"] is True
    assert archived["ok"] is True
    assert archived["path"] == f"memory-bank/archive/{candidate['id']}.md"


def test_build_tools_excludes_governance_tools_from_mcp(repo: Path) -> None:
    config = load_config(repo)
    tool_names = {tool.name for tool in _build_tools(config)}

    assert tool_names == {"memory_read", "memory_write"}
    assert "memory_validate_candidate" not in tool_names
    assert "memory_publish_candidate" not in tool_names
    assert "memory_archive_record" not in tool_names


def test_validate_candidate_does_not_leave_two_copies(repo: Path) -> None:
    """Atomic move: source path must be gone, target path must exist exactly once."""
    from servers.memory_server.memory_config import load_config as _load_config

    config = _load_config(repo)
    candidate = memory_write_record(
        config,
        content_markdown="# Atomic Move\n\nValidating must not leave duplicates.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    candidate_path = repo / candidate["path"]
    assert candidate_path.is_file()

    result = memory_validate_candidate(config, candidate["id"], validated_by="lead")

    assert result["ok"] is True
    new_path = repo / result["path"]
    assert new_path.is_file()
    assert not candidate_path.exists()
    # No leftover staging files in the target directory.
    siblings = [p.name for p in new_path.parent.iterdir()]
    assert all(not name.startswith(".") for name in siblings), siblings
