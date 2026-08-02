from __future__ import annotations

import json
import sys
from io import BytesIO
from io import StringIO
from pathlib import Path

import pytest

from servers.memory_server import cli


def _run(monkeypatch: pytest.MonkeyPatch, repo: Path, *args: str) -> tuple[int, dict]:
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = cli.main(["--root", str(repo), *args])
    monkeypatch.undo()
    payload = json.loads(buf.getvalue())
    return code, payload


def test_cli_health(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "health")
    assert code == 0
    assert payload["ok"] is True


def test_cli_guard(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "guard")
    assert code == 0
    assert payload["ok"] is True


def test_cli_backup(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(
        monkeypatch,
        repo,
        "backup",
        "--path",
        "memory-bank/notes.md",
        "--reason",
        "cli test",
    )
    assert code == 0
    assert payload["ok"] is True
    assert len(payload["backups"]) == 1


def test_cli_rebuild_index(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "rebuild-index")
    assert code == 0
    assert payload["ok"] is True
    assert (repo / ".ai-memory/search.db").exists()


def test_cli_compile_runtime_digest(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "compile", "--target", "runtime_digest")
    assert code == 0
    assert payload["ok"] is True
    assert "path" in payload


def test_cli_invalid_target_returns_exit_1(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "compile", "--target", "bogus_target")
    assert code == 1
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"


def test_cli_archive_unknown_record(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(
        monkeypatch,
        repo,
        "archive",
        "rec_does_not_exist",
        "--reason",
        "cli archive test",
    )
    assert code == 1
    assert payload["ok"] is False


def test_cli_pretty_output(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    buf = StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = cli.main(["--root", str(repo), "--pretty", "health"])
    monkeypatch.undo()
    assert code == 0
    text = buf.getvalue()
    # pretty mode → indent=2 produces multi-line JSON
    assert "\n  " in text


def test_cli_emit_falls_back_when_stdout_encoding_rejects_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    class _LegacyStdout:
        def __init__(self) -> None:
            self.buffer = BytesIO()

        def write(self, text: str) -> int:
            text.encode("gbk")
            return len(text)

        def flush(self) -> None:
            pass

    fake = _LegacyStdout()
    monkeypatch.setattr(sys, "stdout", fake)

    code = cli._emit({"ok": True, "message": "✅ done"}, pretty=False)

    assert code == 0
    assert b"\xe2\x9c\x85 done" in fake.buffer.getvalue()


def test_cli_snapshot_rebuild(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    code, payload = _run(monkeypatch, repo, "snapshot-rebuild")
    assert code == 0
    assert payload["ok"] is True
