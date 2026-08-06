from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from memory_hub.api.routes_graph import _edge
from memory_hub.graph.projector import MAX_EVIDENCE_IDS, _merge_ids


def test_merge_ids_deduplicates_and_keeps_latest_bounded_values() -> None:
    existing = [f"rec-{index:03}" for index in range(MAX_EVIDENCE_IDS)]

    merged = _merge_ids(existing, ("rec-100", "rec-new", "rec-new"), MAX_EVIDENCE_IDS)

    assert len(merged) == MAX_EVIDENCE_IDS
    assert merged.count("rec-100") == 1
    assert merged[-1] == "rec-new"
    assert "rec-000" not in merged


def test_edge_evidence_and_source_event_details_are_independent() -> None:
    edge = SimpleNamespace(
        id=uuid4(),
        source_node_id=uuid4(),
        target_node_id=uuid4(),
        relation_type="affects",
        confidence=1.0,
        source_event_ids=["event-1"],
        evidence_ids=["rec-1"],
    )

    evidence_only = _edge(edge, include_source_event_ids=False, include_evidence_ids=True)
    events_only = _edge(edge, include_source_event_ids=True, include_evidence_ids=False)

    assert evidence_only["evidence_ids"] == ["rec-1"]
    assert "source_event_ids" not in evidence_only
    assert events_only["source_event_ids"] == ["event-1"]
    assert "evidence_ids" not in events_only
    assert evidence_only["evidence_count"] == 1
