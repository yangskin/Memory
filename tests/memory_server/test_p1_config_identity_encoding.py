from __future__ import annotations

import json
import os
from pathlib import Path

from servers.memory_server.memory_config import ReloadableMemoryConfig, load_config
from servers.memory_server.memory_encoding import audit_memory_encoding, repair_memory_encoding
from servers.memory_server.memory_identity import canonical_identity
from servers.memory_server.memory_record_index import memory_search_records
from servers.memory_server.memory_records import memory_write_record


def test_reloadable_config_adopts_valid_changes_and_keeps_last_good_on_corruption(repo: Path) -> None:
    initial = load_config(repo)
    provider = ReloadableMemoryConfig(initial)
    raw = json.loads(initial.config_path.read_text(encoding="utf-8"))
    raw["max_file_size_bytes"] = 2_000_003
    initial.config_path.write_text(json.dumps(raw), encoding="utf-8")

    refreshed = provider.get()
    assert refreshed.max_file_size_bytes == 2_000_003
    assert refreshed.config_hash != initial.config_hash
    assert provider.diagnostics()["reload_count"] == 1

    initial.config_path.write_text("{not-json", encoding="utf-8")
    retained = provider.get()
    assert retained.config_hash == refreshed.config_hash
    assert provider.diagnostics()["reload_error"]


def test_reloadable_config_rejects_semantic_worker_errors_and_clears_recovered_diagnostic(repo: Path) -> None:
    initial = load_config(repo)
    provider = ReloadableMemoryConfig(initial)
    known_good = initial.config_path.read_bytes()
    raw = json.loads(known_good.decode("utf-8"))
    raw.setdefault("worker", {})["poll_seconds"] = "fast"
    initial.config_path.write_text(json.dumps(raw), encoding="utf-8")

    retained = provider.get()
    assert retained.config_hash == initial.config_hash
    assert "worker.poll_seconds" in str(provider.diagnostics()["reload_error"])

    initial.config_path.write_bytes(known_good)
    assert provider.get().config_hash == initial.config_hash
    assert provider.diagnostics()["reload_error"] is None


def test_identity_is_unicode_normalized_casefolded_and_path_safe() -> None:
    assert canonical_identity("  Codex  ") == "codex"
    assert canonical_identity("ＧｉｔＨｕｂ   Copilot") == "github-copilot"
    assert canonical_identity("Alice\\Desktop/User") == "alice-desktop-user"


def test_record_write_rejects_lossy_or_invalid_unicode(repo: Path) -> None:
    config = load_config(repo)
    replacement = memory_write_record(config, content_markdown="# Bad\n\nvalue \ufffd", author="Codex")
    surrogate = memory_write_record(config, content_markdown="# Bad\n\nvalue \ud800", author="Codex")

    assert replacement["ok"] is False
    assert replacement["error"] == "invalid_input"
    assert surrogate["ok"] is False
    assert surrogate["error"] == "invalid_input"


def test_record_write_rejects_non_numeric_scores_without_raising(repo: Path) -> None:
    config = load_config(repo)
    result = memory_write_record(config, content_markdown="# Bad score\n", confidence="certain")
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_encoding_audit_and_explicit_gb18030_repair_preserve_exact_backup(repo: Path) -> None:
    target = repo / "memory-bank" / "legacy.md"
    original = "# 中文旧档\n\n编码修复。\n".encode("gb18030")
    target.write_bytes(original)
    config = load_config(repo)

    audit = audit_memory_encoding(config, paths=["memory-bank/legacy.md"])
    assert audit["healthy"] is False
    assert audit["files"][0]["issues"][0]["code"] == "invalid_utf8"

    dry_run = repair_memory_encoding(config, path="memory-bank/legacy.md", mode="gb18030")
    assert dry_run["ok"] is True
    assert dry_run["dry_run"] is True
    assert target.read_bytes() == original

    applied = repair_memory_encoding(
        config,
        path="memory-bank/legacy.md",
        mode="gb18030",
        apply=True,
        if_match=dry_run["before_sha"],
    )
    assert applied["ok"] is True
    assert target.read_text(encoding="utf-8").startswith("# 中文旧档")
    assert (repo / applied["backup_path"]).read_bytes() == original


def test_encoding_repair_cas_uses_exact_bytes_not_lossy_replacement_text(repo: Path) -> None:
    target = repo / "memory-bank" / "legacy.md"
    first = b"\x81\x40"
    second = b"\x82\x40"
    assert first.decode("utf-8", errors="replace") == second.decode("utf-8", errors="replace")
    target.write_bytes(first)
    config = load_config(repo)

    inspected = repair_memory_encoding(config, path="memory-bank/legacy.md", mode="gb18030")
    assert inspected["ok"] is True
    target.write_bytes(second)
    refused = repair_memory_encoding(
        config,
        path="memory-bank/legacy.md",
        mode="gb18030",
        apply=True,
        if_match=inspected["before_sha"],
    )
    assert refused["ok"] is False
    assert refused["error"] == "source_changed"
    assert target.read_bytes() == second


def test_search_rebuilds_when_record_source_changes_outside_index_writer(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Freshness\n\noriginal search phrase\n",
        record_kind="decision",
        scope="project_shared",
        status="published",
        author="Codex",
        task_id="task-index-freshness",
    )
    assert written["ok"] is True
    assert memory_search_records(config, "original search phrase")["results"]

    target = repo / written["path"]
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("original search phrase", "external freshness needle"), encoding="utf-8")

    refreshed = memory_search_records(config, "external freshness needle")
    assert refreshed["ok"] is True
    assert any(item["id"] == written["id"] for item in refreshed["results"])


def test_search_detects_equal_size_edit_with_preserved_mtime(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Exact freshness\n\ncontent needle alpha\n",
        record_kind="decision",
        scope="project_shared",
        status="published",
        author="Codex",
        task_id="task-exact-index-freshness",
    )
    assert memory_search_records(config, "needle alpha")["results"]
    target = repo / written["path"]
    before = target.stat()
    original = target.read_text(encoding="utf-8")
    changed = original.replace("needle alpha", "needle bravo")
    assert len(changed.encode("utf-8")) == len(original.encode("utf-8"))
    target.write_text(changed, encoding="utf-8")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    refreshed = memory_search_records(config, "needle bravo")
    assert refreshed["ok"] is True
    assert any(item["id"] == written["id"] for item in refreshed["results"])


def test_historical_author_alias_remains_visible_after_identity_canonicalization(repo: Path) -> None:
    config = load_config(repo)
    written = memory_write_record(
        config,
        content_markdown="# Historical identity\n\nprivate alias needle\n",
        record_kind="note",
        scope="personal",
        status="published",
        author="github-copilot",
        task_id="task-historical-identity",
    )
    assert written["ok"] is True

    target = repo / written["path"]
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("author: github-copilot", "author: GitHub Copilot"), encoding="utf-8")

    result = memory_search_records(config, "private alias needle", user="ＧｉｔＨｕｂ Copilot")
    assert result["ok"] is True
    assert any(item["id"] == written["id"] for item in result["results"])
