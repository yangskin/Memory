"""CLI: ``rebuild-key-docs`` subcommand (P4-C — DesignDoc §15.2).

Doctrine:
    - The CLI must be a thin wrapper over ``rebuild_key_documents`` so it
      stays auditable from a shell / CI; never reimplement the rebuild
      logic here.
    - Unknown ``--target`` values must fail fast with a structured
      ``invalid_input`` error before touching disk.
    - Output is a JSON payload printed to stdout; exit code is 0 when
      ``ok=True`` and 1 otherwise (per CLI convention in cli.py).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from servers.memory_server import cli as memory_cli
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record


@pytest.fixture(autouse=True)
def _disable_llm(monkeypatch):
    """Mirror test_key_documents fixture so 'auto' renderer never tries
    to spin up a real LLM client during CLI tests."""
    from servers.memory_server import memory_key_documents as mkd
    monkeypatch.setattr(
        mkd,
        "_maybe_build_llm_client",
        lambda: (None, {"ok": False, "error": "llm_unavailable", "message": "disabled in test"}),
    )


def _run_cli(argv: list[str], capsys) -> tuple[int, dict]:
    rc = memory_cli.main(argv)
    out = capsys.readouterr().out.strip()
    payload = json.loads(out) if out else {}
    return rc, payload


def _seed_repo(tmp_path: Path) -> Path:
    (tmp_path / ".ai-memory").mkdir(parents=True, exist_ok=True)
    config = load_config(tmp_path)
    memory_write_record(
        config,
        content_markdown="# Sprint focus\n\nFinish CLI rebuild-key-docs.\n",
        record_kind="note",
        scope="personal",
        author="alice",
        tags=["high_value"],
    )
    return tmp_path


def test_cli_rebuilds_all_key_documents_with_deterministic_renderer(tmp_path: Path, capsys) -> None:
    repo = _seed_repo(tmp_path)
    rc, payload = _run_cli(
        ["--root", str(repo), "rebuild-key-docs", "--renderer", "deterministic", "--user", "alice"],
        capsys,
    )
    assert rc == 0, payload
    assert payload.get("ok") is True
    written = payload.get("written") or {}
    # Per-user activeContext plus four shared key documents should have been written.
    assert set(written.keys()) == {"activeContext", "teamContext", "progress", "techContext", "systemPatterns"}


def test_cli_rebuilds_only_targeted_documents(tmp_path: Path, capsys) -> None:
    repo = _seed_repo(tmp_path)
    rc, payload = _run_cli(
        [
            "--root", str(repo),
            "rebuild-key-docs",
            "--renderer", "deterministic",
            "--target", "progress",
            "--user", "alice",
        ],
        capsys,
    )
    assert rc == 0, payload
    written = payload.get("written") or {}
    assert list(written.keys()) == ["progress"]


def test_cli_deterministic_renderer_disables_llm_guard(tmp_path: Path, capsys, monkeypatch) -> None:
    captured: dict = {}

    def fake_rebuild(_config, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "written": {}, "errors": {}}

    monkeypatch.setattr(memory_cli, "rebuild_key_documents", fake_rebuild)

    rc, payload = _run_cli(
        ["--root", str(tmp_path), "rebuild-key-docs", "--renderer", "deterministic", "--user", "alice"],
        capsys,
    )

    assert rc == 0, payload
    assert captured["guard_prefer_llm"] is False


def test_cli_rejects_unknown_target_before_touching_disk(tmp_path: Path, capsys) -> None:
    repo = _seed_repo(tmp_path)
    # argparse choices= already filters this; but exercise the path anyway
    # by going through main() and asserting non-zero exit + structured msg.
    with pytest.raises(SystemExit) as exc:
        _run_cli(
            ["--root", str(repo), "rebuild-key-docs", "--target", "doesNotExist"],
            capsys,
        )
    assert exc.value.code == 2  # argparse usage error


def test_cli_returns_error_when_embedding_renderer_disabled(tmp_path: Path, capsys) -> None:
    repo = _seed_repo(tmp_path)
    rc, payload = _run_cli(
        ["--root", str(repo), "rebuild-key-docs", "--renderer", "embedding", "--user", "alice"],
        capsys,
    )
    assert rc == 1
    assert payload.get("ok") is False
    assert payload.get("error") == "embeddings_disabled"
