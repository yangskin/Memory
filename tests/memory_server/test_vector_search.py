"""Tests for the P5 Phase 2a vector pipeline:

* ``memory_vector_corpus.chunk_record`` chunking determinism + bounds
* ``memory_vector_search.build_vector_index`` end-to-end build
* ``memory_vector_search.vector_search`` ranking & failure modes

The deterministic-hash provider is the embedding under test so the suite
stays free of optional deps (onnxruntime, model files) — these arrive
in Phase 2b and will get gated tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_corpus import CompilableRecord
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_vector_corpus import (
    DEFAULT_CHUNK_CHARS,
    Chunk,
    chunk_record,
    chunk_records,
)
from servers.memory_server.memory_vector_search import (
    build_vector_index,
    vector_search,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_embeddings(repo: Path, **overrides) -> None:
    """Patch the on-disk config to opt the repo into the vector tier."""
    config_path = repo / ".ai-memory/config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    embeddings = {"enabled": True, "provider": "deterministic-hash"}
    embeddings.update(overrides)
    payload["embeddings"] = embeddings
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _make_record(path: str, title: str, body: str) -> CompilableRecord:
    return CompilableRecord(
        path=path,
        metadata={"id": path},
        body=f"# {title}\n\n{body}",
        title=title,
    )


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunk_record_is_deterministic() -> None:
    record = _make_record(
        "memory-bank/notes/a.md",
        "Material Pipeline",
        "Paragraph one about substance painter.\n\nParagraph two about UE5 import.",
    )
    a = chunk_record(record)
    b = chunk_record(record)
    assert a == b
    assert all(isinstance(c, Chunk) for c in a)
    assert {c.chunk_id for c in a} == {c.chunk_id for c in b}


def test_chunk_record_prepends_title_for_recall() -> None:
    record = _make_record(
        "memory-bank/notes/b.md",
        "Decision: lock font set",
        "We freeze on Source Han Sans for now.",
    )
    chunks = chunk_record(record)
    assert chunks
    # First chunk should carry the title so a query that hits the title
    # still ranks the body chunk highly.
    assert "Decision: lock font set" in chunks[0].text


def test_chunk_record_splits_long_paragraph_with_overlap() -> None:
    """One runaway paragraph must not become a single oversized chunk."""
    long_para = "x" * (DEFAULT_CHUNK_CHARS * 3 + 17)
    record = _make_record("memory-bank/notes/c.md", "Long Paste", long_para)
    chunks = chunk_record(record)
    # 3x size + epsilon → at least 3 chunks
    assert len(chunks) >= 3
    # Each chunk respects the character budget.
    for c in chunks:
        assert len(c.text) <= DEFAULT_CHUNK_CHARS + len("Long Paste\n\n")


def test_chunk_records_respects_max_cap() -> None:
    records = [
        _make_record(f"memory-bank/n/{i}.md", f"Note {i}", f"body {i} " * 100)
        for i in range(20)
    ]
    cap = 5
    out = list(chunk_records(records, max_chunks=cap))
    assert len(out) == cap


def test_chunk_record_empty_body_falls_back_to_title() -> None:
    record = _make_record("memory-bank/notes/empty.md", "Title Only", "")
    chunks = chunk_record(record)
    assert len(chunks) == 1
    assert "Title Only" in chunks[0].text


def test_chunk_id_changes_when_content_changes() -> None:
    a = _make_record("memory-bank/notes/v.md", "Versioned", "first version body text.")
    b = _make_record("memory-bank/notes/v.md", "Versioned", "second version body text.")
    ids_a = [c.chunk_id for c in chunk_record(a)]
    ids_b = [c.chunk_id for c in chunk_record(b)]
    assert ids_a != ids_b


# ---------------------------------------------------------------------------
# build_vector_index
# ---------------------------------------------------------------------------


def test_build_returns_disabled_when_embeddings_off(repo: Path) -> None:
    config = load_config(repo)
    result = build_vector_index(config)
    assert result["ok"] is False
    assert result["error"] == "embeddings_disabled"


def test_build_indexes_records_when_enabled(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)

    memory_write_record(
        config,
        content_markdown="# Material Decision\n\nFreeze on PBR metallic-roughness only.\n",
        record_kind="decision",
        scope="personal",
        status="validated",
        author="alice",
        tags=["material"],
    )
    memory_write_record(
        config,
        content_markdown="# Substance Painter Note\n\nExport via SP→UE bridge plugin.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["asset_pipeline"],
    )

    result = build_vector_index(config)
    assert result["ok"] is True, result
    assert result["chunks_indexed"] >= 2
    assert result["provider_id"] == "deterministic-hash"
    # Index dir must be under the configured root.
    assert config.embeddings_index_dir is not None
    assert str(config.embeddings_index_dir.as_posix()) in result["index_dir"]
    # Files exist.
    index_dir = Path(result["index_dir"])
    assert (index_dir / "meta.json").is_file()
    assert (index_dir / "vectors.bin").is_file()


def test_build_with_empty_corpus_succeeds(tmp_path: Path) -> None:
    # Bare repo: directory exists but no records yet.
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory/config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank"],
                "embeddings": {"enabled": True, "provider": "deterministic-hash"},
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    result = build_vector_index(config)
    assert result["ok"] is True
    assert result["chunks_indexed"] == 0


# ---------------------------------------------------------------------------
# vector_search
# ---------------------------------------------------------------------------


def test_vector_search_disabled_returns_disabled(repo: Path) -> None:
    config = load_config(repo)
    result = vector_search(config, "anything")
    assert result["ok"] is False
    assert result["error"] == "embeddings_disabled"


def test_vector_search_missing_index_returns_index_missing(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    result = vector_search(config, "material")
    assert result["ok"] is False
    assert result["error"] == "index_missing"


def test_vector_search_empty_query_rejected(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    build_vector_index(config)
    result = vector_search(config, "  ")
    assert result["ok"] is False
    assert result["error"] == "empty_query"


def test_vector_search_ranks_relevant_chunks_first(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)

    memory_write_record(
        config,
        content_markdown="# Material Pipeline\n\nUnreal material PBR roughness metallic workflow.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["material"],
    )
    memory_write_record(
        config,
        content_markdown="# Audio Bus\n\nSubmix routing through master and music buses.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
        tags=["mcp"],
    )

    build_result = build_vector_index(config)
    assert build_result["ok"] is True
    assert build_result["chunks_indexed"] >= 2

    result = vector_search(
        config, "unreal material pbr roughness", top_k=5, min_score=0.0
    )
    assert result["ok"] is True
    assert result["hits"], result
    top = result["hits"][0]
    assert "material" in top["source_path"].lower() or "material" in top["text_preview"].lower()


def test_vector_search_top_k_caps_results(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    for i in range(6):
        memory_write_record(
            config,
            content_markdown=f"# Note {i}\n\nBody for note number {i} about topic alpha bravo charlie.\n",
            record_kind="note",
            scope="personal",
            status="validated",
            author="alice",
            tags=["mcp"],
        )
    build_vector_index(config)
    result = vector_search(config, "alpha bravo charlie", top_k=3)
    assert result["ok"] is True
    assert len(result["hits"]) <= 3


def test_index_invalidated_by_dim_change(repo: Path) -> None:
    """Changing the embedding model_hash leaves the old index orphaned."""
    _enable_embeddings(repo)
    config = load_config(repo)
    memory_write_record(
        config,
        content_markdown="# A\n\nbody alpha.\n",
        record_kind="note",
        scope="personal",
        status="validated",
        author="alice",
    )
    build_vector_index(config)

    # Simulate a provider/model swap by handing in a custom-dim provider.
    from servers.memory_server.memory_embeddings import DeterministicHashProvider

    other_provider = DeterministicHashProvider(dim=128)
    result = vector_search(config, "alpha", provider=other_provider)
    assert result["ok"] is False
    assert result["error"] == "index_missing"
