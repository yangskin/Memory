"""LLM pipeline glue: dedup cache + chunking + map-reduce orchestrator.

This module is the **only** place that decides how to spend LLM tokens
across multiple records. It composes the primitives from
:mod:`memory_llm` (raw → distilled bridge) with token estimation and a
content-hash dedup cache.

Design contract (see MemorySystemDesignDocument §12 + §11.3.x):

- **No global state.** Cache is passed in by the caller (or default
  in-memory `DistillCache()` is created per call). Persistence is the
  caller's responsibility.
- **Raw is sacred.** Pipeline only ever produces distilled records;
  raw inputs are read-only.
- **Deterministic dedup.** Same raw set + same model + same system prompt
  → same SHA-256 cache key → reuses the previous summary text.
- **Cost gates already enforced** by `LLMClient.chat()`. The chunker
  splits inputs proactively so each chunk respects
  `max_input_tokens_per_call`.
- **Map-reduce.** When more than one chunk is needed, each chunk is
  distilled independently then a final reduce pass collapses the partial
  summaries into one. Single-chunk inputs skip the reduce step (no extra
  tokens).
- **Recall-side summarization** (`summarize_records_for_recall`) is read-
  only: it never writes back to memory; the result is meant to be shown
  to the user / agent transient context, not appended to memory-bank.

The `__all__` export is the public contract. `cli.py`, `server_dispatch.py`
and tests should import from here, not from internals.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .memory_llm import (
    DEFAULT_DISTILL_SYSTEM_PROMPT,
    PROVENANCE_RAW,
    LLMClient,
    LLMConfigError,
    LLMRequestError,
    distill_raw_records,
    extract_text,
    make_distilled_record,
)
from .token_estimator import estimate_tokens

# Per-message overhead used by `LLMClient.estimate_prompt_tokens`. Mirrored
# here so chunking estimates align with the gate that will actually fire.
_PER_MESSAGE_OVERHEAD = 4

# Conservative reserve for the system prompt + per-record headers + the
# user instruction wrapper. Keeps us safely under the per-call input cap.
DEFAULT_PROMPT_OVERHEAD_TOKENS = 512

# Default minimum effective chunk budget. If the input cap minus overhead
# falls below this, we still try with this floor (caller should raise the
# input cap rather than silently swallow content).
MIN_CHUNK_BUDGET_TOKENS = 1024


# ── Cache ─────────────────────────────────────────────────────────────────


def compute_distill_cache_key(
    raw_records: list[dict[str, Any]],
    *,
    model: str,
    system_prompt: str,
    user_instruction: str | None = None,
) -> str:
    """Return a deterministic SHA-256 key for a raw set + model + prompts.

    Only fields that influence the LLM output are hashed:

    - Each raw record's ``id``, ``content``, ``source``, ``captured_at``
      (in input order — order matters because the prompt is concatenated).
    - The model identifier (different model → different summary, do not
      reuse).
    - The system prompt and optional user instruction (different prompt
      → different summary).

    Front-matter / tags / unrelated metadata are ignored on purpose so a
    later cosmetic edit to the raw record's tags does not invalidate the
    cache (the raw content itself is immutable, so this is safe).
    """
    payload = {
        "model": str(model or "").strip(),
        "system": str(system_prompt or "").strip(),
        "user": str(user_instruction or "").strip(),
        "records": [
            {
                "id": str(rec.get("id") or "").strip(),
                "content": str(rec.get("content") or ""),
                "source": str(rec.get("source") or "").strip(),
                "captured_at": str(rec.get("captured_at") or "").strip(),
            }
            for rec in raw_records
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass
class DistillCache:
    """In-memory cache keyed by :func:`compute_distill_cache_key`.

    Caller may persist this across runs by serialising ``entries``;
    nothing in the pipeline assumes process-lifetime state.
    """

    entries: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.entries.get(key)

    def put(self, key: str, summary: str) -> None:
        if key and summary:
            self.entries[key] = summary

    def __len__(self) -> int:  # pragma: no cover — trivial
        return len(self.entries)


class SqliteDistillCache:
    """Persistent ``.get/.put`` cache backed by a tiny SQLite file.

    Drop-in replacement for :class:`DistillCache` for callers that want
    to amortise LLM cost across process restarts. Schema is intentionally
    minimal: ``(key TEXT PRIMARY KEY, summary TEXT, created_at TEXT)``.
    Cache misses are silent; corrupt rows are treated as misses so a
    bad cache never breaks the LLM path.

    Thread-safety: each method opens its own short-lived connection so
    multiple threads / processes can share the file via SQLite's file
    locking (sufficient for the Memory MCP's "occasional summary"
    workload — not a high-throughput cache).
    """

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS distill_cache ("
        " key TEXT PRIMARY KEY,"
        " summary TEXT NOT NULL,"
        " created_at TEXT NOT NULL"
        ")"
    )

    def __init__(self, path: "Path | str") -> None:
        from pathlib import Path as _Path

        self.path = _Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self):
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute(self._SCHEMA)
        except Exception:
            # Schema init must never break callers; subsequent get/put
            # will fail soft and behave as a miss.
            pass

    def get(self, key: str) -> str | None:
        if not key:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT summary FROM distill_cache WHERE key = ?", (key,)
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        value = row[0]
        return value if isinstance(value, str) and value else None

    def put(self, key: str, summary: str) -> None:
        if not key or not summary:
            return
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO distill_cache(key, summary, created_at) "
                    "VALUES (?, ?, ?)",
                    (key, summary, ts),
                )
        except Exception:
            # Persistence failure is non-fatal — caller has the summary
            # in memory; the next call will re-LLM if cache stays empty.
            return

    def __len__(self) -> int:  # pragma: no cover — trivial
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(1) FROM distill_cache").fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0


def default_distill_cache(config: "Any | None" = None) -> "DistillCache | SqliteDistillCache":
    """Pick a cache implementation based on ``config``.

    Returns :class:`SqliteDistillCache` when ``config`` exposes a
    ``llm_cache_path`` attribute (a writable :class:`pathlib.Path`); falls
    back to the in-memory :class:`DistillCache` otherwise.  Callers that
    do not want persistence can construct ``DistillCache()`` directly.
    """

    if config is not None:
        path = getattr(config, "llm_cache_path", None)
        if path is not None:
            try:
                return SqliteDistillCache(path)
            except Exception:
                pass
    return DistillCache()


# ── Chunking ──────────────────────────────────────────────────────────────


def _record_token_estimate(record: dict[str, Any]) -> int:
    """Rough per-record token count: header line + content."""
    header = (
        f"--- raw id={record.get('id', '?')} "
        f"source={record.get('source', '?')} "
        f"captured_at={record.get('captured_at', '?')} ---"
    )
    body = str(record.get("content", ""))
    return estimate_tokens(header) + estimate_tokens(body) + _PER_MESSAGE_OVERHEAD


def chunk_raw_records(
    raw_records: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    overhead_tokens: int = DEFAULT_PROMPT_OVERHEAD_TOKENS,
) -> list[list[dict[str, Any]]]:
    """Split ``raw_records`` into batches that fit the per-call input cap.

    Greedy fit by estimated tokens. A single record larger than the
    effective budget is placed in its own chunk and left to the LLM
    client's input-side gate to reject (loud failure beats silent
    truncation of raw content).

    ``max_input_tokens`` of 0 disables the cap entirely (single chunk).
    """
    if not raw_records:
        return []
    if max_input_tokens <= 0:
        return [list(raw_records)]
    budget = max(MIN_CHUNK_BUDGET_TOKENS, max_input_tokens - max(0, overhead_tokens))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for rec in raw_records:
        if not isinstance(rec, dict):
            raise LLMConfigError("each raw record must be a dict")
        rec_tokens = _record_token_estimate(rec)
        if current and current_tokens + rec_tokens > budget:
            chunks.append(current)
            current = [rec]
            current_tokens = rec_tokens
        else:
            current.append(rec)
            current_tokens += rec_tokens
    if current:
        chunks.append(current)
    return chunks


# ── Map-reduce distillation (write-side) ─────────────────────────────────


REDUCE_SYSTEM_PROMPT = (
    "You are a memory distiller in REDUCE phase. Below are several partial "
    "summaries of an immutable raw record set. Merge them into ONE concise, "
    "faithful overview in the same language. Do NOT invent facts. Do NOT "
    "contradict the partial summaries. Output the merged summary directly, "
    "no preamble."
)


def _format_partial_summaries_for_reduce(parts: list[str]) -> str:
    return "\n\n".join(f"--- partial {i + 1} ---\n{text}" for i, text in enumerate(parts))


def map_reduce_distill(
    client: LLMClient,
    raw_records: list[dict[str, Any]],
    *,
    record_id: str,
    distilled_at: str,
    system_prompt: str | None = None,
    user_instruction: str | None = None,
    model: str | None = None,
    kind: str = "summary",
    tags: list[str] | None = None,
    confidence: float | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    cache: DistillCache | None = None,
    overhead_tokens: int = DEFAULT_PROMPT_OVERHEAD_TOKENS,
) -> dict[str, Any]:
    """Chunk → distil → reduce. Returns one distilled record.

    - Single chunk: equivalent to :func:`distill_raw_records`. Cache is
      consulted on the chunk hash; cache hit short-circuits to a record
      built from the cached text without hitting the network.
    - Multi-chunk: each chunk produces a partial summary (cached
      independently), then a final reduce LLM call merges the partials.
      The reduce step is also cached against the concatenated partials.

    The returned record's ``derived_from`` is the union of input raw ids
    in input order; cumulative LLM calls + cache hits are recorded in
    ``record["pipeline"]``.
    """
    if not raw_records:
        raise LLMConfigError("map_reduce_distill requires at least one raw record")
    for rec in raw_records:
        if not isinstance(rec, dict):
            raise LLMConfigError("each raw record must be a dict")
        if rec.get("provenance") != PROVENANCE_RAW and rec.get("immutable") is not True:
            raise LLMConfigError(
                f"map_reduce_distill only accepts raw records; got "
                f"id={rec.get('id', '?')!r} provenance={rec.get('provenance')!r}"
            )

    sys_prompt = system_prompt or DEFAULT_DISTILL_SYSTEM_PROMPT
    effective_model = model or client.config.model
    cache = cache if cache is not None else DistillCache()
    cap = client.config.max_input_tokens_per_call
    chunks = chunk_raw_records(raw_records, max_input_tokens=cap, overhead_tokens=overhead_tokens)

    partial_summaries: list[str] = []
    cache_hits = 0
    llm_calls = 0
    for chunk in chunks:
        key = compute_distill_cache_key(
            chunk,
            model=effective_model,
            system_prompt=sys_prompt,
            user_instruction=user_instruction,
        )
        cached = cache.get(key)
        if cached is not None:
            partial_summaries.append(cached)
            cache_hits += 1
            continue
        partial = distill_raw_records(
            client,
            chunk,
            record_id=f"{record_id}::partial::{len(partial_summaries)}",
            distilled_at=distilled_at,
            system_prompt=sys_prompt,
            user_instruction=user_instruction,
            model=model,
            kind=kind,
            tags=tags,
            confidence=confidence,
            max_tokens=max_tokens,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
        text = str(partial.get("content") or "").strip()
        if not text:
            raise LLMRequestError("LLM returned empty partial in map phase")
        partial_summaries.append(text)
        cache.put(key, text)
        llm_calls += 1

    if len(partial_summaries) == 1:
        merged = partial_summaries[0]
    else:
        joined = _format_partial_summaries_for_reduce(partial_summaries)
        reduce_key = "reduce::" + hashlib.sha256(
            (effective_model + "::" + REDUCE_SYSTEM_PROMPT + "::" + joined).encode("utf-8")
        ).hexdigest()
        cached_reduce = cache.get(reduce_key)
        if cached_reduce is not None:
            merged = cached_reduce
            cache_hits += 1
        else:
            response = client.chat(
                [
                    {"role": "system", "content": REDUCE_SYSTEM_PROMPT},
                    {"role": "user", "content": joined},
                ],
                model=model,
                max_tokens=max_tokens,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
            merged = extract_text(response).strip()
            if not merged:
                raise LLMRequestError("LLM returned empty reduce summary")
            cache.put(reduce_key, merged)
            llm_calls += 1

    derived_from = [str(rec.get("id") or "").strip() for rec in raw_records if str(rec.get("id") or "").strip()]
    record = make_distilled_record(
        record_id=record_id,
        content=merged,
        derived_from=derived_from,
        model=effective_model,
        distilled_at=distilled_at,
        kind=kind,
        confidence=confidence,
        tags=tags,
    )
    record["pipeline"] = {
        "chunks": len(chunks),
        "llm_calls": llm_calls,
        "cache_hits": cache_hits,
        "reduced": len(partial_summaries) > 1,
    }
    return record


# ── Recall-side summarization (read-only, transient output) ──────────────


SUMMARIZE_RECALL_SYSTEM_PROMPT = (
    "You are summarising memory records that were just retrieved for an "
    "agent. Produce a tight overview that surfaces the facts the agent "
    "needs to act on. Respect the source language. Do NOT invent facts. "
    "Do NOT modify the records — they are stored elsewhere; you are only "
    "synthesising a transient view. Output the overview directly."
)


def _format_records_for_recall(records: list[dict[str, Any]], *, max_chars_per_record: int = 4000) -> str:
    parts: list[str] = []
    for rec in records:
        rid = str(rec.get("id") or rec.get("path") or "?")
        kind = str(rec.get("record_kind") or rec.get("kind") or "")
        scope = str(rec.get("scope") or "")
        body = str(rec.get("content") or rec.get("body") or rec.get("body_excerpt") or "")
        if max_chars_per_record > 0 and len(body) > max_chars_per_record:
            body = body[:max_chars_per_record] + "…"
        head = f"--- record id={rid}"
        if kind:
            head += f" kind={kind}"
        if scope:
            head += f" scope={scope}"
        head += " ---"
        parts.append(f"{head}\n{body}")
    return "\n\n".join(parts)


def _chunk_recall_records(
    records: list[dict[str, Any]],
    *,
    max_input_tokens: int,
    overhead_tokens: int,
    max_chars_per_record: int,
) -> list[list[dict[str, Any]]]:
    if not records:
        return []
    if max_input_tokens <= 0:
        return [list(records)]
    budget = max(MIN_CHUNK_BUDGET_TOKENS, max_input_tokens - max(0, overhead_tokens))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for rec in records:
        body = str(rec.get("content") or rec.get("body") or rec.get("body_excerpt") or "")
        if max_chars_per_record > 0 and len(body) > max_chars_per_record:
            body = body[:max_chars_per_record]
        rec_tokens = estimate_tokens(body) + 32  # header + overhead
        if current and current_tokens + rec_tokens > budget:
            chunks.append(current)
            current = [rec]
            current_tokens = rec_tokens
        else:
            current.append(rec)
            current_tokens += rec_tokens
    if current:
        chunks.append(current)
    return chunks


def summarize_records_for_recall(
    client: LLMClient,
    records: Iterable[dict[str, Any]],
    *,
    query: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    cache: DistillCache | None = None,
    max_chars_per_record: int = 4000,
    overhead_tokens: int = DEFAULT_PROMPT_OVERHEAD_TOKENS,
) -> dict[str, Any]:
    """Run map-reduce summarisation over already-retrieved records.

    Read-only: never writes to memory. Returns
    ``{"summary": str, "model": str, "chunks": int, "llm_calls": int,
       "cache_hits": int, "reduced": bool}``.

    The cache key incorporates the optional ``query`` so a different user
    question against the same record set produces a fresh summary
    (different framing).
    """
    rec_list = [r for r in records if isinstance(r, dict)]
    if not rec_list:
        raise LLMConfigError("summarize_records_for_recall requires at least one record")
    cache = cache if cache is not None else DistillCache()
    sys_prompt = SUMMARIZE_RECALL_SYSTEM_PROMPT
    effective_model = model or client.config.model
    cap = client.config.max_input_tokens_per_call
    chunks = _chunk_recall_records(
        rec_list,
        max_input_tokens=cap,
        overhead_tokens=overhead_tokens,
        max_chars_per_record=max_chars_per_record,
    )

    partials: list[str] = []
    cache_hits = 0
    llm_calls = 0
    user_prefix = f"User query: {query.strip()}\n\n" if query and query.strip() else ""
    for chunk in chunks:
        body = _format_records_for_recall(chunk, max_chars_per_record=max_chars_per_record)
        user_msg = user_prefix + body
        key = "recall::" + hashlib.sha256(
            (effective_model + "::" + sys_prompt + "::" + user_msg).encode("utf-8")
        ).hexdigest()
        cached = cache.get(key)
        if cached is not None:
            partials.append(cached)
            cache_hits += 1
            continue
        response = client.chat(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            max_tokens=max_tokens,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
        text = extract_text(response).strip()
        if not text:
            raise LLMRequestError("LLM returned empty recall summary in map phase")
        partials.append(text)
        cache.put(key, text)
        llm_calls += 1

    if len(partials) == 1:
        summary = partials[0]
        reduced = False
    else:
        joined = _format_partial_summaries_for_reduce(partials)
        reduce_key = "recall_reduce::" + hashlib.sha256(
            (effective_model + "::" + REDUCE_SYSTEM_PROMPT + "::" + joined + "::" + (query or "")).encode("utf-8")
        ).hexdigest()
        cached = cache.get(reduce_key)
        if cached is not None:
            summary = cached
            cache_hits += 1
        else:
            response = client.chat(
                [
                    {"role": "system", "content": REDUCE_SYSTEM_PROMPT},
                    {"role": "user", "content": joined},
                ],
                model=model,
                max_tokens=max_tokens,
                thinking=thinking,
                reasoning_effort=reasoning_effort,
            )
            summary = extract_text(response).strip()
            if not summary:
                raise LLMRequestError("LLM returned empty recall reduce summary")
            cache.put(reduce_key, summary)
            llm_calls += 1
        reduced = True

    return {
        "summary": summary,
        "model": effective_model,
        "chunks": len(chunks),
        "llm_calls": llm_calls,
        "cache_hits": cache_hits,
        "reduced": reduced,
    }


__all__ = [
    "DEFAULT_PROMPT_OVERHEAD_TOKENS",
    "MIN_CHUNK_BUDGET_TOKENS",
    "REDUCE_SYSTEM_PROMPT",
    "SUMMARIZE_RECALL_SYSTEM_PROMPT",
    "DistillCache",
    "chunk_raw_records",
    "compute_distill_cache_key",
    "map_reduce_distill",
    "summarize_records_for_recall",
]
