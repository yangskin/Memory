"""On-disk vector index for the local RAG tier (P5 Phase 1).

============================================================================
⚠️  EXPERIMENTAL — FROZEN  (DesignDoc §15.5 / §15.x slim-down decision)
----------------------------------------------------------------------------
Index format / persistence layer of the frozen vector tier.  See
``memory_vector_search`` for the full freeze rationale.  Touching the
on-disk format requires bumping ``provider_id`` / ``model_hash`` so
existing indexes invalidate cleanly — please don't do it casually.
============================================================================

Storage layout (per design doc §15.4.4):

    .ai-memory/vector_index/<provider_id>__<model_hash>/
        meta.json     # provider/model identity + counts + timestamps
        ids.jsonl     # one entry per vector: {record_id, chunk_id, source_path, text_preview}
        vectors.bin   # raw float32 little-endian, count * dim values

Why float32 raw bytes?

* Pure stdlib: stays inside the "no new hard deps" rule for Phase 1.
* O(1) random access by ``offset = i * dim * 4``.
* Tiny: 30k chunks × 384 dims ≈ 46 MB; well within the §15.4.2 budget.

Why provider/model in the directory name?

* Any change in provider or model_hash deterministically invalidates the
  index.  The reader returns None and the caller (retrieval / rebuild)
  drops the directory and restarts a clean rebuild — never silently
  mixing vectors across embedding spaces.

Failure mode: any I/O or schema-mismatch error raises
:class:`VectorIndexError`; callers MUST treat this as "skip the vector
tier" rather than as a hard fault on the main FTS pipeline.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .memory_embeddings import EmbeddingMetadata


_FLOAT_SIZE = 4  # struct.calcsize("<f")
_META_FILENAME = "meta.json"
_IDS_FILENAME = "ids.jsonl"
_VECTORS_FILENAME = "vectors.bin"


class VectorIndexError(RuntimeError):
    """Raised when the on-disk index cannot be read or written safely."""


@dataclass(frozen=True)
class VectorEntry:
    """A single indexed chunk identified by record + chunk id."""

    record_id: str
    chunk_id: str
    source_path: str
    text_preview: str

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "text_preview": self.text_preview,
        }

    @staticmethod
    def from_dict(data: dict) -> "VectorEntry":
        try:
            return VectorEntry(
                record_id=str(data["record_id"]),
                chunk_id=str(data["chunk_id"]),
                source_path=str(data["source_path"]),
                text_preview=str(data.get("text_preview", "")),
            )
        except (KeyError, TypeError) as exc:
            raise VectorIndexError(f"malformed ids entry: {data!r}") from exc


def index_dir_for(root: Path, metadata: EmbeddingMetadata) -> Path:
    """Resolve the on-disk directory for a given provider+model identity.

    ``root`` is typically ``<repo>/.ai-memory/vector_index``.
    """

    return Path(root) / metadata.index_dir_name()


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write bytes via temp-file + os.replace, mirroring memory_record_io."""

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _pack_vectors(vectors: Sequence[Sequence[float]], dim: int) -> bytes:
    fmt = f"<{dim}f"
    out = bytearray()
    for vector in vectors:
        if len(vector) != dim:
            raise VectorIndexError(
                f"vector dim mismatch: expected {dim}, got {len(vector)}"
            )
        out.extend(struct.pack(fmt, *vector))
    return bytes(out)


def _unpack_vectors(blob: bytes, dim: int) -> list[list[float]]:
    if dim <= 0:
        raise VectorIndexError(f"invalid dim: {dim}")
    expected_stride = dim * _FLOAT_SIZE
    if len(blob) % expected_stride != 0:
        raise VectorIndexError(
            f"vectors.bin size {len(blob)} not aligned to dim={dim}"
        )
    fmt = f"<{dim}f"
    count = len(blob) // expected_stride
    return [list(struct.unpack_from(fmt, blob, i * expected_stride)) for i in range(count)]


def write_index(
    *,
    root: Path,
    metadata: EmbeddingMetadata,
    entries: Sequence[VectorEntry],
    vectors: Sequence[Sequence[float]],
) -> Path:
    """Atomically (re)write a full index for one provider+model pair.

    Phase 1 only supports full-rebuild semantics; incremental append lands
    in Phase 2 alongside the ONNX provider so corruption surface stays
    minimal while we settle the file format.
    """

    if len(entries) != len(vectors):
        raise VectorIndexError(
            f"entries/vectors length mismatch: {len(entries)} vs {len(vectors)}"
        )
    target_dir = index_dir_for(root, metadata)
    target_dir.mkdir(parents=True, exist_ok=True)

    meta_payload = {
        **metadata.to_dict(),
        "count": len(entries),
        "created_at": _iso_now(),
        "format_version": 1,
    }
    _atomic_write_bytes(
        target_dir / _META_FILENAME,
        (json.dumps(meta_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )

    ids_blob = "".join(
        json.dumps(entry.to_dict(), ensure_ascii=False) + "\n" for entry in entries
    ).encode("utf-8")
    _atomic_write_bytes(target_dir / _IDS_FILENAME, ids_blob)

    vector_blob = _pack_vectors(vectors, metadata.dim)
    _atomic_write_bytes(target_dir / _VECTORS_FILENAME, vector_blob)

    return target_dir


def read_index(
    *,
    root: Path,
    metadata: EmbeddingMetadata,
) -> tuple[list[VectorEntry], list[list[float]]] | None:
    """Read a previously-written index for the given provider+model.

    Returns ``None`` when the directory does not exist (cold start) so the
    caller can decide whether to rebuild.  Raises
    :class:`VectorIndexError` for any partial / corrupt state.
    """

    target_dir = index_dir_for(root, metadata)
    if not target_dir.exists():
        return None
    meta_path = target_dir / _META_FILENAME
    ids_path = target_dir / _IDS_FILENAME
    vec_path = target_dir / _VECTORS_FILENAME
    if not (meta_path.is_file() and ids_path.is_file() and vec_path.is_file()):
        raise VectorIndexError(f"incomplete vector index at {target_dir}")

    try:
        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VectorIndexError(f"meta.json is not valid JSON: {exc}") from exc

    # Hard schema check: the directory name encodes provider+model_hash,
    # but we still validate dim so a manually edited meta.json cannot
    # silently cause unpack misalignment.
    on_disk_dim = meta_data.get("dim")
    if on_disk_dim != metadata.dim:
        raise VectorIndexError(
            f"index dim mismatch: meta.json={on_disk_dim} provider={metadata.dim}"
        )
    if meta_data.get("provider_id") != metadata.provider_id:
        raise VectorIndexError("provider_id mismatch between dir name and meta.json")
    if meta_data.get("model_hash") != metadata.model_hash:
        raise VectorIndexError("model_hash mismatch between dir name and meta.json")

    entries = list(_iter_ids(ids_path))
    vectors = _unpack_vectors(vec_path.read_bytes(), metadata.dim)
    if len(entries) != len(vectors):
        raise VectorIndexError(
            f"ids/vectors length mismatch: {len(entries)} vs {len(vectors)}"
        )
    declared_count = meta_data.get("count")
    if isinstance(declared_count, int) and declared_count != len(entries):
        raise VectorIndexError(
            f"meta.count={declared_count} but ids has {len(entries)} rows"
        )
    return entries, vectors


def _iter_ids(path: Path) -> Iterator[VectorEntry]:
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VectorIndexError(
                    f"ids.jsonl line {line_no} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise VectorIndexError(
                    f"ids.jsonl line {line_no} is not a JSON object"
                )
            yield VectorEntry.from_dict(payload)


def _iso_now() -> str:
    # Lightweight ISO8601 in UTC; avoids importing datetime where we just
    # need a timestamp string for diagnostics.
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


__all__ = [
    "VectorIndexError",
    "VectorEntry",
    "index_dir_for",
    "write_index",
    "read_index",
]
