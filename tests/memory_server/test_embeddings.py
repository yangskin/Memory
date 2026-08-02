"""Tests for the P5 Phase 1 RAG foundation:

* :mod:`servers.memory_server.memory_embeddings` provider abstraction
* :mod:`servers.memory_server.memory_vector_index` on-disk format
* :func:`servers.memory_server.memory_config.load_config` ``embeddings`` block

These tests deliberately avoid every optional dep (numpy, onnxruntime) so
they run inside the same lean environment as the rest of the suite.  The
deterministic-hash provider is the unit under test for vector math; ONNX
arrives in Phase 2 and will get its own gated test module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_embeddings import (
    DeterministicHashProvider,
    EmbeddingError,
    EmbeddingMetadata,
    ProviderUnavailableError,
    available_providers,
    cosine_similarity,
    get_provider,
)
from servers.memory_server.memory_vector_index import (
    VectorEntry,
    VectorIndexError,
    index_dir_for,
    read_index,
    write_index,
)


# ---------------------------------------------------------------------------
# Provider behaviour
# ---------------------------------------------------------------------------


def test_deterministic_provider_metadata_is_stable() -> None:
    a = DeterministicHashProvider()
    b = DeterministicHashProvider()
    assert a.metadata == b.metadata
    assert a.metadata.provider_id == "deterministic-hash"
    assert a.metadata.dim == DeterministicHashProvider.DEFAULT_DIM
    assert a.metadata.normalized is True
    # model_hash must be stable across runs so on-disk index dir name is deterministic.
    assert a.metadata.model_hash == b.metadata.model_hash


def test_deterministic_provider_dim_changes_model_hash() -> None:
    """Different dim => different model_hash => different on-disk dir."""
    a = DeterministicHashProvider(dim=64)
    b = DeterministicHashProvider(dim=128)
    assert a.metadata.model_hash != b.metadata.model_hash
    assert a.metadata.index_dir_name() != b.metadata.index_dir_name()


def test_deterministic_provider_embed_shape_and_determinism() -> None:
    provider = DeterministicHashProvider(dim=64)
    out1 = provider.embed(["unreal engine 材质", "cpu only embedding"])
    out2 = provider.embed(["unreal engine 材质", "cpu only embedding"])
    assert len(out1) == 2
    assert all(len(v) == 64 for v in out1)
    assert out1 == out2  # determinism


def test_deterministic_provider_normalises_l2() -> None:
    provider = DeterministicHashProvider()
    [vec] = provider.embed(["material pipeline note"])
    norm_sq = sum(v * v for v in vec)
    assert norm_sq == pytest.approx(1.0, abs=1e-6)


def test_deterministic_provider_empty_text_returns_zero_vector() -> None:
    provider = DeterministicHashProvider()
    [vec] = provider.embed([""])
    assert vec == [0.0] * provider.metadata.dim


def test_deterministic_provider_similar_texts_score_higher() -> None:
    """Lexical overlap should beat unrelated text."""
    provider = DeterministicHashProvider(dim=128)
    [base] = provider.embed(["unreal material pipeline"])
    [near] = provider.embed(["unreal material pipeline notes"])
    [far] = provider.embed(["substance painter export workflow"])
    near_sim = cosine_similarity(base, near)
    far_sim = cosine_similarity(base, far)
    assert near_sim > far_sim


def test_embed_rejects_non_string() -> None:
    provider = DeterministicHashProvider()
    with pytest.raises(EmbeddingError):
        provider.embed([123])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_available_providers_includes_deterministic() -> None:
    assert "deterministic-hash" in available_providers()


def test_get_provider_auto_returns_deterministic_in_phase1() -> None:
    provider = get_provider("auto")
    assert isinstance(provider, DeterministicHashProvider)


def test_get_provider_unknown_raises() -> None:
    with pytest.raises(ProviderUnavailableError):
        get_provider("nonexistent-provider")


# ---------------------------------------------------------------------------
# Cosine helper
# ---------------------------------------------------------------------------


def test_cosine_similarity_dim_mismatch_raises() -> None:
    with pytest.raises(EmbeddingError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_cosine_similarity_zero_vector_returns_zero() -> None:
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# Vector index — round-trip + invalidation guards
# ---------------------------------------------------------------------------


def _sample_entries_and_vectors(provider: DeterministicHashProvider, texts: list[str]):
    entries = [
        VectorEntry(
            record_id=f"rec-{i}",
            chunk_id=f"chunk-{i}",
            source_path=f"memory-bank/notes/{i}.md",
            text_preview=text[:32],
        )
        for i, text in enumerate(texts)
    ]
    vectors = provider.embed(texts)
    return entries, vectors


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    provider = DeterministicHashProvider()
    entries, vectors = _sample_entries_and_vectors(
        provider, ["alpha", "bravo charlie", "delta echo foxtrot"]
    )

    target = write_index(
        root=tmp_path, metadata=provider.metadata, entries=entries, vectors=vectors
    )
    assert target == index_dir_for(tmp_path, provider.metadata)
    assert (target / "meta.json").is_file()
    assert (target / "ids.jsonl").is_file()
    assert (target / "vectors.bin").is_file()

    loaded = read_index(root=tmp_path, metadata=provider.metadata)
    assert loaded is not None
    loaded_entries, loaded_vectors = loaded
    assert loaded_entries == entries
    # float32 round-trip: identical to ~1e-7
    for original, restored in zip(vectors, loaded_vectors):
        assert len(original) == len(restored)
        for a, b in zip(original, restored):
            assert a == pytest.approx(b, abs=1e-6)


def test_read_index_missing_returns_none(tmp_path: Path) -> None:
    provider = DeterministicHashProvider()
    assert read_index(root=tmp_path, metadata=provider.metadata) is None


def test_write_rejects_length_mismatch(tmp_path: Path) -> None:
    provider = DeterministicHashProvider()
    entries, vectors = _sample_entries_and_vectors(provider, ["a", "b"])
    with pytest.raises(VectorIndexError):
        write_index(
            root=tmp_path,
            metadata=provider.metadata,
            entries=entries,
            vectors=vectors[:1],
        )


def test_write_rejects_dim_mismatch(tmp_path: Path) -> None:
    provider = DeterministicHashProvider(dim=64)
    entries, _ = _sample_entries_and_vectors(provider, ["a"])
    bad_vectors = [[0.0] * 32]  # wrong dim
    with pytest.raises(VectorIndexError):
        write_index(
            root=tmp_path,
            metadata=provider.metadata,
            entries=entries,
            vectors=bad_vectors,
        )


def test_provider_change_invalidates_index_dir(tmp_path: Path) -> None:
    """Different provider/model => different dir => no cross-space contamination."""
    p1 = DeterministicHashProvider(dim=64)
    p2 = DeterministicHashProvider(dim=128)
    entries1, vectors1 = _sample_entries_and_vectors(p1, ["alpha"])
    write_index(root=tmp_path, metadata=p1.metadata, entries=entries1, vectors=vectors1)
    # Reading with a different model_hash must not see the old data.
    assert read_index(root=tmp_path, metadata=p2.metadata) is None


def test_corrupted_meta_json_raises(tmp_path: Path) -> None:
    provider = DeterministicHashProvider()
    entries, vectors = _sample_entries_and_vectors(provider, ["alpha"])
    write_index(
        root=tmp_path, metadata=provider.metadata, entries=entries, vectors=vectors
    )
    meta_path = index_dir_for(tmp_path, provider.metadata) / "meta.json"
    meta_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(VectorIndexError):
        read_index(root=tmp_path, metadata=provider.metadata)


def test_dim_mismatch_in_meta_raises(tmp_path: Path) -> None:
    provider = DeterministicHashProvider(dim=64)
    entries, vectors = _sample_entries_and_vectors(provider, ["alpha"])
    write_index(
        root=tmp_path, metadata=provider.metadata, entries=entries, vectors=vectors
    )
    meta_path = index_dir_for(tmp_path, provider.metadata) / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    payload["dim"] = 128  # tamper
    meta_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(VectorIndexError):
        read_index(root=tmp_path, metadata=provider.metadata)


def test_incomplete_index_dir_raises(tmp_path: Path) -> None:
    provider = DeterministicHashProvider()
    entries, vectors = _sample_entries_and_vectors(provider, ["alpha"])
    write_index(
        root=tmp_path, metadata=provider.metadata, entries=entries, vectors=vectors
    )
    (index_dir_for(tmp_path, provider.metadata) / "vectors.bin").unlink()
    with pytest.raises(VectorIndexError):
        read_index(root=tmp_path, metadata=provider.metadata)


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


def test_default_config_keeps_embeddings_off(tmp_path: Path) -> None:
    """Phase 1 hard rule: vector tier OFF until the user opts in."""
    cfg = load_config(tmp_path)
    assert cfg.embeddings_enabled is False
    assert cfg.embeddings_provider == "auto"
    assert cfg.embeddings_max_batch == 32
    assert cfg.embeddings_index_dir is not None
    assert cfg.embeddings_index_dir.is_absolute()
    assert cfg.embeddings_index_dir.name == "vector_index"


def test_user_can_enable_embeddings_via_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".ai-memory/config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "embeddings": {
                    "enabled": True,
                    "provider": "deterministic-hash",
                    "max_batch": 16,
                    "max_index_chunks": 50_000,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path, config_path)
    assert cfg.embeddings_enabled is True
    assert cfg.embeddings_provider == "deterministic-hash"
    assert cfg.embeddings_max_batch == 16
    assert cfg.embeddings_max_index_chunks == 50_000


def test_invalid_provider_falls_back_to_auto(tmp_path: Path) -> None:
    config_path = tmp_path / ".ai-memory/config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"embeddings": {"provider": "remote-openai-api"}}),
        encoding="utf-8",
    )
    cfg = load_config(tmp_path, config_path)
    # Remote/unknown providers must NOT be silently honoured: fall back to auto.
    assert cfg.embeddings_provider == "auto"
