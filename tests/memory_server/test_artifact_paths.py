"""P1-5: artifact path normalization (v0.6.0 OOTB hardening)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from servers.memory_server.memory_artifact_paths import (
    attach_git_sha,
    get_git_sha,
    normalize_asset_path,
    normalize_asset_paths,
)
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_lineage import memory_link_artifact
from servers.memory_server.memory_records import memory_write_record


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/Game/Foo/Bar", "/Game/Foo/Bar"),
        ("/Game/Foo/Bar.uasset", "/Game/Foo/Bar"),
        ("Content/Foo/Bar.uasset", "/Game/Foo/Bar"),
        ("Content/Foo/Bar", "/Game/Foo/Bar"),
        ("content\\Foo\\Bar.uasset", "/Game/Foo/Bar"),
        ("C:/Project/Content/Foo/Bar.uasset", "/Game/Foo/Bar"),
        ("Content/Maps/Test.umap", "/Game/Maps/Test"),
        ("/Game/Foo", "/Game/Foo"),  # idempotent
        ("", ""),
    ],
)
def test_normalize_asset_path_canonicalizes(raw: str, expected: str) -> None:
    assert normalize_asset_path(raw) == expected


def test_normalize_asset_path_is_idempotent() -> None:
    once = normalize_asset_path("Content/Foo/Bar.uasset")
    twice = normalize_asset_path(once)
    assert once == twice == "/Game/Foo/Bar"


def test_normalize_asset_paths_dedupes() -> None:
    out = normalize_asset_paths(
        [
            "/Game/Foo/Bar",
            "Content/Foo/Bar.uasset",
            "Content/Foo/Bar",
            "/Game/Other",
        ]
    )
    assert out == ["/Game/Foo/Bar", "/Game/Other"]


def test_get_git_sha_returns_none_when_not_a_git_repo(tmp_path: Path) -> None:
    assert get_git_sha(tmp_path) is None


def test_attach_git_sha_no_op_for_non_git(tmp_path: Path) -> None:
    payload: dict = {}
    attach_git_sha(tmp_path, payload)
    assert "git_sha" not in payload


def test_attach_git_sha_when_git_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()

    class FakeCompleted:
        returncode = 0
        stdout = "abcdef0\n"

    def fake_run(*_a, **_kw):
        return FakeCompleted()

    monkeypatch.setattr(subprocess, "run", fake_run)
    payload: dict = {}
    attach_git_sha(tmp_path, payload)
    assert payload["git_sha"] == "abcdef0"


def test_link_artifact_normalizes_paths(tmp_path: Path) -> None:
    (tmp_path / "memory-bank").mkdir()
    (tmp_path / ".ai-memory").mkdir()
    (tmp_path / ".ai-memory" / "config.json").write_text(
        json.dumps({"allowed_roots": ["memory-bank"]}), encoding="utf-8"
    )
    config = load_config(tmp_path)

    write_result = memory_write_record(
        config,
        content_markdown="hello",
        record_kind="note",
        scope="personal",
    )
    assert write_result["ok"] is True
    rid = write_result["id"]

    link_result = memory_link_artifact(
        config,
        rid,
        asset_paths=["Content/Foo/Bar.uasset", "/Game/Foo/Bar"],
        blueprint_paths=["Content/BP/Hero"],
    )
    assert link_result["ok"] is True
    assert link_result["linked_fields"]["asset_paths"] == ["/Game/Foo/Bar"]
    assert link_result["linked_fields"]["blueprint_paths"] == ["/Game/BP/Hero"]
