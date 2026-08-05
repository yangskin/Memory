"""Tests for memory_key_documents (P4-C: rebuildable key documents).

Doctrine (README §0 / DesignDoc §2.0 / DEVLOG 2026-04-27):
    - Key documents include per-user `activeContext/{user}.md` plus shared
      `teamContext.md` / `progress.md` / `techContext.md` / `systemPatterns.md`.
    - Deterministic renderer must produce a complete, no-LLM document.
    - Header MUST carry `<!-- generated_by=memory-mcp renderer=… ... -->`.
    - Existing files without the marker are archived to
      `memory-bank/archive/manual-edits/<doc>-<timestamp>.md` before overwrite.
    - Failure must NOT damage raw records.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_key_documents import (
    KEY_DOCUMENT_KEYS,
    KEY_DOCUMENTS,
    build_generated_header,
    is_generated,
    parse_generated_meta,
    rebuild_key_documents,
    render_deterministic_document,
    select_records_for,
)
from servers.memory_server.memory_key_document_jobs import (
    drain_key_document_rebuild_jobs,
    read_key_document_rebuild_jobs,
)
from servers.memory_server.memory_record_index import memory_rebuild_index, memory_search_records
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.server import _dispatch_tool


@pytest.fixture(autouse=True)
def _disable_llm_by_default(monkeypatch):
    """Make `auto` mode default to deterministic across this whole module
    so we never accidentally call a real LLM provider during CI / local
    runs that happen to have llm_config.local.json configured. Tests that
    need the LLM tier re-monkeypatch ``_maybe_build_llm_client``."""
    from servers.memory_server import memory_key_documents as mkd
    monkeypatch.setattr(
        mkd,
        "_maybe_build_llm_client",
        lambda: (None, {"ok": False, "error": "llm_unavailable", "message": "disabled in test"}),
    )


# ── header utilities ──────────────────────────────────────────────────────


def test_build_generated_header_contains_required_fields() -> None:
    header = build_generated_header(
        renderer="deterministic",
    )
    assert header.startswith("<!--")
    assert "generated_by=memory-mcp" in header
    assert "renderer=deterministic" in header
    assert "source_record_ids" not in header
    assert "generated_at" not in header
    assert "config_hash" not in header
    assert header.rstrip().endswith("-->")


def test_is_generated_detects_marker_first_line() -> None:
    text = build_generated_header(
        renderer="deterministic",
    ) + "\n# Active Context\n"
    assert is_generated(text) is True


def test_is_generated_rejects_arbitrary_html_comment() -> None:
    assert is_generated("<!-- last overwritten by alice -->\n# Active\n") is False
    assert is_generated("# Active\n") is False
    assert is_generated("") is False


def test_parse_generated_meta_round_trips() -> None:
    header = build_generated_header(
        renderer="llm",
    )
    meta = parse_generated_meta(header + "\n# Title\n")
    assert meta is not None
    assert meta["renderer"] == "llm"
    assert "source_record_ids" not in meta
    assert "generated_at" not in meta
    assert "config_hash" not in meta


def test_parse_generated_meta_returns_none_for_unmarked() -> None:
    assert parse_generated_meta("# Plain markdown\n") is None


# ── KEY_DOCUMENTS contract ────────────────────────────────────────────────


def test_key_documents_covers_four_required_targets() -> None:
    assert set(KEY_DOCUMENT_KEYS) == {
        "activeContext",
        "teamContext",
        "progress",
        "techContext",
        "systemPatterns",
    }
    for key in KEY_DOCUMENT_KEYS:
        spec = KEY_DOCUMENTS[key]
        assert spec["rel_path"].startswith("memory-bank/")
        assert spec["title"]
        assert isinstance(spec.get("include_kinds", []), list)


# ── deterministic renderer ───────────────────────────────────────────────


@pytest.fixture()
def populated_repo(tmp_path: Path) -> Path:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Sprint focus\n\nFinish P4-C deterministic renderer.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["high_value"],
    )
    memory_write_record(
        config,
        content_markdown="# Spdlog adopted\n\nDecision to switch logging backend.\n",
        record_kind="decision",
        scope="project_shared",
        author="alice",
        tags=["build"],
    )
    memory_write_record(
        config,
        content_markdown="# Coding pattern: Result type\n\nUse Result for all dispatch.\n",
        record_kind="rule_candidate",
        scope="personal",
        author="alice",
        tags=["workflow"],
    )
    return tmp_path


def test_render_deterministic_document_contains_header_and_records(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    text = render_deterministic_document(
        config,
        doc_key="progress",
        user="alice",
        generated_at="2026-04-27T12:00:00+00:00",
    )
    assert is_generated(text)
    meta = parse_generated_meta(text)
    assert meta is not None
    assert meta["renderer"] == "deterministic"
    # title section present
    assert "# Progress" in text
    # at least one record body shows up (doctrine: no fabrication, content from raw)
    assert "Spdlog adopted" in text
    assert "Sprint focus" not in text


def test_rebuild_skips_write_when_generated_content_unchanged(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    first = rebuild_key_documents(
        config,
        targets=["progress"],
        user="alice",
        renderer="deterministic",
    )
    assert first["ok"] is True
    before = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")

    second = rebuild_key_documents(
        config,
        targets=["progress"],
        user="alice",
        renderer="deterministic",
    )
    assert second["ok"] is True
    assert second["written"]["progress"].get("skipped") is True
    assert second["written"]["progress"].get("skip_reason") == "no_content_change"
    after = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert after == before


def test_rebuild_migrates_legacy_generated_header_when_body_is_unchanged(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    first = rebuild_key_documents(
        config,
        targets=["progress"],
        user="alice",
        renderer="deterministic",
    )
    assert first["ok"] is True

    target = populated_repo / "memory-bank/progress.md"
    current = target.read_text(encoding="utf-8")
    legacy_header = (
        "<!-- generated_by=memory-mcp renderer=deterministic "
        "source_record_ids=[old-record] generated_at=2026-08-05T00:00:00+00:00 "
        "config_hash=abc123 guard_optimized=deterministic -->"
    )
    target.write_text(current.replace(current.splitlines()[0], legacy_header, 1), encoding="utf-8")

    second = rebuild_key_documents(
        config,
        targets=["progress"],
        user="alice",
        renderer="deterministic",
    )

    assert second["ok"] is True
    assert second["written"]["progress"].get("skipped") is not True
    migrated = target.read_text(encoding="utf-8")
    assert migrated.startswith("<!-- generated_by=memory-mcp renderer=deterministic -->")
    assert "source_record_ids" not in migrated
    assert "generated_at=" not in migrated
    assert "config_hash=" not in migrated
    assert "guard_optimized=" not in migrated


def test_team_documents_exclude_private_and_session_records(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    memory_write_record(
        config,
        content_markdown="# Session scratch\n\nDo not settle into team docs.\n",
        record_kind="note",
        scope="session",
        author="alice",
        tags=["high_value"],
    )

    text = render_deterministic_document(
        config,
        doc_key="teamContext",
        user="alice",
        generated_at="2026-04-27T12:00:00+00:00",
    )

    assert "Spdlog adopted" in text
    assert "Sprint focus" not in text
    assert "Session scratch" not in text


def test_render_deterministic_document_uses_record_id_anchors_and_demotes_body_headings(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    first = memory_write_record(
        config,
        content_markdown="## Summary\n\nFirst shared fact.\n\n## Validation\n\nFirst check.",
        record_kind="handoff",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        task_id="task_first",
    )
    second = memory_write_record(
        config,
        content_markdown="## Summary\n\nSecond shared fact.\n\n## Validation\n\nSecond check.",
        record_kind="handoff",
        scope="project_shared",
        author="alice",
        tags=["mcp"],
        task_id="task_second",
    )

    text = render_deterministic_document(
        config,
        doc_key="teamContext",
        user="alice",
        generated_at="2026-04-27T12:00:00+00:00",
    )

    h2_lines = [line for line in text.splitlines() if line.startswith("## ")]
    assert f"## Summary [{first['id']}]" in h2_lines
    assert f"## Summary [{second['id']}]" in h2_lines
    assert len(h2_lines) == len(set(h2_lines))
    assert "\n## Validation\n" not in text
    assert text.count("\n### Validation\n") == 2


def test_active_context_is_strictly_current_user_scoped(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    memory_write_record(
        config,
        content_markdown="# Bob focus\n\nBob-only work must not leak to Alice.\n",
        record_kind="note",
        scope="personal",
        author="bob",
        tags=["high_value"],
    )

    text = render_deterministic_document(
        config,
        doc_key="activeContext",
        user="alice",
        generated_at="2026-04-27T12:00:00+00:00",
    )

    assert "Sprint focus" in text
    assert "Bob focus" not in text
    assert "Spdlog adopted" in text


def test_render_deterministic_no_records_still_produces_valid_doc(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-context").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    text = render_deterministic_document(
        config,
        doc_key="techContext",
        user=None,
        generated_at="2026-04-27T00:00:00+00:00",
    )
    assert is_generated(text)
    assert "# Tech Context" in text
    # explicit "no records" marker so the document never looks half-rendered
    assert "no records" in text.lower() or "empty" in text.lower()


def test_select_records_for_returns_empty_when_corpus_empty(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-context").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    selected = select_records_for(config, doc_key="progress", user=None)
    assert selected == []


def test_active_context_rebuild_can_read_auto_archived_context(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    archive_dir = tmp_path / "memory-bank/archive/activeContext/alice"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / "activeContext-20260509T120000-deadbeef.md"
    archive.write_text(
        "\n".join(
            [
                "# Archived activeContext for `alice`",
                "",
                "<!-- archived-by: memory-mcp active-context auto-archive; source=memory-bank/activeContext/alice.md -->",
                "",
                "# Active",
                "## Current sprint",
                "- archived alpha focus should be recoverable",
                "## Recent decisions",
                "- archived decision survives live compaction",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    text = render_deterministic_document(config, doc_key="activeContext", user="alice")

    assert "Archived Active Context (alice)" in text
    assert "archived alpha focus should be recoverable" in text
    assert "archived decision survives live compaction" in text
    assert "source: `memory-bank/archive/activeContext/alice/activeContext-20260509T120000-deadbeef.md`" in text


# ── rebuild orchestrator ─────────────────────────────────────────────────


def test_rebuild_writes_all_targets_and_includes_generated_header(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=None, user="alice", renderer="deterministic")
    assert result["ok"] is True
    assert set(result["written"].keys()) == set(KEY_DOCUMENT_KEYS)
    for key, info in result["written"].items():
        assert info["ok"] is True
        spec = KEY_DOCUMENTS[key]
        rel_path = str(spec["rel_path"]).replace("{user}", "alice")
        path = populated_repo / rel_path
        assert path.exists(), f"{key} not written"
        text = path.read_text(encoding="utf-8")
        assert is_generated(text), f"{key} missing generated_by header"
    assert (populated_repo / "memory-bank/teamContext.md").is_file()
    assert (populated_repo / "memory-bank/activeContext/alice.md").is_file()
    assert not (populated_repo / "memory-bank/activeContext.md").exists()


def test_rebuild_enforces_guard_budget_without_mutating_raw_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Envelope:
        ok = False
        status = "unavailable"
        error = "disabled"

        def to_dict(self) -> dict:
            return {"ok": False, "status": "unavailable", "error": "disabled"}

    import servers.memory_server.memory_llm_runner as runner

    monkeypatch.setattr(runner, "run_llm_capability", lambda *a, **kw: _Envelope())

    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps(
            {
                "allowed_roots": ["memory-bank", ".ai-context"],
                "guard": {
                    "default_max_tokens": 3000,
                    "targets": [
                        {
                            "path": "memory-bank/progress.md",
                            "max_chars": 700,
                            "policy": "warm_context",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    long_body = "# Huge milestone\n\n" + "\n".join(
        f"- alpha detail {idx}: deterministic rebuild should not keep all verbose history"
        for idx in range(80)
    )
    memory_write_record(
        config,
        content_markdown=long_body,
        record_kind="note",
        scope="project_shared",
        author="alice",
        tags=["high_value"],
    )

    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="deterministic")

    assert result["ok"] is True
    info = result["written"]["progress"]
    assert info["guard_optimization"]["optimized"] is True
    assert info["guard_optimization"]["method"] == "deterministic"
    rendered = (tmp_path / "memory-bank" / "progress.md").read_text(encoding="utf-8")
    assert is_generated(rendered)
    assert len(rendered) <= 700
    assert any(
        "alpha detail 79" in p.read_text(encoding="utf-8", errors="replace")
        for p in (tmp_path / "memory-bank").rglob("*.md")
        if "progress.md" not in p.as_posix()
    )


def test_rebuild_skips_targets_filter(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="deterministic")
    assert result["ok"] is True
    assert set(result["written"].keys()) == {"progress"}
    other = populated_repo / "memory-bank/techContext.md"
    assert not other.exists() or not is_generated(other.read_text(encoding="utf-8"))


def test_rebuild_archives_pre_existing_manual_edits(populated_repo: Path) -> None:
    target = populated_repo / "memory-bank/progress.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Hand-written progress\n\nlegacy content\n", encoding="utf-8")

    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="deterministic")
    assert result["ok"] is True

    archive_dir = populated_repo / "memory-bank/archive/manual-edits"
    assert archive_dir.exists()
    archived = list(archive_dir.glob("progress-*.md"))
    assert archived, "manual-edit must be archived before overwrite"
    archived_text = archived[0].read_text(encoding="utf-8")
    assert "Hand-written progress" in archived_text
    assert "legacy content" in archived_text

    new_text = target.read_text(encoding="utf-8")
    assert is_generated(new_text)
    assert "Hand-written progress" not in new_text


def test_rebuild_does_not_archive_when_already_generated(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    rebuild_key_documents(config, targets=["progress"], user="alice", renderer="deterministic")
    archive_dir = populated_repo / "memory-bank/archive/manual-edits"
    before = list(archive_dir.glob("progress-*.md")) if archive_dir.exists() else []
    rebuild_key_documents(config, targets=["progress"], user="alice", renderer="deterministic")
    after = list(archive_dir.glob("progress-*.md")) if archive_dir.exists() else []
    assert len(after) == len(before), "second rebuild must not re-archive a generated file"


def test_rebuild_unknown_target_returns_invalid_input(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["bogus"], user="alice", renderer="deterministic")
    assert result["ok"] is False
    assert result["error"] == "invalid_input"


def test_rebuild_llm_renderer_without_client_returns_llm_unavailable(monkeypatch, populated_repo: Path) -> None:
    """When `renderer='llm'` is forced but no LLM is configured, the call
    must surface ``llm_unavailable`` per-doc rather than silently downgrading.

    In ``renderer='auto'`` mode (covered elsewhere) the orchestrator falls
    back to deterministic instead.
    """
    from servers.memory_server import memory_key_documents as mkd
    monkeypatch.setattr(
        mkd,
        "_maybe_build_llm_client",
        lambda: (None, {"ok": False, "error": "llm_unavailable", "message": "forced for test"}),
    )
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="llm")
    assert result["ok"] is False
    err = result["errors"]["progress"]
    assert err["error"] == "llm_unavailable"


def test_rebuild_embedding_renderer_requires_enabled_flag(populated_repo: Path) -> None:
    """Without ``embeddings.enabled=true`` the embedding tier is hard-disabled.

    Once the flag is on, the orchestrator falls through to the deterministic
    tier when the vector index is missing — see
    ``test_rebuild_embedding_renderer_falls_back_to_deterministic`` below.
    """

    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="embedding")
    assert result["ok"] is False
    assert result["error"] == "embeddings_disabled"


def test_rebuild_key_documents_is_internal_cli_only(populated_repo: Path) -> None:
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["activeContext"], user="alice")
    assert result["ok"] is True
    assert "activeContext" in result["written"]
    assert result["written"]["activeContext"]["path"] == "memory-bank/activeContext/alice.md"


# ── mode gating + LLM tier ───────────────────────────────────────────────


def _set_key_documents_mode(repo: Path, *, mode: str) -> None:
    import json as _json
    cfg_path = repo / ".ai-memory/config.json"
    data = _json.loads(cfg_path.read_text(encoding="utf-8"))
    kd = data.get("key_documents") or {}
    kd["mode"] = mode
    data["key_documents"] = kd
    cfg_path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def test_rebuild_returns_manual_mode_when_disabled(populated_repo: Path) -> None:
    _set_key_documents_mode(populated_repo, mode="manual")
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice")
    assert result["ok"] is False
    assert result["error"] == "key_documents_manual_mode"
    assert result["mode"] == "manual"
    # untouched
    assert not (populated_repo / "memory-bank/progress.md").exists()


def test_rebuild_returns_manual_mode_when_disabled_value(populated_repo: Path) -> None:
    _set_key_documents_mode(populated_repo, mode="disabled")
    config = load_config(populated_repo)
    result = rebuild_key_documents(config, user="alice")
    assert result["ok"] is False
    assert result["error"] == "key_documents_manual_mode"
    assert result["mode"] == "disabled"


def test_rebuild_auto_falls_back_to_deterministic_when_llm_unavailable(monkeypatch, populated_repo: Path) -> None:
    from servers.memory_server import memory_key_documents as mkd
    monkeypatch.setattr(
        mkd,
        "_maybe_build_llm_client",
        lambda: (None, {"ok": False, "error": "llm_unavailable", "message": "forced for test"}),
    )
    config = load_config(populated_repo)
    result = rebuild_key_documents(
        config, targets=["progress"], user="alice", renderer="auto"
    )
    assert result["ok"] is True
    info = result["written"]["progress"]
    assert info["renderer"] == "deterministic"
    assert "llm" in result["renderer_order"]
    assert "deterministic" in result["renderer_order"]


class _StubLLMClient:
    """Minimal LLMClient stand-in for the LLM renderer happy path."""

    class _Cfg:
        model = "stub-model"
        max_input_tokens_per_call = 8000
        max_output_tokens_per_call = 1024

    config = _Cfg()


def test_render_llm_document_uses_map_reduce_distill(monkeypatch, populated_repo: Path) -> None:
    """LLM renderer happy path: header + title + role come from us; body from LLM."""
    from servers.memory_server import memory_key_documents as mkd

    captured: dict[str, object] = {}

    def fake_map_reduce_distill(client, raw_records, **kwargs):
        captured["records"] = raw_records
        captured["kwargs"] = kwargs
        return {"id": kwargs["record_id"], "content": "## Highlights\n- Sprint focus: P4-C\n- Decision: spdlog\n"}

    monkeypatch.setattr(
        "servers.memory_server.memory_llm_pipeline.map_reduce_distill",
        fake_map_reduce_distill,
    )

    config = load_config(populated_repo)
    text = mkd.render_llm_document(
        config,
        doc_key="progress",
        user="alice",
        llm_client=_StubLLMClient(),
        generated_at="2026-04-27T00:00:00+00:00",
    )
    assert is_generated(text)


# ── D2 slim-down: tier dispatch table ────────────────────────────────────


def test_tier_invokers_table_covers_three_tiers() -> None:
    """D2: ``_TIER_INVOKERS`` is the single dispatch surface for ``_rebuild_one``;
    keep its key set tied to the documented renderer choices so a missing
    invoker can never silently fall through to deterministic."""
    from servers.memory_server import memory_key_documents as mkd

    assert set(mkd._TIER_INVOKERS.keys()) == {"deterministic", "embedding", "llm"}
    for tier, invoker in mkd._TIER_INVOKERS.items():
        assert callable(invoker), f"{tier} invoker must be callable"


def test_tier_invokers_share_signature(populated_repo: Path) -> None:
    """D2: all three invokers must accept the same positional args and
    return ``(text | None, error_dict | None)``. Embedding is gated by
    ``embeddings_enabled`` so we only assert it returns the unified tuple
    shape via the public ``render_embedding_document`` raising path."""
    from servers.memory_server import memory_key_documents as mkd

    config = load_config(populated_repo)

    text, err = mkd._invoke_deterministic_tier(
        config, "progress", "alice", "2026-04-27T00:00:00+00:00", "memory-bank/progress.md"
    )
    assert err is None
    assert isinstance(text, str) and is_generated(text)

    # LLM tier with no client configured must return (None, error_dict)
    # via the runner's ``unavailable`` envelope rather than raising.
    text2, err2 = mkd._invoke_llm_tier(
        config, "progress", "alice", "2026-04-27T00:00:00+00:00", "memory-bank/progress.md"
    )
    assert text2 is None
    assert isinstance(err2, dict)
    assert err2.get("ok") is False
    assert err2.get("error") in {"llm_unavailable", "llm_disabled", "render_failed"}
    assert "envelope" in err2


def test_rebuild_one_routes_through_dispatch_table(monkeypatch, populated_repo: Path) -> None:
    """D2: ``_rebuild_one`` must look up its invoker via ``_TIER_INVOKERS``
    rather than hard-coded if/elif. Override the table and verify the
    override is actually called."""
    from servers.memory_server import memory_key_documents as mkd

    config = load_config(populated_repo)
    calls: list[str] = []

    def _fake_invoker(cfg, doc_key, user, generated_at, rel_path):
        calls.append(doc_key)
        return (
            mkd.build_generated_header(
                renderer="deterministic",
            )
            + "\n# Faked\n",
            None,
        )

    monkeypatch.setitem(mkd._TIER_INVOKERS, "deterministic", _fake_invoker)
    result = mkd._rebuild_one(
        config,
        doc_key="progress",
        user="alice",
        request_id="test-req",
        tier="deterministic",
    )
    assert result.get("ok") is True
    assert result.get("renderer") == "deterministic"
    assert calls == ["progress"]
    written = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "Faked" in written



def test_rebuild_auto_uses_llm_when_client_available(monkeypatch, populated_repo: Path) -> None:
    """When LLM client is available (stub), auto mode prefers it over deterministic."""
    from servers.memory_server import memory_key_documents as mkd

    monkeypatch.setattr(
        mkd, "_maybe_build_llm_client", lambda: (_StubLLMClient(), None)
    )

    def fake_map_reduce_distill(client, raw_records, **kwargs):
        return {"id": kwargs["record_id"], "content": "## LLM body\n- distilled\n"}

    monkeypatch.setattr(
        "servers.memory_server.memory_llm_pipeline.map_reduce_distill",
        fake_map_reduce_distill,
    )

    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="auto")
    assert result["ok"] is True
    info = result["written"]["progress"]
    assert info["renderer"] == "llm"

    text = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    meta = parse_generated_meta(text)
    assert meta is not None
    assert meta["renderer"] == "llm"
    assert "## LLM body" in text


def test_rebuild_auto_falls_back_when_llm_render_raises(monkeypatch, populated_repo: Path) -> None:
    """If the LLM tier raises mid-render, the orchestrator must rebuild via
    the deterministic tier so the file ends up in a consistent state."""
    from servers.memory_server import memory_key_documents as mkd

    monkeypatch.setattr(
        mkd, "_maybe_build_llm_client", lambda: (_StubLLMClient(), None)
    )

    def boom(*args, **kwargs):
        from servers.memory_server.memory_llm import LLMRequestError
        raise LLMRequestError("simulated provider outage")

    monkeypatch.setattr(
        "servers.memory_server.memory_llm_pipeline.map_reduce_distill",
        boom,
    )

    config = load_config(populated_repo)
    result = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="auto")
    assert result["ok"] is True
    info = result["written"]["progress"]
    assert info["renderer"] == "deterministic"
    text = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    meta = parse_generated_meta(text)
    assert meta is not None
    assert meta["renderer"] == "deterministic"


def test_llm_rebuild_appends_incremental_delta_when_document_is_short(
    monkeypatch,
    populated_repo: Path,
) -> None:
    """LLM tier should append incremental updates for short generated docs.

    This reduces rewrite churn in collaborative merges while preserving
    summary updates.
    """
    from servers.memory_server import memory_key_documents as mkd

    monkeypatch.setattr(
        mkd, "_maybe_build_llm_client", lambda: (_StubLLMClient(), None)
    )

    calls = {"n": 0}

    def fake_map_reduce_distill(client, raw_records, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            content = "## LLM body\n- baseline summary"
        else:
            content = "## LLM body\n- baseline summary\n- newly validated change"
        return {"id": kwargs["record_id"], "content": content}

    monkeypatch.setattr(
        "servers.memory_server.memory_llm_pipeline.map_reduce_distill",
        fake_map_reduce_distill,
    )

    config = load_config(populated_repo)
    first = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="llm")
    assert first["ok"] is True
    second = rebuild_key_documents(config, targets=["progress"], user="alice", renderer="llm")
    assert second["ok"] is True

    text = (populated_repo / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "## Incremental Update" in text
    assert "- newly validated change" in text


def test_config_parses_key_documents_section(tmp_path: Path) -> None:
    import json as _json
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-context").mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / ".ai-memory/config.json"
    cfg.write_text(_json.dumps({
        "key_documents": {
            "mode": "manual",
            "renderers": {"prefer_order": ["llm", "deterministic"]},
        }
    }), encoding="utf-8")
    config = load_config(tmp_path)
    assert config.key_documents_mode == "manual"
    assert config.key_documents_prefer_order == ("llm", "deterministic")


def test_config_defaults_when_key_documents_absent(tmp_path: Path) -> None:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory-bank").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".ai-context").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    assert config.key_documents_mode == "auto"
    assert config.key_documents_prefer_order == ("llm", "deterministic")
    assert config.key_documents_auto_rebuild is not None
    assert config.key_documents_auto_rebuild.enabled is True
    assert config.key_documents_auto_rebuild.after_successful_writes == 5
    assert config.key_documents_auto_rebuild.llm_gate == "when_available"
    assert config.key_documents_auto_rebuild.async_enabled is True
    assert config.key_documents_auto_rebuild.guard_prefer_llm is False
    assert config.key_documents_auto_team_settlement is not None
    assert config.key_documents_auto_team_settlement.enabled is True
    assert config.key_documents_auto_team_settlement.llm_gate == "when_available"
    assert config.key_documents_auto_team_settlement.target_scope == "project_shared"


def test_auto_rebuild_after_successful_record_writes(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "after_successful_writes": 2,
                        "renderer": "deterministic",
                        "llm_gate": "off",
                        "targets": ["progress"],
                        "count_operations": ["record"],
                        "async": False,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    first = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# First\n\nFirst fact.\n",
            "record_kind": "decision",
            "scope": "project_shared",
            "author": "alice",
            "tags": ["mcp"],
        },
    )
    second = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Second\n\nSecond fact.\n",
            "record_kind": "decision",
            "scope": "project_shared",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert first["ok"] is True
    assert first["key_documents_auto_rebuild"]["triggered"] is False
    assert first["key_documents_auto_rebuild"]["pending_successful_writes"] == 1
    assert second["ok"] is True
    assert second["key_documents_auto_rebuild"]["triggered"] is True
    assert second["key_documents_auto_rebuild"]["rebuild"]["ok"] is True

    progress = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert progress.startswith("<!-- generated_by=memory-mcp")
    assert "Second" in progress


def test_auto_team_settlement_promotes_personal_decision_before_rebuild(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_team_settlement": {
                        "enabled": True,
                        "llm_gate": "off",
                    },
                    "auto_rebuild": {
                        "enabled": True,
                        "after_successful_writes": 1,
                        "renderer": "deterministic",
                        "targets": ["teamContext", "progress"],
                        "count_operations": ["record"],
                        "llm_gate": "off",
                        "async": False,
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Auto team split\n\nPersonal writes can generate a derived team summary.\n",
            "record_kind": "decision",
            "scope": "personal",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert result["ok"] is True
    settlement = result["auto_team_settlement"]
    assert settlement["promoted"] is True
    assert settlement["source_record_id"] == result["id"]
    promoted_path = tmp_path / settlement["promoted_path"]
    assert promoted_path.is_file()
    promoted_text = promoted_path.read_text(encoding="utf-8")
    assert "scope: project_shared" in promoted_text
    assert f"- {result['id']}" in promoted_text

    assert result["key_documents_auto_rebuild"]["triggered"] is True
    team_context = (tmp_path / "memory-bank/teamContext.md").read_text(encoding="utf-8")
    progress = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "Auto team split" in team_context
    assert "Auto team split" in progress


def test_auto_team_settlement_does_not_fallback_promote_personal_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_team_settlement": {
                        "enabled": True,
                        "llm_gate": "when_available",
                    },
                    "auto_rebuild": {
                        "enabled": False,
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    from servers.memory_server import memory_llm_runner

    def unavailable_run(config, capability, invoke, **kwargs):  # noqa: ANN001, ARG001
        return memory_llm_runner.LLMRunResult(
            ok=False,
            status=memory_llm_runner.STATUS_UNAVAILABLE,
            capability=capability,
            error="disabled in test",
        )

    monkeypatch.setattr(memory_llm_runner, "run_llm_capability", unavailable_run)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Personal note\n\nThis body must remain private when the LLM gate is unavailable.",
            "record_kind": "decision",
            "scope": "personal",
            "author": "alice",
            "tags": ["mcp", "high_value"],
            "task_id": "task_private_fallback",
        },
    )

    assert result["ok"] is True
    settlement = result["auto_team_settlement"]
    assert settlement["promoted"] is False
    assert settlement["reason"] == "personal_scope_requires_llm"
    assert not (tmp_path / "memory-bank/shared").exists()


def test_auto_team_settlement_llm_summary_gets_stable_heading_and_remains_searchable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_team_settlement": {
                        "enabled": True,
                        "llm_gate": "when_available",
                    },
                    "auto_rebuild": {
                        "enabled": False,
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    from servers.memory_server import memory_llm_runner

    def fake_run_llm_capability(config, capability, invoke, **kwargs):  # noqa: ANN001, ARG001
        return memory_llm_runner.LLMRunResult(
            ok=True,
            status=memory_llm_runner.STATUS_OK,
            capability=capability,
            value={
                "promote": True,
                "reason": "test",
                "summary": "## Summary\n- Needle stable team summary.\n\n## Validation\n- Still searchable.",
                "tags": ["mcp", "validation"],
            },
        )

    monkeypatch.setattr(memory_llm_runner, "run_llm_capability", fake_run_llm_capability)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Source Topic\n\nOriginal private note for team settlement.",
            "record_kind": "decision",
            "scope": "personal",
            "author": "alice",
            "tags": ["mcp"],
            "task_id": "task_heading_stability",
        },
    )

    assert result["ok"] is True
    settlement = result["auto_team_settlement"]
    assert settlement["promoted"] is True
    promoted_text = (tmp_path / settlement["promoted_path"]).read_text(encoding="utf-8")
    assert promoted_text.split("---", 2)[2].lstrip().startswith("# Source Topic [")
    assert "## Summary" in promoted_text

    rendered = render_deterministic_document(
        config,
        doc_key="teamContext",
        user="alice",
        generated_at="2026-04-27T12:00:00+00:00",
    )
    h2_lines = [line for line in rendered.splitlines() if line.startswith("## ")]
    assert len(h2_lines) == len(set(h2_lines))
    assert any(line.startswith("## Source Topic [") for line in h2_lines)
    assert "\n## Summary\n" not in rendered
    assert "\n### Summary\n" in rendered
    assert "\n### Validation\n" in rendered

    rebuild = memory_rebuild_index(config)
    assert rebuild["ok"] is True
    search = memory_search_records(config, "Needle stable team summary", top_k=5)
    assert search["ok"] is True
    assert settlement["promoted_record_id"] in {item["id"] for item in search["results"]}


def test_auto_team_settlement_skips_session_records(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_team_settlement": {
                        "enabled": True,
                        "llm_gate": "off",
                    },
                    "auto_rebuild": {
                        "enabled": False,
                    },
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Scratch\n\nTemporary private work.\n",
            "record_kind": "decision",
            "scope": "session",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert result["ok"] is True
    settlement = result["auto_team_settlement"]
    assert settlement["promoted"] is False
    assert settlement["reason"] == "private_or_transient_scope"
    assert not (tmp_path / "memory-bank/shared").exists()


def test_auto_rebuild_checkpoint_phase_triggers_without_threshold(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "after_successful_writes": 5,
                        "renderer": "deterministic",
                        "targets": ["activeContext", "teamContext", "progress", "techContext", "systemPatterns"],
                        "phase_triggers": ["task_done"],
                        "llm_gate": "off",
                        "async": False,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Done\n\nTask completed.\n",
        record_kind="decision",
        scope="project_shared",
        status="published",
        author="alice",
        tags=["mcp"],
    )

    checkpoint = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "checkpoint",
            "task_phase": "task_done",
            "task_id": "task_auto",
            "user": "alice",
        },
    )

    assert checkpoint["ok"] is True
    auto = checkpoint["key_documents_auto_rebuild"]
    assert auto["triggered"] is True
    assert auto["phase"] == "task_done"
    assert set(auto["selected_targets"]) == {"activeContext", "teamContext", "progress", "techContext", "systemPatterns"}
    assert auto["selected_layer"] == "fa"


def test_auto_rebuild_checkpoint_does_not_prefer_llm_guard_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "renderer": "deterministic",
                        "targets": ["activeContext"],
                        "phase_triggers": ["task_done"],
                        "llm_gate": "off",
                        "async": False,
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    seen: list[bool] = []

    from servers.memory_server import memory_key_documents as key_docs

    def fake_optimize_text_for_guard(*args, **kwargs):
        seen.append(bool(kwargs.get("prefer_llm")))
        return kwargs["text"], {"optimized": False, "reason": "test"}

    monkeypatch.setattr(key_docs, "optimize_text_for_guard", fake_optimize_text_for_guard)

    checkpoint = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "checkpoint",
            "task_phase": "task_done",
            "task_id": "task_auto",
            "user": "alice",
        },
    )

    assert checkpoint["ok"] is True
    assert seen == [False]


def test_auto_rebuild_checkpoint_enqueues_async_job_by_default(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "renderer": "deterministic",
                        "targets": ["progress"],
                        "phase_triggers": ["task_done"],
                        "llm_gate": "off",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Async done\n\nQueued rebuild should publish later.\n",
        record_kind="decision",
        scope="project_shared",
        status="published",
        author="alice",
        tags=["mcp"],
    )

    checkpoint = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "checkpoint",
            "task_phase": "task_done",
            "task_id": "task_auto",
            "user": "alice",
        },
    )

    assert checkpoint["ok"] is True
    auto = checkpoint["key_documents_auto_rebuild"]
    assert auto["triggered"] is True
    assert auto["mode"] == "async"
    assert auto["rebuild"]["queued"] is True
    assert not (tmp_path / "memory-bank/progress.md").exists()

    drained = drain_key_document_rebuild_jobs(config, max_jobs=1, worker_id="test")
    assert drained["ok"] is True
    assert drained["processed"] == 1
    progress = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "Async done" in progress


def test_async_rebuild_coalesces_pending_jobs(tmp_path: Path) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "after_successful_writes": 1,
                        "renderer": "deterministic",
                        "targets": ["progress"],
                        "count_operations": ["record"],
                        "llm_gate": "off",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    config = load_config(tmp_path)

    first = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# First async\n\nFirst queued fact.\n",
            "record_kind": "decision",
            "scope": "project_shared",
            "author": "alice",
            "tags": ["mcp"],
        },
    )
    second = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Second async\n\nSecond queued fact.\n",
            "record_kind": "decision",
            "scope": "project_shared",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert first["key_documents_auto_rebuild"]["rebuild"]["queued"] is True
    assert second["key_documents_auto_rebuild"]["rebuild"]["coalesced"] is True
    jobs = read_key_document_rebuild_jobs(config)
    assert len(jobs["queue"]) == 1

    drained = drain_key_document_rebuild_jobs(config, max_jobs=1, worker_id="test")
    assert drained["ok"] is True
    progress = (tmp_path / "memory-bank/progress.md").read_text(encoding="utf-8")
    assert "First async" in progress
    assert "Second async" in progress


def test_async_rebuild_requeues_when_watermark_changes_during_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_config(tmp_path)
    queued = read_key_document_rebuild_jobs(config)
    assert queued["queue"] == []
    from servers.memory_server import memory_key_document_jobs as jobs_mod

    enqueue = jobs_mod.enqueue_key_document_rebuild(
        config,
        targets=["progress"],
        user="alice",
        renderer="deterministic",
        guard_prefer_llm=False,
        phase="task_done",
        layer="fa",
        trigger="test",
    )
    assert enqueue["ok"] is True

    def fake_rebuild(*args, **kwargs):
        memory_write_record(
            config,
            content_markdown="# Concurrent write\n\nA write arrived during rebuild.\n",
            record_kind="decision",
            scope="project_shared",
            status="published",
            author="alice",
            tags=["mcp"],
        )
        return {"ok": True, "written": {"progress": {"ok": True}}, "errors": {}, "request_id": "req-test"}

    monkeypatch.setattr(jobs_mod, "rebuild_key_documents", fake_rebuild)
    drained = drain_key_document_rebuild_jobs(config, max_jobs=1, worker_id="test")

    assert drained["ok"] is True
    assert drained["jobs"][0]["stale_at_publish"] is True
    state = read_key_document_rebuild_jobs(config)
    assert len(state["queue"]) == 1
    next_job = state["jobs"][state["queue"][0]]
    assert next_job["trigger"] == "stale_requeue"


def test_auto_rebuild_llm_gate_can_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_path = tmp_path / ".ai-memory" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "key_documents": {
                    "auto_rebuild": {
                        "enabled": True,
                        "after_successful_writes": 1,
                        "renderer": "deterministic",
                        "targets": ["progress"],
                        "count_operations": ["record"],
                        "llm_gate": "always",
                    }
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    from servers.memory_server import memory_key_documents_autorun as autorun

    monkeypatch.setattr(
        autorun,
        "_llm_gate_decision",
        lambda *args, **kwargs: {
            "used": True,
            "status": "ok",
            "should_rebuild": False,
            "targets": ["progress"],
            "layer": "shu",
            "reason": "no durable content",
        },
    )
    config = load_config(tmp_path)

    result = _dispatch_tool(
        config,
        "memory_write",
        {
            "operation": "record",
            "content_markdown": "# Scratch\n\nTemporary note.\n",
            "record_kind": "note",
            "scope": "personal",
            "author": "alice",
            "tags": ["mcp"],
        },
    )

    assert result["ok"] is True
    auto = result["key_documents_auto_rebuild"]
    assert auto["triggered"] is False
    assert auto["reason"] == "llm_gate_skipped"
    assert auto["gate"]["reason"] == "no durable content"
    assert not (tmp_path / "memory-bank/progress.md").exists()
