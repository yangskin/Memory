"""Tests for the gated real-LLM smoke script.

The smoke itself is manual-only.  CI only verifies that the gate prevents
network calls unless ``MEMORY_LLM_SMOKE=1`` is explicitly set.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import llm_smoke  # noqa: E402


def test_llm_smoke_skips_without_env(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.delenv("MEMORY_LLM_SMOKE", raising=False)
    rc = llm_smoke.main(["--config-path", str(tmp_path)])
    assert rc == 2
    assert "MEMORY_LLM_SMOKE=1" in capsys.readouterr().err


def test_llm_smoke_gate_accepts_truthy_values() -> None:
    assert llm_smoke._gate_enabled({"MEMORY_LLM_SMOKE": "1"}) is True
    assert llm_smoke._gate_enabled({"MEMORY_LLM_SMOKE": "true"}) is True
    assert llm_smoke._gate_enabled({"MEMORY_LLM_SMOKE": "0"}) is False
