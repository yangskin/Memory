"""LLM/non-LLM responsibility split — single source of truth.

The Memory MCP keeps deterministic code paths in charge of every capability the
non-LLM stack can already do well (writing raw records, FTS search, scoring,
compaction, governance, etc.) and only invokes an LLM for tasks where it is
genuinely better (free-text summarization, semantic clustering, NL query
parsing). This module codifies that boundary so any future code that asks
"should I call the LLM here?" has one place to consult.

See ``MemorySystemDesignDocument.md`` §12 for the rationale and the full
matrix.
"""

from __future__ import annotations

from typing import Literal

Owner = Literal["non_llm", "llm", "hybrid"]


# Capability → owner. Keep keys stable; tests below pin every entry.
# - "non_llm": deterministic code only. LLM MUST NOT participate.
# - "llm":     LLM-driven by design. Output lands in distilled layer.
# - "hybrid":  LLM proposes, deterministic code validates / persists.
LLM_CAPABILITY_MATRIX: dict[str, Owner] = {
    # --- Pure deterministic capabilities (LLM forbidden) ---
    "raw_write": "non_llm",
    "frontmatter_parse": "non_llm",
    "event_log": "non_llm",
    "lock": "non_llm",
    "backup": "non_llm",
    "compaction": "non_llm",
    "fts_search": "non_llm",
    "scoring": "non_llm",
    "token_estimate": "non_llm",
    "compile_template_digest": "non_llm",
    "lineage_tracking": "non_llm",
    "governance_legacy": "non_llm",
    "budget_control": "non_llm",
    # --- LLM-native capabilities (output is always distilled, replaceable) ---
    "distill_summary": "llm",
    "topic_cluster": "llm",
    "rewrite": "llm",
    "generate_handoff": "llm",
    # --- Hybrid: LLM proposes, deterministic layer accepts/persists ---
    "conflict_detection": "hybrid",
    "nl_query_parse": "hybrid",
    "classify_record": "hybrid",
    "extract_candidates": "hybrid",
    "merge_candidates": "hybrid",
    "generate_skill_candidate": "hybrid",
    "explain_conflict": "hybrid",
    # P4-C: rebuild a key document body. LLM proposes the prose,
    # deterministic layer enforces header/contracts and atomic write.
    "rebuild_key_document": "hybrid",
    # Guard overflow repair. LLM may propose a compacted replacement, but
    # deterministic validation enforces max_chars / max_tokens before write.
    "guard_compaction": "hybrid",
    # Agent-first automatic memory settling. LLM may propose whether the
    # current batch is worth settling and which derived views to refresh;
    # deterministic routing validates/falls back and performs all writes.
    "auto_memory_gate": "hybrid",
    # v0.10.0 — opt-in recall enhancements ----------------------------------
    # Read-only summary of already-retrieved records (memory_context
    # retrieve_context summarize=true). Same posture as distill_summary
    # but never persisted.
    "summarize_recall": "llm",
    # LLM-assisted query expansion for FTS recall. Strictly read-only:
    # variants feed back into the deterministic ranker.
    "query_rewrite": "hybrid",
    # LLM "executive summary" prepended to weekly/monthly snapshots
    # (deterministic body remains the source of truth).
    "snapshot_narrative": "llm",
    # 每次任务启动的上下文简报。LLM 只压缩和重组确定性证据，最终槽位、
    # 来源、预算和降级路径仍由确定性层控制。
    "generate_task_brief": "hybrid",
    # Background project reflection: the LLM extracts/reviews proposals, then
    # deterministic evidence gates decide whether anything may be persisted.
    "project_reflection": "hybrid",
}


class UnknownCapability(KeyError):
    """Raised when a capability is not registered in the matrix."""


def capability_owner(capability: str) -> Owner:
    """Return the registered owner for ``capability``.

    Raises :class:`UnknownCapability` for unregistered names so we never
    silently default to "no LLM" or "yes LLM" — every capability must be an
    explicit, reviewed choice.
    """
    try:
        return LLM_CAPABILITY_MATRIX[capability]
    except KeyError as exc:
        raise UnknownCapability(
            f"capability {capability!r} is not registered in LLM_CAPABILITY_MATRIX; "
            "register it (with rationale) before wiring code paths"
        ) from exc


def should_use_llm(capability: str) -> bool:
    """True iff ``capability`` is owned by the LLM layer (``llm`` or ``hybrid``).

    Use this at any call site that's deciding whether to invoke an LLM. The
    boolean intentionally collapses ``hybrid`` to ``True`` because hybrid
    paths still need an LLM call (it just gets validated afterwards).
    """
    return capability_owner(capability) in {"llm", "hybrid"}


def must_be_deterministic(capability: str) -> bool:
    """True iff ``capability`` MUST run through deterministic code only.

    This is the hard guard for paths that are forbidden from invoking an LLM
    — e.g. raw-record writing, lineage tracking, or budget control. Use this
    in assertions to prevent regressions where someone adds a stray LLM call
    in a deterministic-only path.
    """
    return capability_owner(capability) == "non_llm"


__all__ = [
    "LLM_CAPABILITY_MATRIX",
    "Owner",
    "UnknownCapability",
    "capability_owner",
    "must_be_deterministic",
    "should_use_llm",
]
