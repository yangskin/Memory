"""End-to-end build / query orchestration for the local RAG tier (Phase 2a).

============================================================================
⚠️  EXPERIMENTAL — FROZEN  (DesignDoc §15.5 / §15.x slim-down decision)
----------------------------------------------------------------------------
The vector / RAG tier is frozen at v0.11.1.  Reasoning:

  * It is *opt-in* (`embeddings.enabled=False` by default); on default
    config every entry point here is a 0-byte no-op.
  * Activation thresholds (DesignDoc §15.5) are NOT met:
      - chunks  : ~1.5–3 万   (threshold ≥ 100k)
      - rebuild : sub-minute  (threshold ≥ 10 min)
      - QPS     : <1          (threshold ≥ 20)
  * Real-model recall baseline is still a pending observation item
    (DesignDoc §16); no production caller currently relies on it.

Do NOT extend this module with new features unless one of the §15.5
thresholds is hit OR an explicit user / design-doc decision reopens it.
Bug fixes that keep the existing contract intact are still welcome.
============================================================================

This module is the seam between the existing record corpus and the
vector index files written by :mod:`memory_vector_index`.  It deliberately
stays provider-agnostic — the only thing it needs from a provider is the
:class:`EmbeddingProvider` interface plus its
:class:`EmbeddingMetadata`, so swapping
:class:`DeterministicHashProvider` for a future ONNX provider is a one
line change at the call site.

Failure model: every error is converted into a structured result dict
``{"ok": False, "error": "...", "hint": "..."}`` so callers can keep the
main FTS path running even if the vector tier is broken (per §15.4.1
"可选 + 可降级").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_corpus import iter_compilable_records
from .memory_embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    cosine_similarity,
    get_provider,
)
from .memory_vector_corpus import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_CHUNK_OVERLAP,
    chunk_records,
)
from .memory_vector_index import (
    VectorEntry,
    VectorIndexError,
    read_index,
    write_index,
)


# ── Result types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VectorHit:
    """One vector-search match."""

    entry: VectorEntry
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {**self.entry.to_dict(), "score": float(self.score)}


# ── Provider resolution ─────────────────────────────────────────────────


def _resolve_provider(config: MemoryConfig) -> EmbeddingProvider:
    """Build the configured provider, falling back to ``auto`` on failure.

    The fall-through is intentional: an outdated/unsupported provider id
    in config must never block a basic vector build.  The retrieval layer
    can still decide to disable the tier outright via
    ``embeddings_enabled``.
    """

    try:
        return get_provider(
            config.embeddings_provider,
            model_path=config.embeddings_model_path,
        )
    except EmbeddingError:
        return get_provider(
            "auto", model_path=config.embeddings_model_path
        )


# ── Build ───────────────────────────────────────────────────────────────


def build_vector_index(
    config: MemoryConfig,
    *,
    provider: EmbeddingProvider | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> dict[str, Any]:
    """(Re)build the vector index for the entire corpus.

    Always a full rebuild in Phase 2a; incremental writes wait for Phase
    2b once the on-disk format has been exercised in real use.
    """

    if not config.embeddings_enabled:
        return {
            "ok": False,
            "error": "embeddings_disabled",
            "hint": "set embeddings.enabled=true in config to opt in",
        }

    embed_provider = provider or _resolve_provider(config)
    metadata = embed_provider.metadata
    index_root = config.embeddings_index_dir
    if index_root is None:
        return {
            "ok": False,
            "error": "no_index_dir",
            "hint": "embeddings.index_dir is unset",
        }

    records, _stats = iter_compilable_records(config)
    chunks = list(
        chunk_records(
            records,
            chunk_chars=chunk_chars,
            overlap_chars=chunk_overlap,
            max_chunks=config.embeddings_max_index_chunks,
        )
    )
    if not chunks:
        return {
            "ok": True,
            "provider_id": metadata.provider_id,
            "model_hash": metadata.model_hash,
            "dim": metadata.dim,
            "chunks_indexed": 0,
            "skipped_records": 0,
            "index_dir": str((index_root / metadata.index_dir_name()).as_posix()),
        }

    # Embed in bounded batches so a huge corpus cannot allocate one
    # gigantic Python list at peak.  ``max_batch`` is a soft ceiling; the
    # provider may further split internally.
    batch_size = max(1, int(config.embeddings_max_batch))
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), batch_size):
        batch_texts = [c.text for c in chunks[start : start + batch_size]]
        try:
            vectors.extend(embed_provider.embed(batch_texts))
        except EmbeddingError as exc:
            return {
                "ok": False,
                "error": "embed_failed",
                "hint": f"{type(exc).__name__}: {exc}",
                "provider_id": metadata.provider_id,
            }

    entries = [chunk.to_entry() for chunk in chunks]
    try:
        target_dir = write_index(
            root=index_root,
            metadata=metadata,
            entries=entries,
            vectors=vectors,
        )
    except VectorIndexError as exc:
        return {
            "ok": False,
            "error": "index_write_failed",
            "hint": str(exc),
            "provider_id": metadata.provider_id,
        }

    return {
        "ok": True,
        "provider_id": metadata.provider_id,
        "model_hash": metadata.model_hash,
        "dim": metadata.dim,
        "chunks_indexed": len(entries),
        "skipped_records": 0,
        "index_dir": str(target_dir.as_posix()),
    }


# ── Query ───────────────────────────────────────────────────────────────


def vector_search(
    config: MemoryConfig,
    query: str,
    *,
    top_k: int = 8,
    provider: EmbeddingProvider | None = None,
    min_score: float = 0.0,
) -> dict[str, Any]:
    """Top-k vector search over the on-disk index.

    Brute-force cosine over up to a few tens of thousands of float32
    vectors is well under 50 ms on a laptop CPU (see design doc
    §15.4.2 numbers); HNSW is intentionally deferred until corpus size
    crosses the threshold.
    """

    if not config.embeddings_enabled:
        return {
            "ok": False,
            "error": "embeddings_disabled",
            "hits": [],
        }
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "empty_query", "hits": []}

    embed_provider = provider or _resolve_provider(config)
    metadata = embed_provider.metadata
    index_root = config.embeddings_index_dir
    if index_root is None:
        return {"ok": False, "error": "no_index_dir", "hits": []}

    try:
        loaded = read_index(root=index_root, metadata=metadata)
    except VectorIndexError as exc:
        return {
            "ok": False,
            "error": "index_unreadable",
            "hint": str(exc),
            "hits": [],
        }
    if loaded is None:
        return {
            "ok": False,
            "error": "index_missing",
            "hint": "run build_vector_index first",
            "hits": [],
        }
    entries, vectors = loaded

    try:
        [query_vec] = embed_provider.embed([query])
    except EmbeddingError as exc:
        return {
            "ok": False,
            "error": "query_embed_failed",
            "hint": f"{type(exc).__name__}: {exc}",
            "hits": [],
        }

    scored: list[VectorHit] = []
    for entry, vec in zip(entries, vectors):
        score = cosine_similarity(query_vec, vec)
        if score < min_score:
            continue
        scored.append(VectorHit(entry=entry, score=score))

    scored.sort(key=lambda hit: hit.score, reverse=True)
    capped = scored[: max(0, int(top_k))]
    return {
        "ok": True,
        "provider_id": metadata.provider_id,
        "model_hash": metadata.model_hash,
        "query": query,
        "hits": [hit.to_dict() for hit in capped],
        "scanned": len(entries),
    }


__all__ = [
    "VectorHit",
    "build_vector_index",
    "vector_search",
]
