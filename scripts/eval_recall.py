"""§15.1-C: recall、precision、重复率与离题率评估工具。

Loads a curated query → expected ``record_id`` set from
``tests/data/recall_set.jsonl`` and reports::

    recall@5  recall@10  MRR  precision@5  duplicate@5  off_topic@5

This stays intentionally tiny so it can run in CI without optional
dependencies (the deterministic-hash provider is a pure-Python lexical
baseline).  Pass ``--provider local-onnx --model-path ...`` to evaluate
the ONNX tier locally; CI keeps the deterministic baseline so the
recall floor cannot silently regress when the LLM/embedding stack is
swapped.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from servers.memory_server.memory_config import load_config  # noqa: E402
from servers.memory_server.memory_embeddings import get_provider  # noqa: E402
from servers.memory_server.memory_vector_search import (  # noqa: E402
    build_vector_index,
    vector_search,
)


@dataclass
class RecallCase:
    query: str
    expected: set[str]
    tag: str = ""
    forbidden: set[str] | None = None
    duplicate_groups: list[set[str]] | None = None


def _load_cases(path: Path) -> list[RecallCase]:
    cases: list[RecallCase] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        row = json.loads(raw)
        cases.append(
            RecallCase(
                query=row["query"],
                expected=set(row.get("expected_record_ids", [])),
                tag=row.get("tag", ""),
                forbidden=set(row.get("forbidden_record_ids", [])),
                duplicate_groups=[
                    {str(value) for value in group if str(value)}
                    for group in row.get("duplicate_record_groups", [])
                    if isinstance(group, list)
                ],
            )
        )
    if not cases:
        raise ValueError(f"no recall cases loaded from {path}")
    return cases


def _hits_to_record_ids(hits: Iterable[dict]) -> list[str]:
    """``vector_search`` returns chunk-level hits; deduplicate to record
    IDs preserving best-first order.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for hit in hits:
        rid = str(hit.get("record_id", ""))
        if rid and rid not in seen_set:
            seen.append(rid)
            seen_set.add(rid)
    return seen


def _recall_at(ranked: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    top = set(ranked[:k])
    return len(top & expected) / len(expected)


def _reciprocal_rank(ranked: list[str], expected: set[str]) -> float:
    for idx, rid in enumerate(ranked, start=1):
        if rid in expected:
            return 1.0 / idx
    return 0.0


def _precision_at(ranked: list[str], expected: set[str], k: int) -> float:
    """返回 top-k 中明确期望记录所占比例。"""

    if k <= 0:
        return 0.0
    return len(set(ranked[:k]).intersection(expected)) / k


def _duplicate_rate_at(ranked: list[str], duplicate_groups: list[set[str]], k: int) -> float:
    """统计 top-k 被同一知识簇的额外副本占用的比例。"""

    if k <= 0:
        return 0.0
    top = ranked[:k]
    duplicate_slots = 0
    for group in duplicate_groups:
        hits = sum(1 for record_id in top if record_id in group)
        duplicate_slots += max(0, hits - 1)
    return min(1.0, duplicate_slots / k)


def _off_topic_at(ranked: list[str], forbidden: set[str], k: int) -> float:
    """统计 top-k 中黄金集明确标记为离题的记录比例。"""

    if k <= 0:
        return 0.0
    return len(set(ranked[:k]).intersection(forbidden)) / k


def evaluate(
    config_path: Path,
    cases_path: Path,
    *,
    top_k: int = 10,
    rebuild_index: bool = True,
) -> dict:
    config = load_config(config_path)
    if rebuild_index:
        build_vector_index(config)
    cases = _load_cases(cases_path)

    provider = None  # let vector_search resolve from config
    metadata = None
    rows = []
    sum_r5 = 0.0
    sum_r10 = 0.0
    sum_mrr = 0.0
    sum_precision = 0.0
    sum_duplicate = 0.0
    sum_off_topic = 0.0
    for case in cases:
        result = vector_search(config, case.query, top_k=top_k, provider=provider)
        if metadata is None and result.get("ok"):
            # Probe metadata from the index by piggy-backing a tiny call.
            from servers.memory_server.memory_vector_search import _resolve_provider

            metadata = _resolve_provider(config).metadata
        ranked = _hits_to_record_ids(result.get("hits", []))
        r5 = _recall_at(ranked, case.expected, 5)
        r10 = _recall_at(ranked, case.expected, top_k)
        mrr = _reciprocal_rank(ranked, case.expected)
        precision = _precision_at(ranked, case.expected, 5)
        duplicate_rate = _duplicate_rate_at(ranked, case.duplicate_groups or [], 5)
        off_topic = _off_topic_at(ranked, case.forbidden or set(), 5)
        sum_r5 += r5
        sum_r10 += r10
        sum_mrr += mrr
        sum_precision += precision
        sum_duplicate += duplicate_rate
        sum_off_topic += off_topic
        rows.append(
            {
                "query": case.query,
                "tag": case.tag,
                "expected": sorted(case.expected),
                "top": ranked[:top_k],
                "recall@5": r5,
                "recall@10": r10,
                "rr": mrr,
                "precision@5": precision,
                "duplicate@5": duplicate_rate,
                "off_topic@5": off_topic,
            }
        )
    n = len(cases)
    return {
        "provider_id": metadata.provider_id if metadata else None,
        "model_hash": metadata.model_hash if metadata else None,
        "n_queries": n,
        "recall@5": sum_r5 / n,
        f"recall@{top_k}": sum_r10 / n,
        "mrr": sum_mrr / n,
        "precision@5": sum_precision / n,
        "duplicate@5": sum_duplicate / n,
        "off_topic@5": sum_off_topic / n,
        "rows": rows,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Memory MCP recall harness (§15.1-C)")
    p.add_argument("--config-path", type=Path, required=True,
                   help="repo root that contains .ai-memory/config.json")
    p.add_argument("--cases", type=Path,
                   default=REPO_ROOT / "tests" / "data" / "recall_set.jsonl",
                   help="JSONL: query/expected_record_ids/forbidden_record_ids/duplicate_record_groups/tag")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--no-rebuild", action="store_true",
                   help="skip build_vector_index (use existing on-disk index)")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of a summary line")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = evaluate(
        args.config_path,
        args.cases,
        top_k=args.top_k,
        rebuild_index=not args.no_rebuild,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"provider={report['provider_id']} "
            f"model_hash={report['model_hash']} "
            f"n={report['n_queries']} "
            f"recall@5={report['recall@5']:.3f} "
            f"recall@{args.top_k}={report[f'recall@{args.top_k}']:.3f} "
            f"mrr={report['mrr']:.3f}"
            f" precision@5={report['precision@5']:.3f}"
            f" duplicate@5={report['duplicate@5']:.3f}"
            f" off_topic@5={report['off_topic@5']:.3f}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
