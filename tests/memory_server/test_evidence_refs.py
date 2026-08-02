"""P1-E: verify important_memories evidence_refs covers source_refs,
related_artifact_ids, record path and id (not only source_refs)."""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_governance import memory_publish_candidate, memory_validate_candidate
from servers.memory_server.memory_retrieval import memory_get_important_memories


def _make_config(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "memory-bank").mkdir()
    (workspace / ".ai-context").mkdir()
    (workspace / ".ai-memory").mkdir()
    cfg = workspace / ".ai-memory" / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    return load_config(str(workspace), str(cfg))


def test_evidence_refs_includes_artifact_ids_path_and_id(tmp_path):
    config = _make_config(tmp_path)
    res = memory_write_record(
        config,
        content_markdown="# Evidence Test\n\n" + ("This is a substantial body paragraph used to satisfy the important-memory minimum body budget so the item lands in the digest. " * 4),
        record_kind="claim_candidate",
        scope="shared",
        status="candidate",
        tags=["mcp"],
        source_refs=["docs/intro.md", "https://example.com/spec"],
        related_artifact_ids=["asset-001", "blueprint-002"],
        importance_score=0.9,
    )
    assert res["ok"], res
    rid = res["id"]
    v = memory_validate_candidate(config, rid)
    assert v["ok"], v
    p = memory_publish_candidate(config, rid)
    assert p["ok"], p

    out = memory_get_important_memories(config)
    assert out["ok"], out
    refs = set(out["evidence_refs"])

    # source_refs preserved
    assert "docs/intro.md" in refs
    assert "https://example.com/spec" in refs
    # related_artifact_ids included
    assert "asset-001" in refs
    assert "blueprint-002" in refs
    # record id and path included
    assert rid in refs
    assert any(r.endswith(".md") and "memory-bank" in r for r in refs)
    # Nothing empty.
    assert all(r.strip() for r in refs)
