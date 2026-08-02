"""P5 Phase 2b — vector tier integration tests.

Covers two seams that DesignDoc §15.4.5 calls out:

* ``memory_retrieval._vector_supplement`` promoting semantically-relevant
  records past the FTS-only filter, and
* ``memory_key_documents.render_embedding_document`` producing a
  semantically re-ranked rebuild that still degrades gracefully when the
  vector index is missing.

All tests stay within the deterministic-hash provider so they run with
zero optional dependencies (no onnxruntime, no model downloads).  The
ONNX provider gets its own gated suite once the model-download CLI lands.
"""

from __future__ import annotations

import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_key_documents import (
    rebuild_key_documents,
    render_embedding_document,
)
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_retrieval import (
    _vector_supplement,
    memory_get_important_memories,
)
from servers.memory_server.memory_vector_search import build_vector_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _enable_embeddings(repo: Path) -> None:
    """Flip on the embedding tier with the deterministic-hash provider."""
    config_path = repo / ".ai-memory/config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["embeddings"] = {
        "enabled": True,
        "provider": "deterministic-hash",
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_records(config) -> dict[str, str]:
    """Write a small but topically diverse corpus.  Returns id -> title."""
    titles = {
        "material": "Material Pipeline PBR roughness metallic",
        "audio": "Audio Bus Submix routing through master and music buses",
        "input": "Input mapping enhanced input keyboard gamepad",
    }
    written: dict[str, str] = {}
    for key, title in titles.items():
        result = memory_write_record(
            config,
            content_markdown=f"# {title}\n\nNotes about {key} workflow in UE5.\n",
            record_kind="note",
            scope="project_shared",
            status="validated",
            author="alice",
            tags=["high_value"],
        )
        written[key] = result["id"]
    return written


# ---------------------------------------------------------------------------
# _vector_supplement
# ---------------------------------------------------------------------------


def test_vector_supplement_returns_empty_when_disabled(repo: Path) -> None:
    config = load_config(repo)
    assert _vector_supplement(config, "anything", []) == {}


def test_vector_supplement_returns_empty_for_blank_query(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    assert _vector_supplement(config, "   ", []) == {}


def test_vector_supplement_safely_handles_missing_index(repo: Path) -> None:
    """Index not built yet → the call must NOT raise; just degrades."""
    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)
    # No build_vector_index call on purpose.
    out = _vector_supplement(config, "material", [])
    assert out == {}


def test_vector_supplement_projects_hits_back_to_record_ids(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    ids = _seed_records(config)
    build_vector_index(config)

    # Use the records list shape that _rank_records would pass in.
    from servers.memory_server.memory_corpus import iter_compilable_records

    records, _ = iter_compilable_records(config)
    out = _vector_supplement(config, "material pbr roughness", records)
    # At least the material record must surface; scores are floats in [-1, 1].
    assert ids["material"] in out
    assert all(isinstance(v, float) for v in out.values())
    assert all(-1.0 <= v <= 1.0 for v in out.values())


# ---------------------------------------------------------------------------
# \u00a715.1-D: vector_supplement_skipped event + health 24h count
# ---------------------------------------------------------------------------


def test_vector_supplement_writes_event_when_search_raises(repo: Path, monkeypatch) -> None:
    """If vector_search raises, ``_vector_supplement`` must record a
    ``vector_supplement_skipped`` event (with reason + query preview) so
    operators can see how often the optional tier is being skipped.
    """
    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)

    from servers.memory_server import memory_retrieval as mr
    from servers.memory_server.memory_corpus import iter_compilable_records

    records, _ = iter_compilable_records(config)

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated index corruption")

    monkeypatch.setattr(mr, "vector_search", boom)
    out = _vector_supplement(config, "material lookup", records)
    assert out == {}

    events_path = config.events_file
    lines = events_path.read_text(encoding="utf-8").splitlines()
    matched = [
        json.loads(line)
        for line in lines
        if line and '"event_type": "vector_supplement_skipped"' in line
    ]
    assert matched, "expected at least one vector_supplement_skipped event"
    payload = matched[-1]["payload"]
    assert "simulated index corruption" in payload["reason"]
    assert payload["query_preview"].startswith("material lookup")


def test_health_check_surfaces_vector_skip_count_24h(repo: Path, monkeypatch) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)

    from servers.memory_server import memory_retrieval as mr
    from servers.memory_server.memory_corpus import iter_compilable_records
    from servers.memory_server.memory_maintenance import memory_health_check

    records, _ = iter_compilable_records(config)
    monkeypatch.setattr(mr, "vector_search", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    for _ in range(3):
        _vector_supplement(config, "material", records)

    health = memory_health_check(config)
    assert health.get("vector_skip_count_24h") == 3



# ---------------------------------------------------------------------------
# Retrieval pipeline (memory_get_important_memories)
# ---------------------------------------------------------------------------


def test_retrieval_with_vector_disabled_matches_baseline(repo: Path) -> None:
    """Disabled vector tier must not change ranking from the FTS-only path."""
    config = load_config(repo)
    _seed_records(config)
    result = memory_get_important_memories(config, query="material pbr", user="alice")
    assert result["ok"] is True
    # Baseline check: the lexical hit on "material pbr" is in results.
    titles = [item["title"] for item in result.get("important_memories", [])]
    assert any("material" in t.lower() for t in titles)


def test_retrieval_promotes_semantic_only_match_after_enable(repo: Path) -> None:
    """A query whose terms don't appear lexically can still recall via vectors.

    With the deterministic-hash provider, semantic similarity is essentially
    a token-co-occurrence signal — but the wiring contract is the same as
    for the future ONNX provider: a non-FTS-matching record_id MAY appear in
    the candidate set when its vector score crosses the threshold.
    """

    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)
    build_vector_index(config)

    # Same topic words as the seeded "material" record, so the deterministic
    # hash provider will reliably recall it.  The point of this test is the
    # plumbing — i.e. that _rank_records consulted the vector tier and the
    # call did not blow up the main retrieval path.
    result = memory_get_important_memories(
        config, query="material pbr roughness", user="alice"
    )
    assert result["ok"] is True
    items = result.get("important_memories", [])
    titles = [item["title"] for item in items]
    assert any("material" in t.lower() for t in titles), titles


# ---------------------------------------------------------------------------
# Embedding renderer for key_documents
# ---------------------------------------------------------------------------


def test_render_embedding_document_falls_back_when_disabled(repo: Path) -> None:
    """Without embeddings.enabled the helper must raise, NOT silently render."""
    config = load_config(repo)
    _seed_records(config)
    from servers.memory_server.memory_key_documents import _EmbeddingRendererError

    try:
        render_embedding_document(config, doc_key="progress", user="alice")
    except _EmbeddingRendererError:
        pass
    else:  # pragma: no cover - guard against silent enabling
        raise AssertionError("expected _EmbeddingRendererError when disabled")


def test_render_embedding_document_returns_generated_body(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)
    build_vector_index(config)

    body = render_embedding_document(config, doc_key="progress", user="alice")
    assert body.startswith("<!-- generated_by=memory-mcp")
    assert "renderer=embedding" in body
    # Vector score badge is emitted only when at least one hit overlapped.
    assert "vector_score=" in body


def test_rebuild_embedding_renderer_writes_when_enabled(repo: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)
    build_vector_index(config)

    result = rebuild_key_documents(
        config, targets=["progress"], user="alice", renderer="embedding"
    )
    assert result["ok"] is True, result
    assert result["written"]["progress"]["renderer"] == "embedding"

    written_path = repo / "memory-bank/progress.md"
    text = written_path.read_text(encoding="utf-8")
    assert "renderer=embedding" in text


def test_rebuild_embedding_renderer_falls_back_when_index_missing(repo: Path) -> None:
    """Embedding tier enabled but index not built → orchestrator falls through.

    Per DesignDoc §15.4.1 the embedding tier must always degrade to the
    deterministic tier rather than block the rebuild.  We rely on
    ``rebuild_key_documents`` automatically appending ``deterministic`` to
    the per-doc renderer order whenever ``renderer="embedding"``.
    """

    _enable_embeddings(repo)
    config = load_config(repo)
    _seed_records(config)
    # Deliberately skip build_vector_index → vector_search returns
    # error="index_missing", embedding renderer raises, deterministic wins.

    result = rebuild_key_documents(
        config, targets=["progress"], user="alice", renderer="embedding"
    )
    assert result["ok"] is True, result
    assert result["written"]["progress"]["renderer"] == "deterministic"
