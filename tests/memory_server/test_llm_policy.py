"""Tests for the LLM/non-LLM responsibility matrix.

Pins the policy so any change to the matrix forces a deliberate test update.
"""

from __future__ import annotations

import pytest

from servers.memory_server.memory_llm_policy import (
    LLM_CAPABILITY_MATRIX,
    UnknownCapability,
    capability_owner,
    must_be_deterministic,
    should_use_llm,
)


def test_owners_are_within_allowed_vocabulary() -> None:
    allowed = {"non_llm", "llm", "hybrid"}
    for name, owner in LLM_CAPABILITY_MATRIX.items():
        assert owner in allowed, f"{name} has invalid owner {owner!r}"


@pytest.mark.parametrize(
    "capability",
    [
        "raw_write",
        "frontmatter_parse",
        "event_log",
        "lock",
        "backup",
        "compaction",
        "fts_search",
        "scoring",
        "token_estimate",
        "compile_template_digest",
        "lineage_tracking",
        "governance_legacy",
        "budget_control",
    ],
)
def test_deterministic_capabilities_forbid_llm(capability: str) -> None:
    """raw / index / scoring / budget paths must never call an LLM."""
    assert capability_owner(capability) == "non_llm"
    assert must_be_deterministic(capability) is True
    assert should_use_llm(capability) is False


@pytest.mark.parametrize(
    "capability",
    ["distill_summary", "topic_cluster", "rewrite", "generate_handoff", "summarize_recall", "snapshot_narrative"],
)
def test_llm_native_capabilities_use_llm(capability: str) -> None:
    """Free-text generation/clustering/rewriting are LLM-native."""
    assert capability_owner(capability) == "llm"
    assert should_use_llm(capability) is True
    assert must_be_deterministic(capability) is False


@pytest.mark.parametrize(
    "capability",
    [
        "conflict_detection",
        "nl_query_parse",
        "classify_record",
        "extract_candidates",
        "merge_candidates",
        "generate_skill_candidate",
        "explain_conflict",
        "rebuild_key_document",
        "guard_compaction",
        "auto_memory_gate",
        "query_rewrite",
    ],
)
def test_hybrid_capabilities_use_llm_then_validate(capability: str) -> None:
    """Hybrid paths use LLM but the result is validated deterministically."""
    assert capability_owner(capability) == "hybrid"
    assert should_use_llm(capability) is True
    assert must_be_deterministic(capability) is False


def test_unknown_capability_raises() -> None:
    """Unregistered capabilities must error loudly — never silent default."""
    with pytest.raises(UnknownCapability):
        capability_owner("not_registered_xyz")
    with pytest.raises(UnknownCapability):
        should_use_llm("not_registered_xyz")
    with pytest.raises(UnknownCapability):
        must_be_deterministic("not_registered_xyz")


def test_matrix_pins_critical_invariants() -> None:
    """raw_write / lineage / budget MUST be deterministic forever.

    These are the load-bearing invariants of the autonomous-distillation
    contract. Demoting them to LLM/hybrid would let LLMs mutate the audit
    trail or override their own cost gates — never permitted.
    """
    assert LLM_CAPABILITY_MATRIX["raw_write"] == "non_llm"
    assert LLM_CAPABILITY_MATRIX["lineage_tracking"] == "non_llm"
    assert LLM_CAPABILITY_MATRIX["budget_control"] == "non_llm"
    assert LLM_CAPABILITY_MATRIX["event_log"] == "non_llm"
