from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from memory_hub.graph.extractor import InvalidGraphDelta, extract_event_facts, validate_graph_delta


def _seal(delta):
    body = {key: value for key, value in delta.items() if key != "delta_id"}
    canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {**body, "delta_id": f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"}


def _event(**overrides):
    values = {
        "scope": "project_shared",
        "agent_id": "copilot",
        "agent_instance_id": "copilot-1",
        "task_id": "task-1",
        "task_run_id": "run-1",
        "metadata_json": {"active_files": ["a.py", "a.py"], "class_names": ["Thing"], "module_names": ["core"]},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_extract_event_facts_is_deterministic_and_deduplicated() -> None:
    first = extract_event_facts(_event())
    second = extract_event_facts(_event())
    assert first == second
    assert [(node.node_type, node.node_key) for node in first.nodes] == [
        ("class", "Thing"),
        ("file", "a.py"),
        ("module", "core"),
        ("task", "task-1"),
    ]
    assert len(first.edges) == 3


def test_extract_event_facts_does_not_project_agent_instances() -> None:
    facts = extract_event_facts(_event(task_id="", metadata_json={"active_files": ["a.py"]}))
    assert [(node.node_type, node.node_key) for node in facts.nodes] == [("file", "a.py")]
    assert facts.edges == ()


def test_extract_event_facts_excludes_private_events() -> None:
    assert extract_event_facts(_event(scope="personal")).nodes == ()
    assert extract_event_facts(_event(scope="user_private")).edges == ()


def test_extract_event_facts_skips_oversized_node_keys() -> None:
    facts = extract_event_facts(_event(metadata_json={"active_files": ["x" * 1025, "ok.py"]}))
    assert [(node.node_type, node.node_key) for node in facts.nodes if node.node_type == "file"] == [("file", "ok.py")]


def test_extract_event_facts_prefers_valid_graph_delta() -> None:
    delta = _seal({
        "version": "1.0",
        "task_id": "task-1",
        "nodes": [
            {"type": "task", "key": "task-1", "name": "task-1"},
            {"type": "module", "key": "outbox", "name": "outbox"},
        ],
        "edges": [
            {
                "source": {"type": "task", "key": "task-1"},
                "target": {"type": "module", "key": "outbox"},
                "relation": "implements",
                "origin": "observed",
                "confidence": 1.0,
                "evidence_ids": ["rec-1"],
            }
        ],
    })

    facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": delta}))

    assert [(node.node_type, node.node_key) for node in facts.nodes] == [("module", "outbox"), ("task", "task-1")]
    assert [(edge.relation_type, edge.confidence) for edge in facts.edges] == [("implements", 1.0)]
    assert facts.edges[0].evidence_ids == ("rec-1",)


def test_extract_event_facts_falls_back_when_graph_delta_is_invalid() -> None:
    invalid = {
        "version": "1.0",
        "task_id": "another-task",
        "nodes": [],
        "edges": [],
    }

    facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": invalid}))

    assert [(node.node_type, node.node_key) for node in facts.nodes] == [("file", "legacy.py"), ("task", "task-1")]
    assert [edge.relation_type for edge in facts.edges] == ["affects"]


def test_validate_graph_delta_strictly_rejects_invalid_submission() -> None:
    invalid = {
        "version": "1.0",
        "task_id": "another-task",
        "nodes": [],
        "edges": [],
    }

    with pytest.raises(InvalidGraphDelta):
        validate_graph_delta({"graph_delta": invalid}, "task-1")


def test_extract_event_facts_rejects_non_finite_confidence_and_unbounded_evidence() -> None:
    base = {
        "version": "1.0",
        "task_id": "task-1",
        "nodes": [
            {"type": "task", "key": "task-1", "name": "task-1"},
            {"type": "module", "key": "core", "name": "core"},
        ],
        "edges": [
            {
                "source": {"type": "task", "key": "task-1"},
                "target": {"type": "module", "key": "core"},
                "relation": "depends_on",
                "origin": "inferred",
                "confidence": 0.9,
                "evidence_ids": ["rec-1"],
            }
        ],
    }
    non_finite = deepcopy(base)
    non_finite["edges"][0]["confidence"] = float("nan")
    unbounded = deepcopy(base)
    unbounded["edges"][0]["evidence_ids"] = [f"rec-{index}" for index in range(17)]
    non_string = deepcopy(base)
    non_string["edges"][0]["evidence_ids"] = [{"id": "rec-1"}]

    nan_facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": _seal(non_finite)}))
    unbounded_facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": _seal(unbounded)}))
    non_string_facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": _seal(non_string)}))

    assert [node.node_key for node in nan_facts.nodes] == ["legacy.py", "task-1"]
    assert [node.node_key for node in unbounded_facts.nodes] == ["legacy.py", "task-1"]
    assert [node.node_key for node in non_string_facts.nodes] == ["legacy.py", "task-1"]


def test_extract_event_facts_rejects_tampered_delta_and_duplicate_nodes() -> None:
    base = {
        "version": "1.0",
        "task_id": "task-1",
        "nodes": [
            {"type": "task", "key": "task-1", "name": "task-1"},
            {"type": "module", "key": "core", "name": "core"},
        ],
        "edges": [],
    }
    tampered = _seal(base)
    tampered["nodes"][1]["key"] = "changed"
    duplicate = deepcopy(base)
    duplicate["nodes"].append(deepcopy(duplicate["nodes"][1]))

    tampered_facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": tampered}))
    duplicate_facts = extract_event_facts(_event(metadata_json={"active_files": ["legacy.py"], "graph_delta": _seal(duplicate)}))

    assert [node.node_key for node in tampered_facts.nodes] == ["legacy.py", "task-1"]
    assert [node.node_key for node in duplicate_facts.nodes] == ["legacy.py", "task-1"]