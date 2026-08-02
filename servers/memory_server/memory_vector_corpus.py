"""Chunking helpers for the local RAG / vector tier (P5 Phase 2a).

============================================================================
⚠️  EXPERIMENTAL — FROZEN  (DesignDoc §15.5 / §15.x slim-down decision)
----------------------------------------------------------------------------
Chunking layer of the frozen vector tier.  See ``memory_vector_search``
for the full freeze rationale.  Chunking constants here directly affect
the stored ``chunk_id`` values; changing them invalidates every index.
============================================================================

We feed embeddings one *chunk* at a time, not one whole record at a time:

* Records range from a few hundred to a few thousand characters; embedding
  a 5KB body as a single vector loses local detail.
* Smaller chunks also mean a query can pinpoint *which paragraph* of a
  long note matched, which is what callers (retrieval supplement,
  embedding renderer) actually need.

Design constraints (per MemorySystemDesignDocument.md §15.4):

* Pure stdlib — no tokenizer downloads, no regex blow-ups on CJK.
* Deterministic: same record body always produces the same chunks (and
  therefore stable ``chunk_id`` values), so the on-disk index stays
  diff-friendly across rebuilds.
* Bounded: a single oversized record cannot blow up the chunk count past
  ``max_index_chunks`` from config.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

from .memory_corpus import CompilableRecord, body_without_title
from .memory_vector_index import VectorEntry


# Default chunk sizing.  These numbers target the §15.4.2 budget
# (≤ 100k chunks for the whole repo) for current corpus sizes.
DEFAULT_CHUNK_CHARS = 400
DEFAULT_CHUNK_OVERLAP = 50
MIN_CHUNK_CHARS = 32  # below this, vectorising is mostly noise


@dataclass(frozen=True)
class Chunk:
    """One piece of a record, ready to be embedded + indexed."""

    record_id: str
    chunk_id: str
    source_path: str
    text: str

    def to_entry(self, *, preview_chars: int = 80) -> VectorEntry:
        preview = self.text.strip().replace("\n", " ")
        if len(preview) > preview_chars:
            preview = preview[: preview_chars - 1] + "…"
        return VectorEntry(
            record_id=self.record_id,
            chunk_id=self.chunk_id,
            source_path=self.source_path,
            text_preview=preview,
        )


_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _record_id_for(record: CompilableRecord) -> str:
    """Stable id for a record.

    Prefer an explicit ``id`` from front matter (records have one when
    they came through ``memory_writer``); fall back to the relative path
    so the function is total even on hand-edited files.
    """

    metadata = record.metadata or {}
    rid = metadata.get("id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    return record.path


def _chunk_id_for(record_id: str, index: int, text: str) -> str:
    """Deterministic chunk id: ``<seq>-<short text hash>``.

    Including the text hash means a record edit that shifts paragraph
    order produces detectably different ids, which is what lets index
    rebuilds stay incremental in Phase 2b without false-cache-hits.
    """

    digest = hashlib.blake2b(
        f"{record_id}|{index}|{text}".encode("utf-8"), digest_size=6
    ).hexdigest()
    return f"{index:04d}-{digest}"


def _split_into_paragraphs(text: str) -> list[str]:
    """Paragraph-first split; empty paragraphs are dropped."""

    return [p.strip() for p in _PARAGRAPH_BREAK.split(text) if p.strip()]


def _pack_paragraphs(
    paragraphs: list[str],
    *,
    chunk_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Greedy paragraph packing into ~``chunk_chars`` windows.

    A single oversized paragraph is sliced with character overlap so the
    chunk count stays bounded (one runaway log paste cannot dominate the
    index).
    """

    if chunk_chars <= 0:
        raise ValueError("chunk_chars must be > 0")
    overlap = max(0, min(overlap_chars, max(0, chunk_chars - 1)))

    out: list[str] = []
    current: list[str] = []
    current_len = 0

    def _flush() -> None:
        nonlocal current, current_len
        if current:
            joined = "\n\n".join(current).strip()
            if len(joined) >= MIN_CHUNK_CHARS or not out:
                out.append(joined)
            current = []
            current_len = 0

    for paragraph in paragraphs:
        # Slice oversized paragraphs into overlapping windows.
        if len(paragraph) > chunk_chars:
            _flush()
            stride = max(1, chunk_chars - overlap)
            for start in range(0, len(paragraph), stride):
                piece = paragraph[start : start + chunk_chars]
                if piece.strip():
                    out.append(piece.strip())
            continue

        # Would adding this paragraph blow the budget?  Flush first.
        if current_len + len(paragraph) + 2 > chunk_chars and current:
            _flush()

        current.append(paragraph)
        current_len += len(paragraph) + 2  # +2 ≈ "\n\n" separator

    _flush()
    return out


def chunk_record(
    record: CompilableRecord,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Split one record into chunks suitable for embedding.

    The record title is prepended to every chunk so a query that matches
    the record kind (e.g. "decision about material pipeline") still ranks
    chunks from inside that record highly even if the matched paragraph
    doesn't repeat the title.
    """

    body = body_without_title(record.body, record.title).strip()
    paragraphs = _split_into_paragraphs(body)
    pieces = _pack_paragraphs(
        paragraphs, chunk_chars=chunk_chars, overlap_chars=overlap_chars
    )
    if not pieces:
        # Fall back to the title alone so the record is still discoverable.
        pieces = [record.title.strip()] if record.title.strip() else []

    record_id = _record_id_for(record)
    chunks: list[Chunk] = []
    for idx, piece in enumerate(pieces):
        title = record.title.strip()
        text = f"{title}\n\n{piece}" if title and title not in piece else piece
        chunks.append(
            Chunk(
                record_id=record_id,
                chunk_id=_chunk_id_for(record_id, idx, piece),
                source_path=record.path,
                text=text,
            )
        )
    return chunks


def chunk_records(
    records: Iterable[CompilableRecord],
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
    max_chunks: int | None = None,
) -> Iterator[Chunk]:
    """Yield chunks across a stream of records, capped at ``max_chunks``.

    The cap exists so a corrupted corpus or runaway paste cannot make the
    index allocation explode (see §15.4.2 budget).
    """

    yielded = 0
    for record in records:
        for chunk in chunk_record(
            record, chunk_chars=chunk_chars, overlap_chars=overlap_chars
        ):
            if max_chunks is not None and yielded >= max_chunks:
                return
            yield chunk
            yielded += 1


__all__ = [
    "Chunk",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP",
    "MIN_CHUNK_CHARS",
    "chunk_record",
    "chunk_records",
]
