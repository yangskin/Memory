from types import SimpleNamespace

import pytest

from memory_hub.graph.semantic import project_graph_inputs, validate_project_graph
from memory_hub.worker.runner import _validate_structured


def test_worker_rejects_unknown_source_event() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["unknown"],
    }
    with pytest.raises(ValueError, match="outside"):
        _validate_structured(job, payload, {"evt_1"})


def test_worker_accepts_valid_user_brief() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["evt_1"],
    }
    assert _validate_structured(job, payload, {"evt_1"})["summary"] == "report"


def test_worker_rejects_conclusion_without_its_own_sources() -> None:
    job = SimpleNamespace(brief_type="user_recent")
    payload = {
        "schema_version": "1.0",
        "as_of": "2026-08-04T00:00:00Z",
        "summary": "report",
        "workstreams": [{"task_id": "task-1"}],
        "cross_agent_overlaps": [],
        "stale_workstreams": [],
        "source_event_ids": ["evt_1"],
    }
    with pytest.raises(ValueError, match="missing source_event_ids"):
        _validate_structured(job, payload, {"evt_1"})


def test_project_graph_keeps_only_edges_supported_by_one_annotated_event() -> None:
    events = [
        {
            "event_id": "evt_1",
            "entities": [
                {"type": "class", "key": "CheckoutVerifier"},
                {"type": "module", "key": "payments"},
            ],
            "content": "Checkout validates payments.",
        },
        {
            "event_id": "evt_2",
            "entities": [
                {"type": "class", "key": "InventoryVerifier"},
                {"type": "module", "key": "stock"},
            ],
            "content": "Inventory validates stock.",
        },
    ]
    raw = {
        "nodes": [
            {"type": "class", "key": "CheckoutVerifier"},
            {"type": "module", "key": "payments"},
            {"type": "class", "key": "InventoryVerifier"},
            {"type": "module", "key": "stock"},
        ],
        "edges": [
            {
            "source": {"type": "class", "key": "CheckoutVerifier"},
                "target": {"type": "module", "key": "payments"},
                "relation": "validates",
                "confidence": 0.9,
                "evidence_ids": ["evt_1"],
            },
            {
                "source": {"type": "class", "key": "CheckoutVerifier"},
                "target": {"type": "module", "key": "stock"},
                "relation": "validates",
                "confidence": 0.9,
                "evidence_ids": ["evt_1"],
            },
        ],
    }

    graph = validate_project_graph(raw, events)

    assert graph["source_event_ids"] == ["evt_1"]
    assert graph["nodes"] == [
        {"type": "class", "key": "CheckoutVerifier", "name": "CheckoutVerifier"},
        {"type": "module", "key": "payments", "name": "payments"},
    ]
    assert graph["edges"] == [
        {
            "source": {"type": "class", "key": "CheckoutVerifier"},
            "target": {"type": "module", "key": "payments"},
            "relation": "validates",
            "confidence": 0.9,
            "evidence_ids": ["evt_1"],
        }
    ]


def test_project_graph_inputs_exclude_system_area_from_entities() -> None:
    source_event = SimpleNamespace(
        event_id="evt_1",
        scope="project_shared",
        record_kind="handoff",
        content_markdown="# Cart handoff\n\nCart implementation details.",
        metadata_json={"system_area": "Cart handoff", "class_names": ["CartActor"]},
    )
    title_only_event = SimpleNamespace(
        event_id="evt_2",
        scope="project_shared",
        record_kind="report",
        content_markdown="Validation report.",
        metadata_json={"system_area": "Validation report"},
    )

    inputs = project_graph_inputs([source_event, title_only_event])

    assert len(inputs) == 1
    assert inputs[0]["source"] == {"type": "source", "key": "event:evt_1", "name": "handoff: Cart handoff"}
    assert inputs[0]["entities"] == [{"type": "class", "key": "CartActor", "name": "CartActor"}]