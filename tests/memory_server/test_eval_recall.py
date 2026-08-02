"""§15.1-C smoke test: ``scripts/eval_recall`` against a tiny synthetic
corpus seeded with ids matching the curated cases in
``tests/data/recall_set.jsonl``.  We only assert a conservative floor on
the deterministic-hash provider — its purpose is regression detection,
not absolute quality.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import eval_recall  # noqa: E402
from servers.memory_server.memory_config import load_config  # noqa: E402
from servers.memory_server.memory_records import memory_write_record  # noqa: E402


def _enable_embeddings(repo: Path) -> None:
    config_path = repo / ".ai-memory/config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["embeddings"] = {"enabled": True, "provider": "deterministic-hash"}
    config_path.write_text(json.dumps(payload), encoding="utf-8")


def _seed(config) -> dict[str, str]:
    titles = {
        "material": "Material Pipeline PBR roughness metallic",
        "audio": "Audio Bus Submix routing through master and music buses",
        "input": "Input mapping enhanced input keyboard gamepad",
    }
    written: dict[str, str] = {}
    for key, title in titles.items():
        result = memory_write_record(
            config,
            content_markdown=f"# {title}\n\nNotes about {key} workflow in UE5.\n",
            record_kind="note",
            scope="personal",
            status="validated",
            author="alice",
            tags=["high_value"],
        )
        written[key] = result["id"]
    return written


def _write_cases(tmp_path: Path, ids: dict[str, str]) -> Path:
    """Materialise a recall_set.jsonl with the actually-allocated record
    ids so the harness can score against the seeded corpus.
    """
    rows = [
        {"query": "material pipeline pbr roughness metallic",
         "expected_record_ids": [ids["material"]], "tag": "material"},
        {"query": "audio bus submix master music",
         "expected_record_ids": [ids["audio"]], "tag": "audio"},
        {"query": "input mapping enhanced keyboard gamepad",
         "expected_record_ids": [ids["input"]], "tag": "input"},
        {"query": "PBR roughness",
         "expected_record_ids": [ids["material"]], "tag": "material-short"},
        {"query": "submix routing master",
         "expected_record_ids": [ids["audio"]], "tag": "audio-short"},
    ]
    target = tmp_path / "recall_set.jsonl"
    target.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    return target


def test_eval_recall_smoke_deterministic(repo: Path, tmp_path: Path) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    ids = _seed(config)
    cases_path = _write_cases(tmp_path, ids)

    report = eval_recall.evaluate(repo, cases_path, top_k=10)

    assert report["n_queries"] == 5
    # Deterministic-hash provider is purely lexical; require a soft floor
    # so this catches catastrophic regressions without flapping when the
    # hash dimension or feature mix is tweaked.
    assert report["recall@10"] >= 0.4, report


def test_eval_recall_main_prints_summary(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _enable_embeddings(repo)
    config = load_config(repo)
    ids = _seed(config)
    cases_path = _write_cases(tmp_path, ids)

    rc = eval_recall.main(
        ["--config-path", str(repo), "--cases", str(cases_path), "--top-k", "10"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "recall@5=" in out
    assert "recall@10=" in out
    assert "mrr=" in out
    assert "precision@5=" in out
    assert "duplicate@5=" in out
    assert "off_topic@5=" in out
