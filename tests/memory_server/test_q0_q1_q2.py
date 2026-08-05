from __future__ import annotations

import json
from pathlib import Path

from scripts.eval_recall import _duplicate_rate_at, _off_topic_at, _precision_at
from servers.memory_server import memory_retrieval, memory_task_brief
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_corpus import iter_parsed_records
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_reflection import (
    publish_reflection_proposal,
    validate_reflection_frame,
)
from servers.memory_server.memory_retrieval import memory_retrieve_context
from servers.memory_server.memory_task_brief import build_task_brief
from servers.memory_server.server_dispatch import _compact_memory_item


def _write(
    config,
    *,
    title: str,
    body: str,
    task_id: str,
    scope: str = "project_shared",
    status: str = "validated",
    kind: str = "note",
    importance: float | None = None,
    **kwargs,
):
    return memory_write_record(
        config,
        content_markdown=f"# {title}\n\n{body}\n",
        record_kind=kind,
        scope=scope,
        status=status,
        author="alice",
        task_id=task_id,
        importance_score=importance,
        **kwargs,
    )


def _metadata(config, record_id: str) -> dict:
    records, _stats = iter_parsed_records(config)
    return next(record.metadata for record in records if str(record.metadata.get("id")) == record_id)


def test_q0_relevance_band_beats_generic_high_importance_records(repo: Path) -> None:
    config = load_config(repo)
    exact = _write(
        config,
        title="SampleDomain Mesh Segment MaterialGroup 多材质减面",
        body="Primary 和 secondary 几何压缩必须保持 MaterialGroup 材质语义。",
        task_id="task-packed",
        kind="decision",
        importance=0.1,
        system_area="SampleEditor mesh segment compression",
    )
    broad_ids = []
    for index, topic in enumerate(("墙片集合", "武器伤害", "Tablet Niagara", "事件绑定", "编辑器选择")):
        broad = _write(
            config,
            title=f"SampleDomain {topic}",
            body=f"SampleDomain 通用高重要度历史记录 {index}。",
            task_id=f"task-broad-{index}",
            kind="incident",
            importance=1.0,
            system_area=f"SampleDomain {topic}",
        )
        broad_ids.append(broad["id"])
    workflow_only = _write(
        config,
        title="SampleDomain validation code next step",
        body="继续开发、复核、验证并给出下一步代码和验收入口。",
        task_id="task-workflow-words-only",
        kind="handoff",
        importance=1.0,
        system_area="SampleEditor",
    )

    result = memory_retrieve_context(
        config,
        query="SampleDomain Mesh Segment MaterialGroup 多材质 减面 primary secondary 几何压缩",
        user="alice",
        ranking_version="v2",
        max_items=10,
        top_k=10,
        max_chars=20_000,
        max_tokens=8_000,
    )

    assert result["ok"] is True
    assert result["context_items"][0]["id"] == exact["id"]
    assert exact["id"] in {item["id"] for item in result["context_items"]}
    assert result["pipeline"]["ranking_version"] == "v2"
    assert result["context_items"][0]["relevance_band"] >= 2
    workflow_item = next(item for item in result["context_items"] if item["id"] == workflow_only["id"])
    assert workflow_item["relevance_band"] == 1


def test_q0_four_character_cjk_phrase_recalls_composed_terms(repo: Path) -> None:
    config = load_config(repo)
    record = _write(
        config,
        title="SampleDomain 烘焙数据体积基线",
        body="烘焙数据进一步有损压缩，PCA 字节是主要优化对象。",
        task_id="task-cjk-bigram",
        kind="decision",
        system_area="SampleEditor compression",
    )

    result = memory_retrieve_context(
        config,
        query="烘焙压缩 几何字节",
        user="alice",
        ranking_version="v2",
        max_items=5,
        top_k=5,
        max_chars=10_000,
        max_tokens=4_000,
    )

    item = next(item for item in result["context_items"] if item["id"] == record["id"])
    assert item["relevance_band"] >= 2
    assert item["query_match_score"] >= 0.35


def test_q0_collapses_auto_team_settlement_and_superseded_records(repo: Path) -> None:
    config = load_config(repo)
    personal = _write(
        config,
        title="Mesh Segment 压缩决策",
        body="对 mesh segment 做属性感知减面。",
        task_id="task-source",
        scope="personal",
        kind="decision",
    )
    shared = _write(
        config,
        title="Mesh Segment 压缩决策共享摘要",
        body="共享摘要：属性感知减面并保护材质边界。",
        task_id="task-source",
        kind="decision",
        provenance="auto_team_settlement",
        derived_from_record_ids=[personal["id"]],
        replaceable=True,
    )
    old = _write(
        config,
        title="Mesh Segment 旧全局结论",
        body="旧结论要求保留全部 secondary 几何。",
        task_id="task-old",
        kind="decision",
        provenance="background_reflection",
        replaceable=True,
    )
    replacement = _write(
        config,
        title="Mesh Segment 新全局结论",
        body="新结论允许验证后压缩 secondary 几何。",
        task_id="task-new",
        kind="decision",
        provenance="background_reflection",
        replaceable=True,
        supersedes=[old["id"]],
        derived_from_record_ids=[old["id"]],
    )

    result = memory_retrieve_context(
        config,
        query="Mesh Segment 压缩 secondary 几何",
        user="alice",
        ranking_version="v2",
        max_items=10,
        max_chars=40_000,
        max_tokens=12_000,
    )
    ids = [item["id"] for item in result["context_items"]]
    auto_group = [item for item in result["context_items"] if {personal["id"], shared["id"]}.intersection({item["id"], *item.get("collapsed_record_ids", [])})]

    assert len(auto_group) == 1
    assert set(auto_group[0]["collapsed_record_ids"]) == {personal["id"], shared["id"]}
    assert replacement["id"] in ids
    assert old["id"] not in ids
    assert old["id"] in next(item for item in result["context_items"] if item["id"] == replacement["id"])["collapsed_record_ids"]


def test_q0_boost_facets_do_not_filter_exact_query_match(repo: Path) -> None:
    config = load_config(repo)
    exact = _write(
        config,
        title="Mesh Segment MaterialGroup reducer",
        body="多材质 primary secondary reduction。",
        task_id="task-exact",
        kind="decision",
        system_area="compression",
    )
    hinted = _write(
        config,
        title="SampleEditor 通用面板",
        body="不相关的编辑器面板历史。",
        task_id="task-hint",
        plugin_names=["SampleEditor"],
        system_area="ui",
    )

    boosted = memory_retrieve_context(
        config,
        query="Mesh Segment MaterialGroup primary secondary",
        user="alice",
        plugin_names=["SampleEditor"],
        facet_mode="boost",
        ranking_version="v2",
        max_items=5,
    )
    hard = memory_retrieve_context(
        config,
        query="Mesh Segment MaterialGroup primary secondary",
        user="alice",
        plugin_names=["SampleEditor"],
        facet_mode="hard",
        ranking_version="v2",
        max_items=5,
    )

    assert exact["id"] in {item["id"] for item in boosted["context_items"]}
    assert exact["id"] not in {item["id"] for item in hard["context_items"]}
    assert hinted["id"] not in {item["id"] for item in boosted["context_items"][:1]}
    assert boosted["pipeline"]["facet_mode"] == "boost"


def test_q0_v2_failure_falls_back_to_v1_without_breaking_recall(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    record = _write(
        config,
        title="Fallback retrieval",
        body="ranking fallback evidence",
        task_id="task-fallback",
        kind="decision",
    )
    original = memory_retrieval._rank_records

    def fail_v2(*args, **kwargs):
        if kwargs.get("ranking_version") == "v2":
            raise RuntimeError("injected v2 failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(memory_retrieval, "_rank_records", fail_v2)
    result = memory_retrieval.memory_retrieve_context(
        config,
        query="fallback retrieval evidence",
        user="alice",
        ranking_version="v2",
    )

    assert result["ok"] is True
    assert record["id"] in {item["id"] for item in result["context_items"]}
    assert result["pipeline"]["ranking_version"] == "v1"
    assert result["pipeline"]["ranking_fallback"] == "v2_failed:RuntimeError"


def test_q0_superseding_record_inherits_old_query_match(repo: Path) -> None:
    config = load_config(repo)
    old = _write(
        config,
        title="Legacy polygon reducer",
        body="LEGACY_ONLY_QUERY_TOKEN",
        task_id="task-legacy",
        kind="decision",
        provenance="background_reflection",
        replaceable=True,
    )
    replacement = _write(
        config,
        title="Current reducer policy",
        body="The current durable policy supersedes prior guidance.",
        task_id="task-current",
        kind="decision",
        provenance="background_reflection",
        replaceable=True,
        supersedes=[old["id"]],
    )

    result = memory_retrieve_context(config, query="LEGACY_ONLY_QUERY_TOKEN", user="alice")
    ids = {item["id"] for item in result["context_items"]}
    current = next(item for item in result["context_items"] if item["id"] == replacement["id"])

    assert old["id"] not in ids
    assert old["id"] in current["collapsed_record_ids"]


def test_q0_domain_query_downranks_memory_meta_records(repo: Path) -> None:
    config = load_config(repo)
    meta = _write(
        config,
        title="SampleDomain Mesh Segment MaterialGroup 多材质检索评估",
        body="这是一条 Memory MCP Task Brief 召回质量评估，不是 SampleDomain 实现事实。",
        task_id="task-memory-eval",
        kind="validation_result",
        importance=1.0,
        system_area="Memory MCP retrieval evaluation",
    )
    domain = _write(
        config,
        title="SampleDomain Mesh Segment reducer 材质契约",
        body="Primary 和 secondary 减面必须保持 MaterialGroup 到材质槽的映射。",
        task_id="task-sample_domain-domain",
        kind="decision",
        importance=0.1,
        system_area="SampleEditor mesh segment compression",
    )

    result = memory_retrieve_context(
        config,
        query="SampleDomain Mesh Segment MaterialGroup 多材质 primary secondary reducer",
        user="alice",
        ranking_version="v2",
        max_items=10,
    )

    items = result["context_items"]
    assert items[0]["id"] == domain["id"]
    meta_item = next(item for item in items if item["id"] == meta["id"])
    assert meta_item["memory_role"] == "memory_meta"
    assert meta_item["query_role"] == "domain"
    assert meta_item["role_alignment"] == 0


def test_q0_memory_hub_query_prioritizes_memory_meta_record(repo: Path) -> None:
    config = load_config(repo)
    meta = _write(
        config,
        title="Memory Hub shared context authority",
        body="Memory Hub supplies the authoritative project brief and shared agent context.",
        task_id="task-memory-hub",
        kind="decision",
        system_area="Memory Hub shared context",
    )
    domain = _write(
        config,
        title="SampleDomain asset hub runtime budget",
        body="The asset hub keeps a bounded runtime memory budget for texture streaming.",
        task_id="task-domain-hub",
        kind="decision",
        importance=1.0,
        system_area="SampleEditor asset pipeline",
    )

    result = memory_retrieve_context(
        config,
        query="Memory Hub",
        user="alice",
        ranking_version="v2",
        max_items=10,
        top_k=10,
    )

    assert result["context_items"][0]["id"] == meta["id"]
    meta_item = next(item for item in result["context_items"] if item["id"] == meta["id"])
    assert meta_item["query_role"] == "memory_meta"
    assert meta_item["memory_role"] == "memory_meta"
    domain_item = next(item for item in result["context_items"] if item["id"] == domain["id"])
    item_ids = [item["id"] for item in result["context_items"]]
    assert item_ids.index(meta["id"]) < item_ids.index(domain["id"])
    assert domain_item["memory_role"] == "domain"
    assert domain_item["role_alignment"] == 1


def test_q0_canonical_representative_inherits_group_best_relevance(repo: Path) -> None:
    config = load_config(repo)
    personal = _write(
        config,
        title="EXACT_CANONICAL_SIGNAL SampleDomain Mesh Segment",
        body="个人侧完整的 MaterialGroup 减面决策。",
        task_id="task-canonical",
        scope="personal",
        kind="decision",
        system_area="SampleEditor mesh segment compression",
    )
    shared = _write(
        config,
        title="SampleDomain 共享摘要",
        body="共享侧保留材质语义。",
        task_id="task-canonical",
        kind="decision",
        system_area="SampleEditor",
        provenance="auto_team_settlement",
        derived_from_record_ids=[personal["id"]],
    )

    result = memory_retrieve_context(
        config,
        query="EXACT_CANONICAL_SIGNAL SampleDomain Mesh Segment",
        user="alice",
        ranking_version="v2",
        max_items=10,
    )
    item = next(entry for entry in result["context_items"] if shared["id"] in {entry["id"], *entry["collapsed_record_ids"]})

    assert item["id"] == shared["id"]
    assert item["collapsed_best_record_id"] == personal["id"]
    assert item["relevance_band"] >= 2
    assert item["query_match_score"] >= 0.35

    compact = _compact_memory_item(item)
    assert compact["memory_role"] == "domain"
    assert compact["query_role"] == "domain"
    assert compact["collapsed_best_record_id"] == personal["id"]


def test_q1_task_brief_v39_injects_bounded_experience_and_accepts_large_budget(repo: Path) -> None:
    config = load_config(repo)
    stable = _write(
        config,
        title="Mesh Segment 稳定压缩决策",
        body="STABLE_ABSTRACT_VISIBLE：保护 MaterialGroup 材质边界并量化 primary/secondary 几何字节。",
        task_id="task-stable",
        kind="decision",
        status="validated",
        system_area="SampleEditor mesh segment compression",
    )
    episodic = _write(
        config,
        title="Mesh Segment 最近验证",
        body="EPISODIC_ABSTRACT_VISIBLE：L_SampleArena 是当前 A/B 测试入口。",
        task_id="task-episodic",
        scope="personal",
        kind="validation_result",
        status="validated",
        system_area="SampleEditor mesh segment compression",
    )
    task_context = {
        "context_token": "ctx-q1",
        "task_id": "task-current",
        "task_run_id": "run-current",
        "user": "alice",
        "workspace_id": str(repo).replace("\\", "/"),
        "branch": None,
    }

    result = build_task_brief(
        config,
        task_context=task_context,
        current_task={"content": ""},
        user_goal="继续开发 SampleDomain Mesh Segment MaterialGroup 多材质 primary secondary 几何压缩",
        active_files=[],
        brief_mode="deep",
        max_chars=800_000,
        max_tokens=200_000,
        use_llm=False,
        refresh=True,
    )

    assert result["ok"] is True
    assert result["generation"]["prompt_version"] == "task-brief-v3.9"
    assert result["budget_report"]["max_chars"] == 800_000
    assert result["budget_report"]["max_tokens"] == 200_000
    assert "STABLE_ABSTRACT_VISIBLE" in result["brief_markdown"]
    assert "EPISODIC_ABSTRACT_VISIBLE" in result["brief_markdown"]
    assert result["quality"]["memory_abstracts_included"] is True
    assert result["quality"]["memory_bodies_included"] is False
    stable_ids = {item["id"] for item in result["map"]["memory"]["stable"]}
    validation_ids = {item.get("record_id") for item in result["map"]["authority"]["validation"]}
    assert stable["id"] in stable_ids
    assert episodic["id"] in validation_ids
    assert episodic["id"] not in {item["id"] for item in result["map"]["memory"]["episodic"]}


def test_q1_domain_brief_filters_meta_sources_and_applies_section_quotas(repo: Path) -> None:
    config = load_config(repo)
    relevant_path = repo / "Plugins/SampleEditor/Source/SampleEditor/Private/MeshSegmentReducer.cpp"
    irrelevant_path = repo / "Source/SampleGame/GameplayCore/SampleActor.cpp"
    meta_path = repo / "MCP/Memory/README.md"
    for path in (relevant_path, irrelevant_path, meta_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// fixture\n", encoding="utf-8")

    for index in range(20):
        _write(
            config,
            title=f"SampleDomain Mesh Segment 稳定决策 {index}",
            body=f"MaterialGroup primary secondary reducer 材质语义证据 {index}。",
            task_id=f"task-sample_domain-stable-{index}",
            kind="decision",
            system_area="SampleEditor mesh segment compression",
            source_refs=[
                relevant_path.relative_to(repo).as_posix(),
                irrelevant_path.relative_to(repo).as_posix(),
                meta_path.relative_to(repo).as_posix(),
            ],
        )
        _write(
            config,
            title=f"SampleDomain Mesh Segment 验证 {index}",
            body=f"L_SampleArena MaterialGroup reducer 验证证据 {index}。",
            task_id=f"task-sample_domain-validation-{index}",
            kind="validation_result",
            system_area="SampleEditor mesh segment compression",
        )
    meta = _write(
        config,
        title="SampleDomain Mesh Segment Task Brief 自评",
        body="Memory MCP retrieval evaluation MaterialGroup primary secondary reducer。",
        task_id="task-meta-eval",
        kind="validation_result",
        system_area="Memory MCP task brief evaluation",
        source_refs=[meta_path.relative_to(repo).as_posix()],
    )

    result = build_task_brief(
        config,
        task_context={
            "context_token": "ctx-domain-filter",
            "task_id": "task-current",
            "task_run_id": "run-current",
            "user": "alice",
            "workspace_id": str(repo).replace("\\", "/"),
            "branch": None,
        },
        user_goal="继续开发 SampleDomain Mesh Segment MaterialGroup primary secondary reducer",
        active_files=[relevant_path.relative_to(repo).as_posix()],
        brief_mode="deep",
        max_chars=800_000,
        max_tokens=200_000,
        use_llm=False,
        refresh=True,
    )

    source_paths = {item["path"] for item in result["map"]["authority"]["sources"]}
    assert meta["id"] not in result["provenance"]["record_ids"]
    assert result["quality"]["memory_meta_excluded"] >= 1
    assert len(result["map"]["memory"]["stable"]) <= 6
    assert len(result["map"]["memory"]["episodic"]) <= 6
    assert len(result["map"]["continuity"]["recent_tasks"]) <= 6
    assert len(result["map"]["authority"]["sources"]) <= 14
    abstract_ids = {
        item["id"] for item in [*result["map"]["memory"]["stable"], *result["map"]["memory"]["episodic"]]
    }
    lead_ids = {item["id"] for item in result["map"]["continuity"]["memory_leads"]}
    validation_ids = {
        item.get("record_id") for item in result["map"]["authority"]["validation"] if item.get("record_id")
    }
    assert abstract_ids.isdisjoint(lead_ids)
    assert validation_ids.isdisjoint(abstract_ids)
    assert validation_ids.isdisjoint(lead_ids)
    assert all(
        int(item["relevance_band"]) >= 2
        for item in [*result["map"]["memory"]["stable"], *result["map"]["memory"]["episodic"]]
    )
    cutoff = float(result["quality"]["adaptive_relevance_cutoff"])
    assert cutoff >= 0.35
    assert all(
        float(item["query_match_score"]) >= cutoff
        for item in [*result["map"]["memory"]["stable"], *result["map"]["memory"]["episodic"]]
    )
    assert result["quality"]["weak_relevance_fallback_used"] == 0
    assert relevant_path.relative_to(repo).as_posix() in source_paths
    assert irrelevant_path.relative_to(repo).as_posix() not in source_paths
    assert meta_path.relative_to(repo).as_posix() not in source_paths
    assert result["budget_report"]["used_tokens_est"] < 40_000


def test_q1_relevance_retrieval_failure_degrades_to_recent_memory(repo: Path, monkeypatch) -> None:
    config = load_config(repo)
    record = _write(
        config,
        title="Recent fallback decision",
        body="RECENT_FALLBACK_ABSTRACT",
        task_id="task-recent",
        kind="decision",
    )

    def fail_retrieval(*args, **kwargs):
        raise RuntimeError("injected retrieval failure")

    monkeypatch.setattr(memory_task_brief, "memory_retrieve_context", fail_retrieval)
    result = build_task_brief(
        config,
        task_context={
            "context_token": "ctx-q1-fallback",
            "task_id": "task-current",
            "task_run_id": "run-current",
            "user": "alice",
            "workspace_id": str(repo).replace("\\", "/"),
            "branch": None,
        },
        user_goal="recent fallback decision",
        use_llm=False,
        refresh=True,
    )

    assert result["ok"] is True
    assert record["id"] in result["provenance"]["record_ids"]
    assert result["quality"]["retrieval_failed"] == 1


def _reflection_config(repo: Path):
    path = repo / ".ai-memory" / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reflection"] = {
        **raw.get("reflection", {}),
        "enabled": True,
        "min_confidence": 0.8,
        "auto_publish": True,
        "publish_min_confidence": 0.9,
        "publish_repeated_tasks": 2,
        "publish_with_validation_evidence": True,
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(repo)


def _publish_seed(config, *, task_id: str, title: str) -> dict:
    support = _write(
        config,
        title=f"{title} evidence",
        body="Validated durable evidence.",
        task_id=task_id,
        kind="validation_result",
        status="published",
        system_area="memory-mcp",
    )
    return publish_reflection_proposal(
        config,
        proposal={
            "action": "CREATE",
            "kind": "decision",
            "title": title,
            "content_markdown": f"{title} durable content.",
            "confidence": 0.99,
            "importance": 0.9,
            "system_area": "memory-mcp",
            "supporting_record_ids": [support["id"]],
            "validation_evidence_ids": [support["id"]],
            "target_record_ids": [],
        },
        task_id=task_id,
        model="fake-model",
    )


def test_q2_reflection_actions_create_update_merge_supersede_reject(repo: Path) -> None:
    config = _reflection_config(repo)
    old_a = _publish_seed(config, task_id="task-old-a", title="Old memory A")
    old_b = _publish_seed(config, task_id="task-old-b", title="Old memory B")
    old_c = _publish_seed(config, task_id="task-old-c", title="Old memory C")
    old_d = _publish_seed(config, task_id="task-old-d", title="Old memory D")
    validation = _write(
        config,
        title="Evolution validation",
        body="Validated replacement evidence.",
        task_id="task-evolve",
        kind="validation_result",
        status="published",
        system_area="memory-mcp",
    )

    def apply(action: str, targets: list[str], title: str):
        return publish_reflection_proposal(
            config,
            proposal={
                "action": action,
                "kind": "decision",
                "title": title,
                "content_markdown": f"{title} consolidated durable content.",
                "confidence": 0.99,
                "importance": 0.95,
                "system_area": "memory-mcp",
                "supporting_record_ids": [validation["id"]],
                "validation_evidence_ids": [validation["id"]],
                "target_record_ids": targets,
            },
            task_id="task-evolve",
            model="fake-model",
        )

    updated = apply("UPDATE", [old_a["id"]], "Updated memory A")
    merged = apply("MERGE", [old_b["id"], old_c["id"]], "Merged memory B C")
    superseded = apply("SUPERSEDE", [old_d["id"]], "Replacement memory D")
    before_reject = len(iter_parsed_records(config)[0])
    rejected = apply("REJECT", [], "Rejected candidate")
    after_reject = len(iter_parsed_records(config)[0])

    assert updated["ok"] is True and updated["action"] == "UPDATE"
    assert merged["ok"] is True and merged["action"] == "MERGE"
    assert superseded["ok"] is True and superseded["action"] == "SUPERSEDE"
    assert set(_metadata(config, updated["id"])["supersedes"]) == {old_a["id"]}
    assert set(_metadata(config, merged["id"])["supersedes"]) == {old_b["id"], old_c["id"]}
    assert set(_metadata(config, superseded["id"])["supersedes"]) == {old_d["id"]}
    assert rejected["ok"] is True and rejected["rejected"] is True
    assert before_reject == after_reject


def test_q2_validator_requires_known_replaceable_targets() -> None:
    evidence = [
        {
            "id": "validation",
            "record_kind": "validation_result",
            "status": "published",
            "body": "validated",
        }
    ]
    existing = [
        {
            "id": "old",
            "scope": "project_shared",
            "provenance": "background_reflection",
            "replaceable": True,
            "immutable": False,
        }
    ]
    frame = {
        "proposals": [
            {
                "action": "UPDATE",
                "target_record_ids": ["old"],
                "kind": "decision",
                "title": "Update old",
                "content_markdown": "New validated content.",
                "confidence": 0.99,
                "importance": 0.9,
                "supporting_record_ids": ["validation"],
                "validation_evidence_ids": ["validation"],
            },
            {
                "action": "SUPERSEDE",
                "target_record_ids": ["missing"],
                "kind": "decision",
                "title": "Bad target",
                "content_markdown": "Must be rejected.",
                "confidence": 0.99,
                "importance": 0.9,
                "supporting_record_ids": ["validation"],
                "validation_evidence_ids": ["validation"],
            },
        ]
    }

    result = validate_reflection_frame(
        frame,
        evidence=evidence,
        existing_memories=existing,
        max_candidates=8,
        min_confidence=0.8,
    )

    assert [item["action"] for item in result["proposals"]] == ["UPDATE"]
    assert result["proposals"][0]["target_record_ids"] == ["old"]
    assert {item["reason"] for item in result["rejected"]} == {"invalid_action_targets"}


def test_q2_rejects_human_or_already_superseded_targets(repo: Path) -> None:
    config = _reflection_config(repo)
    validation = _write(
        config,
        title="Target safety validation",
        body="Validated target safety evidence.",
        task_id="task-target-safety",
        kind="validation_result",
        status="published",
    )
    human = _write(
        config,
        title="Human authority",
        body="Human-authored project decision.",
        task_id="task-human",
        kind="decision",
        replaceable=False,
        authoritative=True,
    )
    old = _publish_seed(config, task_id="task-safe-old", title="Safe old reflection")

    def proposal(target: str, title: str) -> dict:
        return {
            "action": "UPDATE",
            "target_record_ids": [target],
            "kind": "decision",
            "title": title,
            "content_markdown": f"{title} durable content.",
            "confidence": 0.99,
            "importance": 0.9,
            "supporting_record_ids": [validation["id"]],
            "validation_evidence_ids": [validation["id"]],
        }

    human_result = publish_reflection_proposal(
        config,
        proposal=proposal(human["id"], "Unsafe human update"),
        task_id="task-target-safety",
        model="fake-model",
    )
    safe_proposal = proposal(old["id"], "Safe reflection update")
    first = publish_reflection_proposal(
        config,
        proposal=safe_proposal,
        task_id="task-target-safety",
        model="fake-model",
    )
    retry = publish_reflection_proposal(
        config,
        proposal=safe_proposal,
        task_id="task-target-safety",
        model="fake-model",
    )
    second = publish_reflection_proposal(
        config,
        proposal=proposal(old["id"], "Repeated reflection update"),
        task_id="task-target-safety",
        model="fake-model",
    )

    assert human_result["error"] == "unsafe_target"
    assert first["ok"] is True
    assert retry["ok"] is True and retry["duplicate"] is True
    assert second["error"] == "unsafe_target"


def test_quality_metrics_cover_precision_duplicates_and_forbidden_hits() -> None:
    ranked = ["a", "a-copy", "forbidden", "b", "noise"]
    expected = {"a", "b"}
    duplicate_groups = [{"a", "a-copy"}]

    assert _precision_at(ranked, expected, 5) == 0.4
    assert _duplicate_rate_at(ranked, duplicate_groups, 5) == 0.2
    assert _off_topic_at(ranked, {"forbidden"}, 5) == 0.2
