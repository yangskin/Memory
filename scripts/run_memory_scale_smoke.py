from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path


def _ensure_import_path() -> Path:
    script_path = Path(__file__).resolve()
    memory_root = script_path.parents[1]
    if str(memory_root) not in sys.path:
        sys.path.insert(0, str(memory_root))
    return memory_root


def main() -> int:
    _ensure_import_path()
    from servers.memory_server.memory_config import load_config
    from servers.memory_server.memory_record_index import memory_rebuild_index, memory_search_records
    from servers.memory_server.memory_records import render_record_markdown
    from servers.memory_server.memory_retrieval import memory_retrieve_context

    parser = argparse.ArgumentParser(description="Memory MCP scale smoke benchmark")
    parser.add_argument("--records", type=int, default=5000, help="Number of synthetic records to generate")
    parser.add_argument("--keep", action="store_true", help="Keep the generated temp workspace")
    args = parser.parse_args()

    if args.records <= 0:
        raise SystemExit("--records must be > 0")

    workspace = Path(tempfile.mkdtemp(prefix="memory_scale_smoke_"))
    try:
        (workspace / "memory-bank" / "people" / "bench").mkdir(parents=True)
        (workspace / ".ai-context").mkdir()
        (workspace / ".ai-memory").mkdir()

        started = time.perf_counter()
        for index in range(args.records):
            module_name = f"Module{index % 100}"
            class_name = f"URecordTool{index % 500}"
            component = f"Component{index % 50}"
            metadata = {
                "id": f"mem_scale_{index:05d}",
                "schema_version": "2.0",
                "record_kind": "decision" if index % 3 == 0 else "observation",
                "scope": "project_shared" if index % 2 else "personal",
                "status": "validated" if index % 5 else "candidate",
                "author": "bench",
                "tags": ["workflow"],
                "memory_tier": ["hot", "warm", "cold"][index % 3],
                "cognitive_level": ["shu", "fa", "dao"][index % 3],
                "system_area": "scale_smoke",
                "module_names": [module_name],
                "class_names": [class_name],
                "related_artifact_ids": [component],
                "occurred_at": "2026-04-25T00:00:00+00:00",
            }
            body = (
                f"# {class_name} note {index}\n\n"
                f"Decision: keep deterministic retrieval rule for {component}.\n\n"
                "Next steps:\n"
                f"- Verify {module_name} candidate filtering.\n"
                f"- Check class {class_name} routing.\n"
            )
            path = workspace / "memory-bank" / "people" / "bench" / f"mem_scale_{index:05d}.md"
            path.write_text(render_record_markdown(metadata, body), encoding="utf-8")
        generated_seconds = time.perf_counter() - started

        config = load_config(workspace)
        t0 = time.perf_counter()
        rebuild = memory_rebuild_index(config)
        rebuild_seconds = time.perf_counter() - t0

        t1 = time.perf_counter()
        search = memory_search_records(config, "deterministic retrieval Module2", top_k=10)
        search_seconds = time.perf_counter() - t1

        t2 = time.perf_counter()
        retrieve = memory_retrieve_context(
            config,
            query="deterministic retrieval",
            include_scopes=["project_shared", "personal"],
            include_statuses=["validated", "candidate"],
            system_area="scale_smoke",
            module_names=["Module2"],
            max_tokens=1200,
            max_items=8,
        )
        retrieve_seconds = time.perf_counter() - t2

        result = {
            "records": args.records,
            "workspace": str(workspace) if args.keep else None,
            "generated_seconds": round(generated_seconds, 3),
            "rebuild_ok": rebuild.get("ok"),
            "indexed_records": rebuild.get("indexed_records"),
            "rebuild_seconds": round(rebuild_seconds, 3),
            "search_ok": search.get("ok"),
            "search_hits": len(search.get("results", [])),
            "search_seconds": round(search_seconds, 4),
            "retrieve_ok": retrieve.get("ok"),
            "retrieve_items": len(retrieve.get("context_items", [])),
            "retrieve_seconds": round(retrieve_seconds, 4),
            "retrieve_prefilter": retrieve.get("stats", {}).get("prefilter"),
            "retrieve_budget": retrieve.get("budget_report"),
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if rebuild.get("ok") and search.get("ok") and retrieve.get("ok") else 1
    finally:
        if not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
