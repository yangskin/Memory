"""Deep robustness tests for the memory MCP.

Targets gaps not yet covered by existing test files:

- Atomic write under concurrent overwrite (no half-written file / interleave).
- Stale ``.tmp`` siblings in target directories don't poison normal writes.
- ``iter_parsed_records`` survives malformed Front Matter and non-record files.
- ``find_record_by_id`` reports ``not_found`` cleanly when corpus is empty.
- ``memory_search_records`` recovers from a corrupted SQLite index via rebuild.
- Path traversal variants (absolute, NUL byte, ``..``-chains) are rejected.
- Unicode (CJK + emoji + ZWJ) survives a write/read roundtrip byte-for-byte.
- Global guard budget rejects oversized writes before touching disk.
- ``memory_write_record`` rejects malformed metadata without leaving artifacts.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_reader import memory_get
from servers.memory_server.memory_record_index import (
    memory_rebuild_index,
    memory_search_records,
)
from servers.memory_server.memory_record_io import (
    find_record_by_id,
    iter_parsed_records,
)
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_writer import memory_write


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_concurrent_overwrite_never_produces_interleaved_file(repo: Path) -> None:
    """N concurrent overwrites must yield a final file equal to ONE writer's
    full payload; no truncation, no interleaving, no zero-byte file.

    On Windows ``os.replace`` may legitimately raise ``WinError 5`` when two
    threads try to swap the same target at the same time. That's an honest
    failure (the writer gets ``write_failed``) and does NOT count as a
    robustness violation; what matters is that the file on disk is never
    corrupted and at least one writer succeeds.
    """
    config = load_config(repo)
    rel = "memory-bank/notes.md"

    payloads = [f"writer-{i}\n" + ("x" * 400) + f"\n# {i}\n" for i in range(8)]

    barrier = threading.Barrier(len(payloads))
    results: list[dict] = []

    def _write(payload: str) -> None:
        barrier.wait()
        results.append(
            memory_write(
                config,
                rel,
                payload,
                mode="overwrite",
                backup=False,
                inject_user_tag=False,
            )
        )

    threads = [threading.Thread(target=_write, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every result is either ok=True or a clean write_failed (no exception leaked).
    for r in results:
        if not r.get("ok"):
            assert r.get("error") == "write_failed", r
    assert any(r.get("ok") for r in results), "all concurrent writers failed"

    final = (repo / rel).read_text(encoding="utf-8")
    canonical = {p if p.endswith("\n") else p + "\n" for p in payloads}
    assert final in canonical, "concurrent overwrite produced corrupted file"


def test_stale_tmp_sibling_does_not_break_subsequent_write(repo: Path) -> None:
    """An orphan ``*.tmp`` left by a previous crash must not block writes."""
    config = load_config(repo)
    rel = "memory-bank/notes.md"
    target = repo / rel
    # Drop a stale temp file next to the target (simulating crash before rename).
    (target.parent / f"{target.name}.20200101000000-deadbeef.tmp").write_text(
        "stale", encoding="utf-8"
    )
    res = memory_write(
        config, rel, "fresh content\n", mode="overwrite", backup=False, inject_user_tag=False
    )
    assert res["ok"] is True
    assert "fresh content" in target.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Record I/O resilience
# ---------------------------------------------------------------------------


def test_iter_parsed_records_skips_malformed_frontmatter(repo: Path) -> None:
    """Broken YAML in a record file must be counted as skipped, not raised."""
    config = load_config(repo)
    # Records live under memory-bank/<sub>/*.md — must use a sub-dir to be
    # picked up by the iter glob (memory-bank/**/*.md).
    bad = repo / "memory-bank" / "personal" / "broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\nthis: is: not: valid: yaml:\n  - [unclosed\n---\nbody\n",
        encoding="utf-8",
    )
    records, stats = iter_parsed_records(config)
    assert stats["scanned_files"] >= 1
    # Either parsed as non-record (no id/kind) or as read error — either way, not raised.
    assert stats["skipped_non_records"] + stats["skipped_read_errors"] >= 1
    assert all(r.metadata.get("id") for r in records)


def test_iter_parsed_records_skips_non_record_markdown(repo: Path) -> None:
    """Plain markdown without Front Matter must be silently skipped."""
    config = load_config(repo)
    plain = repo / "memory-bank" / "personal" / "plain.md"
    plain.parent.mkdir(parents=True, exist_ok=True)
    plain.write_text("# Just notes\nhello\n", encoding="utf-8")
    records, stats = iter_parsed_records(config)
    assert stats["scanned_files"] >= 1
    assert stats["skipped_non_records"] >= 1
    assert all(r.metadata.get("id") and r.metadata.get("record_kind") for r in records)


def test_find_record_by_id_returns_not_found_when_corpus_empty(repo: Path) -> None:
    config = load_config(repo)
    res = find_record_by_id(config, "mem_does_not_exist")
    assert isinstance(res, dict)
    assert res.get("ok") is False
    assert res.get("error") == "not_found"


# ---------------------------------------------------------------------------
# Index recovery
# ---------------------------------------------------------------------------


def test_search_records_recovers_from_corrupted_index(repo: Path) -> None:
    """Corrupting search.db then rebuilding must restore search functionality."""
    config = load_config(repo)
    # Create a real record so the index has something to find.
    write = memory_write_record(
        config,
        content_markdown="# Robust\nrobustness-marker-token recoverable index entry\n",
        record_kind="note",
        scope="personal",
        tags=["workflow"],
    )
    assert write["ok"] is True

    rebuild = memory_rebuild_index(config)
    assert rebuild["ok"] is True

    db_path = repo / ".ai-memory" / "search.db"
    assert db_path.exists()
    db_path.write_bytes(b"\x00\x01\x02not a sqlite db\x00")

    # Rebuilding should drop and recreate the corrupted file rather than crash.
    rebuild2 = memory_rebuild_index(config)
    assert rebuild2["ok"] is True, rebuild2
    assert rebuild2.get("indexed_records", 0) >= 1, rebuild2

    hits = memory_search_records(config, "recoverable")
    assert hits["ok"] is True
    assert hits.get("results"), f"search returned no results after index rebuild: {hits}"


# ---------------------------------------------------------------------------
# Path security variants
# ---------------------------------------------------------------------------


def test_path_traversal_variants_rejected(repo: Path) -> None:
    config = load_config(repo)
    bad_paths = [
        "../outside.md",
        "memory-bank/../../escape.md",
        "memory-bank/sub/../../../escape.md",
        "C:/Windows/system32/evil.md",
        "/etc/passwd",
        "memory-bank/with\x00nul.md",
    ]
    for p in bad_paths:
        res = memory_write(config, p, "x\n", mode="overwrite", backup=False)
        assert res.get("ok") is False, f"path should be rejected: {p!r} -> {res}"
        assert res.get("error") in {"path_not_allowed", "invalid_input", "invalid_path", "not_found"}, res


# ---------------------------------------------------------------------------
# Unicode roundtrip
# ---------------------------------------------------------------------------


def test_unicode_roundtrip_preserves_bytes(repo: Path) -> None:
    config = load_config(repo)
    rel = "memory-bank/unicode.md"
    payload = "# 标题 🌟\n中文 + emoji 👨‍👩‍👧 + RTL ‏مرحبا‎\n"
    res = memory_write(
        config, rel, payload, mode="overwrite", backup=False, inject_user_tag=False
    )
    assert res["ok"] is True
    got = memory_get(config, rel)
    assert got["ok"] is True
    # Writer adds trailing newline if missing; payload already ends with \n.
    assert payload in got["content"]


# ---------------------------------------------------------------------------
# Guard budget
# ---------------------------------------------------------------------------


def test_global_budget_rejects_oversized_write(repo: Path) -> None:
    """total_max_chars=5000 in fixture; a 6 KB write must be rejected."""
    config = load_config(repo)
    huge = "y" * 6000
    res = memory_write(
        config,
        "memory-bank/huge.md",
        huge,
        mode="overwrite",
        backup=False,
        inject_user_tag=False,
    )
    assert res.get("ok") is False
    assert res.get("error") in {"budget_exceeded", "total_budget_exceeded", "guard_budget_exceeded"}, res


# ---------------------------------------------------------------------------
# Record write malformed input
# ---------------------------------------------------------------------------


def test_write_record_rejects_uncontrolled_record_kind_without_artifact(repo: Path) -> None:
    """Bad record_kind must be rejected and must NOT leave a stray file."""
    config = load_config(repo)
    before = {p.name for p in (repo / "memory-bank").rglob("*.md")}
    res = memory_write_record(
        config,
        content_markdown="# Bad\n",
        record_kind="not_a_real_kind",
        scope="personal",
    )
    assert res.get("ok") is False
    after = {p.name for p in (repo / "memory-bank").rglob("*.md")}
    assert after == before, f"rejected write must not create files: new={after - before}"
