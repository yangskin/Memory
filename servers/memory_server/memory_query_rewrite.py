"""LLM-assisted query rewriting for FTS / metadata recall (v0.10.0 §15.3).

Plain FTS recall over Markdown corpora misses obvious query reformulations:
synonyms, abbreviations, language switches (中→英 / 英→中), and decomposed
sub-questions.  This module asks the LLM to generate up to ``max_variants``
short, FTS-friendly variants of the user's query.  The deterministic ranker
in :mod:`memory_retrieval` still does the heavy lifting; rewriting only
*broadens* the candidate set.

Hard contracts (mirroring §12 LLM-vs-non-LLM split):

- **Read-only.**  Variants are returned to the caller, never persisted to
  ``memory-bank/`` or ``.ai-context/``.
- **Bounded.**  At most ``max_variants`` strings, each capped to a
  reasonable character length so a runaway model cannot blow the budget.
- **Deterministic-cacheable.**  Same query + same model produces the same
  cache key, so repeated calls are free.
- **Non-fatal.**  Any LLM error returns ``ok=False`` with a structured
  status; the caller continues with the original query.

The runner integration (see :func:`memory_retrieval._maybe_rewrite_query`)
runs this module under :func:`memory_llm_runner.run_llm_capability` so the
``query_rewrite`` capability flag, timeout, and budget are all enforced
without any per-call boilerplate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .memory_llm import LLMClient, LLMConfigError, LLMRequestError, extract_text
from .memory_llm_pipeline import DistillCache

logger = logging.getLogger(__name__)


REWRITE_SYSTEM_PROMPT = (
    "You expand a user's memory-search query into short alternative queries that "
    "improve recall against a Markdown / front-matter corpus indexed by FTS5. "
    "Rules: respond ONLY with a JSON array of 1-N strings (no prose, no code "
    "fences). Each string must be short (<=120 chars) and self-contained. "
    "Include synonyms, common abbreviations, and plausible translations between "
    "Chinese and English when the original term has an obvious counterpart. "
    "Never invent facts. Never include the original query verbatim — the caller "
    "already has it. If you cannot improve the query, respond with []."
)

# Sane upper bound so a buggy model cannot smuggle a giant payload back.
MAX_VARIANT_CHARS = 200
HARD_MAX_VARIANTS = 8


@dataclass
class QueryRewriteResult:
    """Outcome of :func:`rewrite_query`.

    ``variants`` is empty when the LLM returned an empty list (i.e. it had
    nothing useful to add).  The original query is *not* included; the
    caller is expected to keep ranking against it independently.
    """

    ok: bool
    original: str
    variants: list[str] = field(default_factory=list)
    model: str = ""
    cache_hit: bool = False
    raw_response: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": bool(self.ok),
            "original": self.original,
            "variants": list(self.variants),
            "model": self.model,
            "cache_hit": bool(self.cache_hit),
        }
        if self.error:
            out["error"] = self.error
        return out


def _build_user_message(query: str, context_hint: str | None, max_variants: int) -> str:
    parts = [
        f"Original query: {query.strip()}",
        f"Maximum variants: {max_variants}",
    ]
    if context_hint and context_hint.strip():
        parts.append(f"Domain hint: {context_hint.strip()}")
    parts.append(
        "Return JSON array only. Example shape: [\"variant 1\", \"variant 2\"]."
    )
    return "\n".join(parts)


def _compute_cache_key(*, query: str, model: str, context_hint: str | None, max_variants: int) -> str:
    payload = json.dumps(
        {
            "query": query.strip(),
            "model": model.strip(),
            "context_hint": (context_hint or "").strip(),
            "max_variants": int(max_variants),
            "_v": "v0.10.0",
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return "qrw::" + hashlib.sha256(payload).hexdigest()


def _parse_variants(text: str) -> list[str]:
    """Best-effort JSON-array extraction from the model's reply.

    Tolerates the model wrapping the array in markdown code fences or
    leading prose; refuses to invent variants when parsing fails (returns
    an empty list so the caller falls back to the original query).
    """

    if not isinstance(text, str):
        return []
    cleaned = text.strip()
    if not cleaned:
        return []
    # Strip ``` fences if present.
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    # If still not pure JSON, look for the first '[' and last ']'.
    if not cleaned.startswith("["):
        start = cleaned.find("[")
        end = cleaned.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            continue
        token = item.strip()
        if not token:
            continue
        if len(token) > MAX_VARIANT_CHARS:
            token = token[:MAX_VARIANT_CHARS].rstrip()
        out.append(token)
    return out


def _dedupe_variants(variants: Iterable[str], *, original: str, max_variants: int) -> list[str]:
    """Drop duplicates and any variant that just echoes the original."""
    seen: set[str] = {original.strip().lower()}
    cleaned: list[str] = []
    for token in variants:
        key = token.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(token)
        if len(cleaned) >= max_variants:
            break
    return cleaned


def rewrite_query(
    client: LLMClient,
    query: str,
    *,
    max_variants: int = 3,
    context_hint: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    cache: DistillCache | None = None,
) -> QueryRewriteResult:
    """Ask the LLM for at most ``max_variants`` recall-friendly variants.

    Parameters
    ----------
    client:
        Already-built :class:`LLMClient`.  Caller controls timeout/budget
        via the client config (or the unified runner).
    query:
        User's original question.  Empty/whitespace returns ``ok=True`` with
        an empty variants list so callers do not need to special-case it.
    max_variants:
        Upper bound on returned variants.  Capped to :data:`HARD_MAX_VARIANTS`.
    context_hint:
        Optional short string appended to the prompt (e.g. project name or
        active task) so variants can use the right vocabulary.
    cache:
        Optional :class:`DistillCache` to memoise.  Cache hits short-circuit
        without hitting the network.
    """

    original = (query or "").strip()
    if not original:
        return QueryRewriteResult(ok=True, original="", variants=[], model=model or client.config.model)

    bounded_variants = max(1, min(int(max_variants or 3), HARD_MAX_VARIANTS))
    effective_model = model or client.config.model
    cache = cache if cache is not None else DistillCache()
    cache_key = _compute_cache_key(
        query=original,
        model=effective_model,
        context_hint=context_hint,
        max_variants=bounded_variants,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        variants = _dedupe_variants(_parse_variants(cached), original=original, max_variants=bounded_variants)
        return QueryRewriteResult(
            ok=True,
            original=original,
            variants=variants,
            model=effective_model,
            cache_hit=True,
            raw_response=cached,
        )

    user_msg = _build_user_message(original, context_hint, bounded_variants)
    try:
        response = client.chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            max_tokens=max_tokens,
        )
    except LLMConfigError as exc:
        return QueryRewriteResult(ok=False, original=original, error=f"config: {exc}", model=effective_model)
    except LLMRequestError as exc:
        return QueryRewriteResult(ok=False, original=original, error=f"request: {exc}", model=effective_model)

    text = extract_text(response).strip()
    if not text:
        return QueryRewriteResult(ok=True, original=original, variants=[], model=effective_model, raw_response="")

    cache.put(cache_key, text)
    variants = _dedupe_variants(_parse_variants(text), original=original, max_variants=bounded_variants)
    return QueryRewriteResult(
        ok=True,
        original=original,
        variants=variants,
        model=effective_model,
        cache_hit=False,
        raw_response=text,
    )


__all__ = [
    "HARD_MAX_VARIANTS",
    "MAX_VARIANT_CHARS",
    "REWRITE_SYSTEM_PROMPT",
    "QueryRewriteResult",
    "rewrite_query",
]
