from __future__ import annotations

from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_maintenance import memory_health_check
from servers.memory_server.memory_record_io import iter_parsed_records
from servers.memory_server.memory_record_packing import compact_old_record_packs, pack_existing_records
from servers.memory_server.memory_records import memory_write_record


def _write_single_record(path: Path, *, record_id: str, author: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "schema_version: 1.0\n"
        f"id: {record_id}\n"
        "record_kind: note\n"
        "scope: personal\n"
        "status: raw\n"
        f"author: {author}\n"
        "created_at: 2026-05-12T00:00:00+00:00\n"
        "updated_at: 2026-05-12T00:00:00+00:00\n"
        "tags:\n"
        "  - mcp\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _with_record_packing(config, **overrides):
    values = {
        **config.__dict__,
        "record_packing_max_record_chars": 2000,
        "record_packing_max_pack_chars": 12000,
        "record_packing_archive_after_days": 0,
        "record_packing_archive_pack_max_chars": 1_048_576,
        **overrides,
    }
    return config.__class__(**values)


def test_pack_existing_records_migrates_small_single_files(repo: Path) -> None:
    config = load_config(repo)
    first_path = repo / "memory-bank" / "people" / "alice" / "mem_manual_a.md"
    second_path = repo / "memory-bank" / "people" / "alice" / "mem_manual_b.md"
    _write_single_record(
        first_path,
        record_id="mem_manual_a",
        author="alice",
        body="# Single A\n\nSmall historical record.",
    )
    _write_single_record(
        second_path,
        record_id="mem_manual_b",
        author="alice",
        body="# Single B\n\nAnother historical record.",
    )
    packed_config = _with_record_packing(config)

    result = pack_existing_records(packed_config, dry_run=False)

    assert result["ok"] is True
    assert result["moved"] == 2
    assert not first_path.exists()
    assert not second_path.exists()
    target_paths = {action["to"] for action in result["actions"]}
    assert len(target_paths) == 1
    assert "/packs/" in next(iter(target_paths))
    records, _stats = iter_parsed_records(packed_config)
    ids = {record.metadata["id"] for record in records}
    assert {"mem_manual_a", "mem_manual_b"} <= ids


def test_pack_existing_records_migrates_records_over_soft_size(repo: Path) -> None:
    config = load_config(repo)
    record_path = repo / "memory-bank" / "people" / "alice" / "mem_manual_large.md"
    _write_single_record(
        record_path,
        record_id="mem_manual_large",
        author="alice",
        body="# Larger Historical Record\n\n" + ("x" * 2600),
    )
    packed_config = _with_record_packing(config, record_packing_max_record_chars=100, record_packing_max_pack_chars=12000)

    result = pack_existing_records(packed_config, dry_run=False)

    assert result["ok"] is True
    assert result["moved"] == 1
    assert not record_path.exists()
    assert result["actions"][0]["id"] == "mem_manual_large"


def test_compact_old_record_packs_merges_into_capped_archive_packs(repo: Path) -> None:
    config = _with_record_packing(load_config(repo), record_packing_archive_pack_max_chars=1800)
    first = memory_write_record(
        config,
        content_markdown="# Packed A\n\n" + ("a" * 200) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    second = memory_write_record(
        config,
        content_markdown="# Packed B\n\n" + ("b" * 200) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    source_pack = repo / first["path"]

    result = compact_old_record_packs(config, older_than_days=0, dry_run=False)

    assert result["ok"] is True
    assert result["compacted"] == 1
    assert not source_pack.exists()
    archive_packs = list((repo / "memory-bank" / "archive" / "record-packs").glob("*.md"))
    assert archive_packs
    assert all(path.stat().st_size <= 1800 for path in archive_packs)
    records, _stats = iter_parsed_records(config)
    ids = {record.metadata["id"] for record in records}
    assert {first["id"], second["id"]} <= ids


def test_compact_old_record_packs_dry_run_simulates_archive_size(repo: Path) -> None:
    config = _with_record_packing(load_config(repo), record_packing_archive_pack_max_chars=1800)
    memory_write_record(
        config,
        content_markdown="# Packed A\n\n" + ("a" * 500) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )
    memory_write_record(
        config,
        content_markdown="# Packed B\n\n" + ("b" * 500) + "\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    result = compact_old_record_packs(config, older_than_days=0, dry_run=True)

    assert result["ok"] is True
    target_paths = {path for action in result["actions"] for path in action["to"]}
    assert len(target_paths) == 2
    assert all(not (repo / path).exists() for path in target_paths)


def test_health_check_warns_when_record_packing_quotas_exceeded(repo: Path) -> None:
    config = _with_record_packing(load_config(repo), record_packing_max_single_record_files=1)
    single_a = repo / "memory-bank" / "people" / "alice" / "mem_manual_a.md"
    _write_single_record(single_a, record_id="mem_manual_a", author="alice", body="# Manual A")
    single_b = repo / "memory-bank" / "people" / "bob" / "mem_manual_b.md"
    _write_single_record(single_b, record_id="mem_manual_b", author="bob", body="# Manual B")

    result = memory_health_check(config)

    assert result["ok"] is True
    assert result["status"] == "warn"
    assert any(issue["code"] == "raw_single_record_file_count_exceeded" for issue in result["issues"])


def test_record_packing_is_enabled_by_default_for_legacy_config(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True)
    (tmp_path / ".ai-memory" / "config.json").write_text(
        '{"allowed_roots":["memory-bank"],"events_file":".ai-memory/events.jsonl"}',
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    result = memory_write_record(
        config,
        content_markdown="# Default Packed\n\nLegacy config should pack by default.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["mcp"],
    )

    assert result["ok"] is True
    assert result["packed"] is True
    assert "/packs/" in result["path"]
