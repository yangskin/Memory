"""Gated real-LLM smoke test for the Memory MCP LLM runner.

This script is deliberately not a pytest/CI test.  It only runs when the
developer opts in with ``MEMORY_LLM_SMOKE=1`` and has configured a real
OpenAI-compatible key via ``MEMORY_LLM_API_KEY`` / ``DEEPSEEK_API_KEY`` /
``OPENAI_API_KEY`` or ``llm_config.local.json``.

It exercises the three highest-risk capabilities through the unified
``run_llm_capability`` envelope:

* ``distill_summary`` via ``map_reduce_distill``
* ``query_rewrite`` via ``rewrite_query``
* ``snapshot_narrative`` via ``generate_snapshot_narrative``

Output is one JSON line per capability with ``status``, ``latency_ms`` and
token usage.  Exit code 0 means every capability returned ``status=ok``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Allow running from inside MCP/Memory/scripts/ without installing the package.
_HERE = Path(__file__).resolve()
_MEMORY_ROOT = _HERE.parents[1]
if str(_MEMORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_MEMORY_ROOT))

from servers.memory_server.memory_config import load_config  # noqa: E402
from servers.memory_server.memory_llm import (  # noqa: E402
    LLMConfigError,
    load_llm_config,
    make_raw_record,
)
from servers.memory_server.memory_llm_pipeline import map_reduce_distill  # noqa: E402
from servers.memory_server.memory_llm_runner import run_llm_capability  # noqa: E402
from servers.memory_server.memory_query_rewrite import rewrite_query  # noqa: E402
from servers.memory_server.memory_snapshot_narrative import generate_snapshot_narrative  # noqa: E402


def _gate_enabled(env: dict[str, str]) -> bool:
    return env.get("MEMORY_LLM_SMOKE", "").strip().lower() in {"1", "true", "yes", "on"}


def _has_real_key() -> bool:
    try:
        load_llm_config()
    except LLMConfigError as exc:
        print(f"skip: LLM config unavailable: {exc}", file=sys.stderr)
        return False
    return True


def _usage_tokens(meta: dict[str, Any]) -> int:
    usage = meta.get("usage")
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("total_tokens", 0) or 0)


def _run_case(config, capability: str, fn: Callable[[Any, Any], Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result = run_llm_capability(
        config,
        capability,
        fn,
        force_enabled=True,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    payload = {
        "capability": capability,
        "status": result.status,
        "ok": bool(result.ok and result.status == "ok"),
        "latency_ms": elapsed_ms,
        "token_used": _usage_tokens(result.meta),
        "fallback_used": bool(result.fallback_used),
    }
    if result.error:
        payload["error"] = result.error
    return payload


def _distill_case(_client, profile):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    raw = make_raw_record(
        record_id="llm-smoke-raw-001",
        content="Decision: keep the memory plugin deterministic by default; LLM paths stay opt-in.",
        source="scripts/llm_smoke.py",
        captured_at=now,
        author="smoke",
    )
    record = map_reduce_distill(
        _client,
        [raw],
        record_id="llm-smoke-distilled-001",
        distilled_at=now,
        max_tokens=profile.max_tokens,
    )
    return {"record_id": record["id"], "content_chars": len(str(record.get("content") or ""))}


def _query_rewrite_case(_client, profile):
    result = rewrite_query(
        _client,
        "memory plugin RAG recall quality",
        max_variants=3,
        context_hint="Unreal Engine project memory MCP",
        max_tokens=profile.max_tokens,
    )
    if not result.ok:
        raise RuntimeError(result.error or "query rewrite failed")
    return {"variants": result.variants[:3]}


def _snapshot_narrative_case(_client, profile):
    result = generate_snapshot_narrative(
        _client,
        [
            {
                "id": "smoke-record-1",
                "title": "LLM runner unified",
                "record_kind": "decision",
                "content": "All LLM calls should pass through run_llm_capability.",
            },
            {
                "id": "smoke-record-2",
                "title": "RAG preset hashes",
                "record_kind": "procedure",
                "content": "Embedding presets must pin model and tokenizer SHA-256 values.",
            },
        ],
        target="weekly_snapshot",
        label="llm-smoke",
        max_tokens=profile.max_tokens,
    )
    if not result.ok:
        raise RuntimeError(result.error or "snapshot narrative failed")
    return {"narrative_chars": len(result.narrative)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--config-path",
        default=".",
        help="Workspace root containing .ai-memory/config.json (default: current directory)",
    )
    args = parser.parse_args(argv)

    if not _gate_enabled(os.environ):
        print("skip: set MEMORY_LLM_SMOKE=1 to run real LLM smoke", file=sys.stderr)
        return 2
    if not _has_real_key():
        return 2

    config = load_config(Path(args.config_path).resolve())
    cases = [
        ("distill_summary", _distill_case),
        ("query_rewrite", _query_rewrite_case),
        ("snapshot_narrative", _snapshot_narrative_case),
    ]

    failures = 0
    for capability, fn in cases:
        payload = _run_case(config, capability, fn)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if payload["status"] != "ok":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
