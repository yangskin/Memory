from types import SimpleNamespace

from memory_hub.graph.extractor import extract_event_facts


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
        ("agent", "copilot-1"),
        ("class", "Thing"),
        ("file", "a.py"),
        ("module", "core"),
        ("task", "task-1"),
    ]
    assert len(first.edges) == 4


def test_extract_event_facts_excludes_private_events() -> None:
    assert extract_event_facts(_event(scope="personal")).nodes == ()
    assert extract_event_facts(_event(scope="user_private")).edges == ()


def test_extract_event_facts_skips_oversized_node_keys() -> None:
    facts = extract_event_facts(_event(metadata_json={"active_files": ["x" * 1025, "ok.py"]}))
    assert [(node.node_type, node.node_key) for node in facts.nodes if node.node_type == "file"] == [("file", "ok.py")]