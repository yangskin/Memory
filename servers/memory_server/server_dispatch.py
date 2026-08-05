"""Dispatch layer for the memory MCP server.

Extracted from `server.py` (P1-A). Maps `(name, args)` to the right
domain function and returns a result dict. No FastMCP / asyncio entry
points live here.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from .memory_backup import backup_files
from .memory_board import (
    board_post,
    board_query,
    board_reply,
    board_resolve,
    cache_remote_board_items,
    mark_board_resolve_pending,
    mark_board_resolve_synced,
    mark_board_post_pending,
    mark_board_post_synced,
    pending_board_posts,
    pending_board_resolves,
    remote_board_post_id,
)
from .memory_board_client import (
    remote_board_post,
    remote_board_query,
    remote_board_reply,
    remote_board_resolve,
)
from .memory_compactor import compact_memory
from .memory_compiler import memory_compare_snapshots, memory_compile, memory_get_runtime_digest
from .memory_config import MemoryConfig
from .memory_governance import memory_archive_record, memory_publish_candidate, memory_validate_candidate
from .memory_guard import memory_guard_check
from .memory_key_documents import KEY_DOCUMENT_KEYS, rebuild_key_documents
from .memory_lineage import (
    memory_link_artifact,
    memory_list_conflicts,
    memory_record_observation,
    memory_trace_lineage,
)
from .memory_maintenance import memory_delete_record, memory_health_check, memory_migrate_records
from .memory_reader import memory_get
from .memory_record_index import memory_rebuild_index, memory_search_records, memory_update_index
from .memory_records import memory_write_record
from .memory_result import error_result, ok_result
from .memory_retrieval import memory_get_important_memories, memory_get_latest_memories, memory_retrieve_context
from .memory_search import memory_search
from .memory_task_context import (
    apply_task_context,
    attach_task_context,
    begin_or_resolve_task,
    get_task_context,
    mark_task_checkpoint,
    recover_task_context_for_write,
)
from .memory_task_brief import build_task_brief
from .memory_team_settlement import maybe_auto_settle_team_record
from .memory_events import get_current_user
from .memory_identity import canonical_identity
from .memory_users import is_placeholder_user
from .memory_writer import memory_write as memory_write_file

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _none_if_blank(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sync_pending_board_posts(config: MemoryConfig, *, max_items: int = 20) -> dict[str, int]:
    attempted = 0
    synced = 0
    for local_post in pending_board_posts(config, max_items=max_items):
        attempted += 1
        local_post_id = str(local_post.get("post_id") or "")
        local_thread_id = _none_if_blank(local_post.get("thread_id"))
        payload = {
            "post_id": local_post_id,
            "content": str(local_post.get("content") or ""),
            "task_id": _none_if_blank(local_post.get("task_id")),
            "thread_id": None if local_thread_id == local_post_id else remote_board_post_id(config, local_thread_id),
            "references_json": list(local_post.get("references_json") or []),
            "expires_at": _none_if_blank(local_post.get("expires_at")),
            "author_agent_id": _none_if_blank(local_post.get("author_agent_id")),
            "author_agent_instance_id": _none_if_blank(local_post.get("author_agent_instance_id")),
        }
        if str(local_post.get("post_type") or "") == "reply":
            payload["reply_to"] = remote_board_post_id(config, _none_if_blank(local_post.get("reply_to")))
            remote = remote_board_reply(config, payload)
        else:
            payload["post_type"] = str(local_post.get("post_type") or "note")
            remote = remote_board_post(config, payload)
        if not remote.get("ok"):
            continue
        body = remote.get("remote") if isinstance(remote.get("remote"), dict) else {}
        remote_post = body.get("post") if isinstance(body.get("post"), dict) else {}
        mark_board_post_synced(config, local_post_id, remote_post)
        synced += 1

    for local_post in pending_board_resolves(config, max_items=max_items):
        attempted += 1
        local_post_id = str(local_post.get("post_id") or "")
        remote_id = remote_board_post_id(config, local_post_id)
        remote = remote_board_resolve(config, {"post_id": remote_id})
        if not remote.get("ok"):
            continue
        mark_board_resolve_synced(config, local_post_id)
        synced += 1
    return {"attempted": attempted, "synced": synced}


def _schedule_board_sync(config: MemoryConfig) -> None:
    thread = threading.Thread(
        target=_sync_pending_board_posts,
        args=(config,),
        kwargs={"max_items": 20},
        name="memory-board-sync",
        daemon=True,
    )
    thread.start()


def _merge_board_items(
    remote_items: list[dict[str, Any]],
    local_items: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in remote_items:
        key = str(item.get("post_id") or "")
        if key:
            merged[key] = item
    for item in local_items:
        if str(item.get("remote_sync") or "") == "synced":
            continue
        key = str(item.get("post_id") or "")
        if key and key not in merged:
            merged[key] = item
    items = list(merged.values())
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return items[: max(1, min(200, int(max_items or 20)))]


def _board_priority(post_type: str) -> int:
    order = {
        "warning": 0,
        "request": 1,
        "question": 2,
        "handoff": 3,
        "proposal": 4,
        "reply": 5,
        "note": 6,
    }
    return order.get(post_type, 7)


def _load_open_board_items_for_task(
    config: MemoryConfig,
    *,
    task_id: str,
    max_items: int,
    max_tokens: int,
) -> list[dict[str, Any]]:
    payload = {
        "filter": "unresolved",
        "task_id": task_id,
        "max_items": max(1, min(50, max_items * 3)),
    }
    _schedule_board_sync(config)
    remote = remote_board_query(config, payload)
    local = board_query(config, task_id=task_id, filter_mode="unresolved", max_items=max(1, min(50, max_items * 3)))
    if remote.get("ok"):
        body = remote.get("remote") if isinstance(remote.get("remote"), dict) else {}
        remote_items = [dict(item) for item in body.get("items") or [] if isinstance(item, dict)]
        cache_remote_board_items(config, remote_items)
        local_items = [dict(item) for item in local.get("items") or [] if isinstance(item, dict)]
        raw_items = _merge_board_items(remote_items, local_items, max_items=max(1, min(50, max_items * 3)))
    else:
        raw_items = [dict(item) for item in local.get("items") or [] if isinstance(item, dict)]

    raw_items.sort(key=lambda item: (_board_priority(str(item.get("post_type") or "")), str(item.get("created_at") or "")), reverse=False)

    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for item in raw_items:
        if len(selected) >= max_items:
            break
        content = str(item.get("content") or "")
        est = _estimate_tokens(content) + 20
        if used_tokens + est > max_tokens:
            # 超预算优先丢弃普通 note，其他类型尽量保留。
            if str(item.get("post_type") or "") == "note":
                continue
            if selected:
                continue
        used_tokens += est
        selected.append(item)

    return selected


def _attach_key_document_autorun(
    config: MemoryConfig,
    *,
    operation: str,
    result: dict[str, Any],
    phase: str | None = None,
) -> dict[str, Any]:
    """Attach opt-in key-document autorun metadata to successful writes."""
    try:
        from .memory_key_documents_autorun import maybe_auto_rebuild_key_documents

        outcome = maybe_auto_rebuild_key_documents(config, operation=operation, write_result=result, phase=phase)
    except Exception as exc:  # pragma: no cover - autorun must never break primary writes
        outcome = {
            "enabled": True,
            "triggered": False,
            "error": "auto_rebuild_failed",
            "message": str(exc),
        }
    if outcome.get("enabled"):
        result["key_documents_auto_rebuild"] = outcome
    return result


def _build_llm_client(plugin_root=None):
    """Lazily build an LLMClient. Returns (client, error_dict).

    Returns ``(None, error_dict)`` if config is unavailable so callers can
    surface ``llm_unavailable`` without crashing the primary write/read
    path. Importing here keeps `memory_llm` optional at module load time.
    """
    try:
        from .memory_llm import LLMClient, LLMConfigError  # local import: optional dep
    except Exception as exc:  # pragma: no cover — defensive
        return None, error_result("llm_unavailable", f"memory_llm import failed: {exc}")
    try:
        return LLMClient(plugin_root=plugin_root), None
    except LLMConfigError as exc:
        return None, error_result("llm_unavailable", str(exc))
    except Exception as exc:  # pragma: no cover — defensive
        return None, error_result("llm_unavailable", f"failed to build LLMClient: {exc}")


def _llm_normalize_metadata(
    config: MemoryConfig,
    *,
    content_markdown: str,
    requested_tags: list[str] | tuple[str, ...] | None,
    plugin_root=None,
) -> dict[str, Any]:
    """Soft, in-band LLM-assisted metadata normalization.

    Never raises. Returns a structured suggestion dict the caller decides
    whether to apply. Used by both ``memory_write(record)`` (preflight tag
    normalization) and ``memory_read(task_context)`` (suggested_metadata).

    Result keys (always present):
      - ``status``: one of ``ok``, ``llm_unavailable``, ``llm_failed``,
        ``skipped``.
      - ``applied``: ``True`` only when LLM produced a usable classification.
      - ``requested_tags`` / ``accepted_tags`` / ``rejected_tags`` /
        ``final_tags``: tag bookkeeping (rejected = requested - allowed).
      - ``message``: human-readable status detail.

    When ``status == "ok"`` the result additionally carries
    ``suggested_tags``, ``suggested_record_kind``, ``suggested_scope``,
    ``suggested_system_area`` (rejected business words joined with ``.``),
    ``confidence``, ``rationale``, ``model``.
    """
    from .memory_config import DEFAULT_ALLOWED_TAGS
    from .memory_records import ALLOWED_RECORD_KINDS, ALLOWED_SCOPES

    requested = [str(t) for t in (requested_tags or []) if str(t)]
    allowed_tags = sorted(set(config.tag_allowed_tags or DEFAULT_ALLOWED_TAGS))
    allowed_tags_set = set(allowed_tags)
    accepted = sorted(set(requested) & allowed_tags_set)
    rejected = sorted(set(requested) - allowed_tags_set)
    base: dict[str, Any] = {
        "requested_tags": list(requested),
        "accepted_tags": accepted,
        "rejected_tags": rejected,
        "final_tags": list(accepted),
        "applied": False,
    }

    content = (content_markdown or "").strip()
    if not content and not requested:
        return {**base, "status": "skipped", "message": "no content or tags to classify"}
    if not content:
        # classify_record requires non-empty content; synthesize a minimal seed
        # from the requested tags so the LLM has something to work with.
        content = "Tags requested by caller: " + ", ".join(requested)

    client, err = _build_llm_client(plugin_root)
    if client is None:
        return {
            **base,
            "status": "llm_unavailable",
            "message": (err or {}).get("message") or "LLM client unavailable",
        }

    try:
        from . import memory_llm_enhance as enh

        suggestion = enh.classify_record(
            client,
            content=content,
            allowed_kinds=sorted(ALLOWED_RECORD_KINDS),
            allowed_scopes=sorted(ALLOWED_SCOPES),
            allowed_tags=allowed_tags,
            max_tokens=400,
        )
    except Exception as exc:  # noqa: BLE001 — in-band soft failure
        return {**base, "status": "llm_failed", "message": str(exc)}

    suggested_tags = [str(t) for t in (suggestion.get("tags") or []) if str(t) in allowed_tags_set]
    merged_tags = sorted(set(accepted) | set(suggested_tags))
    suggested_system_area = ".".join(rejected) if rejected else None
    return {
        **base,
        "status": "ok",
        "applied": True,
        "final_tags": merged_tags,
        "suggested_tags": suggested_tags,
        "suggested_record_kind": suggestion.get("record_kind"),
        "suggested_scope": suggestion.get("scope"),
        "suggested_system_area": suggested_system_area,
        "confidence": suggestion.get("confidence"),
        "rationale": suggestion.get("rationale"),
        "model": suggestion.get("model"),
        "message": "LLM classification accepted",
    }


def _run_distill_for_write(
    config: MemoryConfig,
    args: dict[str, Any],
    write_result: dict[str, Any],
) -> dict[str, Any]:
    """Distill the just-written record and persist the summary as a 2nd record.

    Failure modes are reported in-band (``{ok: False, error, message}``)
    so the primary write result is never lost. Always opt-in via
    ``distill=True``.

    \u00a715.2-A: routes through :func:`run_llm_capability` so disabled /
    unavailable / timeout / budget paths emit the canonical 7-status
    envelope (no more ad-hoc try/except + ``_build_llm_client``).
    """
    from datetime import datetime, timezone

    raw_id = str(write_result.get("id") or "").strip()
    if not raw_id:
        return error_result("distill_skipped", "raw record missing id; cannot distill")
    raw_path = str(write_result.get("path") or "").strip()
    raw_content = str(args.get("content_markdown") or "")
    if not raw_content.strip():
        return error_result("distill_skipped", "empty raw content; nothing to distill")

    try:
        from .memory_llm import make_raw_record
        from .memory_llm_pipeline import map_reduce_distill
    except Exception as exc:  # pragma: no cover \u2014 defensive
        return error_result("llm_unavailable", f"pipeline import failed: {exc}")
    from .memory_llm_runner import (
        STATUS_BUDGET,
        STATUS_DISABLED,
        STATUS_FAILED,
        STATUS_OK,
        STATUS_TIMEOUT,
        STATUS_UNAVAILABLE,
        run_llm_capability,
    )

    captured_at = datetime.now(timezone.utc).isoformat()
    raw_view = make_raw_record(
        record_id=raw_id,
        content=raw_content,
        source=f"memory_write:{raw_path}" if raw_path else "memory_write",
        captured_at=captured_at,
        author=str(args.get("author") or "system"),
    )

    def _invoke(client, _profile):
        distilled_payload = map_reduce_distill(
            client,
            [raw_view],
            record_id=f"{raw_id}-distilled",
            distilled_at=captured_at,
            user_instruction=args.get("distill_user_instruction"),
            kind="distilled_summary",
            tags=args.get("distill_tags") or args.get("tags"),
            max_tokens=args.get("distill_max_tokens"),
        )
        usage: dict[str, Any] = {}
        snapshot = getattr(client, "usage_snapshot", None)
        if callable(snapshot):
            try:
                usage_snapshot = snapshot()
                if isinstance(usage_snapshot, dict):
                    usage = usage_snapshot
            except Exception:
                pass
        return {"distilled": distilled_payload, "usage": usage}

    def _client_factory(_profile):
        # Honour the legacy ``_build_llm_client`` seam so tests that
        # monkey-patch it (and production callers that pre-built a client)
        # keep working unchanged.
        client, err = _build_llm_client()
        if client is None:
            from .memory_llm import LLMConfigError

            raise LLMConfigError((err or {}).get("message") or "LLM client unavailable")
        return client

    envelope = run_llm_capability(
        config,
        "distill_summary",
        _invoke,
        client_factory=_client_factory,
        # \u00a715.2-A legacy contract: ``distill=True`` is itself the opt-in
        # signal; we don't gate again on the capability flag.
        force_enabled=True,
    )
    if not envelope.ok:
        status_to_code = {
            STATUS_DISABLED: "llm_disabled",
            STATUS_UNAVAILABLE: "llm_unavailable",
            STATUS_TIMEOUT: "distill_timeout",
            STATUS_BUDGET: "distill_budget_exceeded",
            STATUS_FAILED: "distill_failed",
        }
        code = status_to_code.get(envelope.status, "distill_failed")
        out = error_result(code, envelope.error or "distill skipped")
        out["envelope"] = envelope.to_dict()
        return out

    payload = envelope.value if isinstance(envelope.value, dict) else {}
    distilled = payload.get("distilled") or {}
    summary_text = str(distilled.get("content") or "").strip()
    if not summary_text:
        out = error_result("distill_failed", "empty summary text")
        out["envelope"] = envelope.to_dict()
        return out

    persist = memory_write_record(
        config,
        content_markdown=summary_text,
        record_kind="distilled_summary",
        scope="user_private",
        status="distilled",
        author=str(args.get("author") or "system"),
        derived_from_record_ids=[raw_id],
        provenance=str(distilled.get("provenance") or "llm"),
        immutable=bool(distilled.get("immutable", False)),
        authoritative=bool(distilled.get("authoritative", False)),
        replaceable=bool(distilled.get("replaceable", True)),
        model=str(distilled.get("model") or ""),
        distilled_at=str(distilled.get("distilled_at") or captured_at),
        task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
        branch=str(args["branch"]) if args.get("branch") is not None else None,
    )
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return {
        "ok": bool(persist.get("ok")),
        "status": envelope.status,
        "summary": summary_text,
        "distilled_record_id": persist.get("id"),
        "distilled_path": persist.get("path"),
        "model": distilled.get("model"),
        "pipeline": distilled.get("pipeline", {}),
        "usage": usage,
        "persist_result": persist,
        "envelope": envelope.to_dict(),
    }


def _run_recall_summarize(
    config: MemoryConfig,
    args: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """LLM map-reduce summary over already-retrieved records (read-only).

    \u00a715.2-A: routes through :func:`run_llm_capability` so the 7-status
    envelope replaces the previous ad-hoc opt-in + try/except block.
    """
    if not records:
        return error_result("summarize_skipped", "no records to summarize")
    try:
        from .memory_llm_pipeline import summarize_records_for_recall
    except Exception as exc:  # pragma: no cover
        return error_result("llm_unavailable", f"pipeline import failed: {exc}")
    from .memory_llm_runner import (
        STATUS_BUDGET,
        STATUS_DISABLED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_UNAVAILABLE,
        run_llm_capability,
    )

    def _invoke(client, _profile):
        outcome = summarize_records_for_recall(
            client,
            records,
            query=args.get("summary_query") or args.get("query"),
            max_tokens=args.get("summary_max_tokens"),
            max_chars_per_record=int(args.get("summary_max_chars_per_record") or 4000),
        )
        outcome["__client"] = client
        return outcome

    def _client_factory(_profile):
        client, err = _build_llm_client()
        if client is None:
            from .memory_llm import LLMConfigError

            raise LLMConfigError((err or {}).get("message") or "LLM client unavailable")
        return client

    envelope = run_llm_capability(
        config,
        "summarize_recall",
        _invoke,
        client_factory=_client_factory,
        # Legacy: ``summarize=True`` is the explicit opt-in.
        force_enabled=True,
    )
    if not envelope.ok:
        status_to_code = {
            STATUS_DISABLED: "llm_disabled",
            STATUS_UNAVAILABLE: "llm_unavailable",
            STATUS_TIMEOUT: "summarize_timeout",
            STATUS_BUDGET: "summarize_budget_exceeded",
            STATUS_FAILED: "summarize_failed",
        }
        code = status_to_code.get(envelope.status, "summarize_failed")
        out = error_result(code, envelope.error or "summarize skipped")
        envelope_dict = envelope.to_dict()
        envelope_dict.pop("value", None)
        out["envelope"] = envelope_dict
        return out

    outcome = envelope.value if isinstance(envelope.value, dict) else {}
    client = outcome.pop("__client", None)
    outcome["ok"] = True
    outcome["status"] = envelope.status
    envelope_dict = envelope.to_dict()
    envelope_dict.pop("value", None)
    outcome["envelope"] = envelope_dict
    if client is not None:
        snap = getattr(client, "usage_snapshot", None)
        if callable(snap):
            try:
                usage_snap = snap()
                if isinstance(usage_snap, dict):
                    outcome["usage"] = usage_snap
            except Exception:
                pass
    return outcome


def _run_query_rewrite(
    config: MemoryConfig,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run the v0.10.0 query_rewrite capability under the unified runner.

    Returns a dict with ``ok``, ``status``, ``variants`` (always a list),
    plus the runner envelope so the caller can attach diagnostics to the
    final retrieval result.  When the LLM is disabled / unavailable /
    times out, ``variants`` is empty and the caller continues with the
    deterministic FTS recall — this never blocks retrieval.
    """
    from .memory_llm_runner import run_llm_capability

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": True, "status": "skipped", "variants": [], "reason": "empty_query"}

    raw_max = args.get("rewrite_max_variants")
    try:
        max_variants = int(raw_max) if raw_max is not None else 3
    except (TypeError, ValueError):
        max_variants = 3
    max_variants = max(1, min(max_variants, 8))

    context_hint = args.get("rewrite_context_hint")
    if context_hint is not None:
        context_hint = str(context_hint)

    def _invoke(client, _profile):
        from .memory_query_rewrite import rewrite_query

        result = rewrite_query(
            client,
            query,
            max_variants=max_variants,
            context_hint=context_hint,
        )
        if not result.ok:
            # Surface as a runner-side failure so the unified envelope can
            # apply its standard fallback bookkeeping.
            from .memory_llm import LLMRequestError

            raise LLMRequestError(result.error or "query_rewrite failed")
        return result.to_dict()

    envelope = run_llm_capability(
        config,
        "query_rewrite",
        _invoke,
        fallback=lambda: {"ok": True, "variants": [], "fallback": True},
    )
    payload = envelope.value if isinstance(envelope.value, dict) else {}
    variants = payload.get("variants") if isinstance(payload, dict) else []
    if not isinstance(variants, list):
        variants = []
    return {
        "ok": bool(envelope.ok),
        "status": envelope.status,
        "variants": [str(v) for v in variants if isinstance(v, str) and v.strip()],
        "fallback_used": bool(envelope.fallback_used),
        "error": envelope.error,
        "envelope": envelope.to_dict(),
    }


def _check_required(args: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Return error_result if any required key is missing from args, else None.

    Uses `k not in args or args[k] is None` to allow empty strings and zero values.
    """
    missing = [k for k in keys if k not in args or args[k] is None]
    if missing:
        return error_result("invalid_input", f"missing required parameter(s): {', '.join(missing)}")
    return None


def _effective_retrieval_user(config: MemoryConfig, args: dict[str, Any]) -> str | None | dict[str, Any]:
    """Resolve the user used to isolate private recall results.

    Explicit ``user`` wins. Otherwise use the user from ``context_token`` when
    present, then the configured current user. This keeps personal/session
    memories private by default while still allowing shared/project records.
    """
    raw_user = args.get("user")
    raw_user_text = str(raw_user).strip() if raw_user is not None else ""
    user = canonical_identity(raw_user_text) if raw_user_text else ""
    if not user:
        task_ctx = args.get("_task_context")
        if isinstance(task_ctx, dict):
            context_user = str(task_ctx.get("user") or "").strip()
            user = canonical_identity(context_user) if context_user else ""
    if not user:
        user = canonical_identity(get_current_user(config.repo_root))
    if is_placeholder_user(user) and not getattr(config, "mcp_allow_unknown_user", False):
        return error_result(
            "user_not_configured",
            "retrieve_context requires a stable current user or explicit user to isolate private memories.",
        )
    return user or None


def _explicit_task_filter(args: dict[str, Any]) -> str | None:
    """Return a task filter only when the caller explicitly requested one."""
    if "task_id" not in args:
        return None
    value = args.get("task_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_MEMORY_HEADER_COMMENT_RE = re.compile(
    r"\A\s*<!--(?:(?!-->).)*(?:generated_by=memory-mcp|migrated-from-shared).*?-->\s*",
    re.DOTALL,
)
_HEAVY_VECTOR_KEYS = {
    "embedding",
    "embeddings",
    "embedding_vector",
    "vector",
    "vectors",
    "vector_data",
    "vector_values",
    "query_vector",
    "document_vector",
    "raw_embedding",
}


def _strip_generated_header(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    stripped = text
    while True:
        next_text = _MEMORY_HEADER_COMMENT_RE.sub("", stripped, count=1)
        if next_text == stripped:
            return stripped
        stripped = next_text


def _prune_heavy_payload(value: Any, _seen: set[int] | None = None) -> Any:
    """Drop vector-like payloads defensively before returning MCP context."""
    if _seen is None:
        _seen = set()
    if isinstance(value, dict):
        marker = id(value)
        if marker in _seen:
            return None
        _seen.add(marker)
        pruned: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in _HEAVY_VECTOR_KEYS:
                continue
            pruned[key] = _prune_heavy_payload(item, _seen)
        _seen.remove(marker)
        return pruned
    if isinstance(value, list):
        marker = id(value)
        if marker in _seen:
            return None
        _seen.add(marker)
        if len(value) > 16 and all(isinstance(item, (int, float)) for item in value):
            _seen.remove(marker)
            return []
        pruned = [_prune_heavy_payload(item, _seen) for item in value]
        _seen.remove(marker)
        return pruned
    return value


def _compact_file_read_result(result: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: result.get(key)
        for key in ("ok", "error", "message", "path", "content", "start_line", "end_line", "truncated")
        if key in result
    }
    if "content" in compact:
        compact["content"] = _strip_generated_header(compact["content"])
    return compact


def _compact_query_rewrite(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        key: value.get(key)
        for key in ("ok", "status", "variants", "fallback_used", "error")
        if key in value
    }


def _compact_memory_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    keep = (
        "id",
        "title",
        "path",
        "record_kind",
        "scope",
        "status",
        "tags",
        "cognitive_level",
        "memory_tier",
        "importance_score",
        "system_area",
        "body",
        "snippet",
        "timestamp",
        "reason_selected",
        "query_match_score",
        "relevance_band",
        "memory_role",
        "query_role",
        "role_alignment",
        "collapsed_best_record_id",
        "collapsed_record_ids",
        "rank",
        "degraded",
    )
    compact = {key: item.get(key) for key in keep if key in item and item.get(key) not in (None, [], {})}
    return _prune_heavy_payload(compact)


def _compact_summary_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    keep = (
        "id",
        "context_item_id",
        "title",
        "path",
        "record_kind",
        "scope",
        "status",
        "cognitive_level",
        "memory_tier",
        "importance_score",
        "system_area",
    )
    return {key: item.get(key) for key in keep if key in item and item.get(key) not in (None, [], {})}


def _compact_search_record_hit(hit: Any) -> Any:
    if not isinstance(hit, dict):
        return hit
    keep = (
        "id",
        "title",
        "path",
        "record_kind",
        "scope",
        "status",
        "tags",
        "system_area",
        "importance_score",
        "snippet",
        "score",
    )
    return _prune_heavy_payload(
        {key: hit.get(key) for key in keep if key in hit and hit.get(key) not in (None, [], {})}
    )


def _compact_summary_result(summary: Any) -> Any:
    if not isinstance(summary, dict):
        return summary
    keep = ("ok", "error", "message", "summary", "chunks", "llm_calls", "status", "usage", "model")
    return _prune_heavy_payload(
        {key: summary.get(key) for key in keep if key in summary and summary.get(key) not in (None, [], {})}
    )


def _compact_read_response(operation: str, result: dict[str, Any], *, include_diagnostics: bool) -> dict[str, Any]:
    """Return the minimal MCP-facing memory context by default.

    Internal functions keep their full diagnostic payloads; this facade trims
    data that helps debugging but does not help the agent continue the task.
    """
    if include_diagnostics or not isinstance(result, dict):
        return _prune_heavy_payload(result)

    if operation == "task_context":
        compact = {
            key: result.get(key)
            for key in (
                "ok",
                "error",
                "message",
                "operation",
                "context_token",
                "status",
                "confidence",
                "matched_by",
                "task_id",
                "task_run_id",
                "user",
                "agent_id",
            )
            if key in result
        }
        if isinstance(result.get("active_context"), dict):
            compact["active_context"] = _compact_file_read_result(result["active_context"])
        if isinstance(result.get("current_task"), dict):
            compact["current_task"] = _compact_file_read_result(result["current_task"])
        if isinstance(result.get("retrieved_context"), dict):
            compact["retrieved_context"] = _compact_read_response(
                "retrieve_context",
                result["retrieved_context"],
                include_diagnostics=False,
            )
        if isinstance(result.get("task_brief"), dict):
            compact["task_brief"] = _compact_read_response(
                "task_brief",
                result["task_brief"],
                include_diagnostics=False,
            )
        if isinstance(result.get("open_board_items"), list):
            compact["open_board_items"] = result["open_board_items"]
        if "suggested_metadata" in result:
            compact["suggested_metadata"] = result["suggested_metadata"]
        for key in ("shared_context", "shared_sync"):
            if key in result:
                compact[key] = result[key]
        return _prune_heavy_payload(compact)

    if operation == "task_brief":
        compact = {
            key: result.get(key)
            for key in (
                "ok",
                "error",
                "message",
                "operation",
                "task_id",
                "user",
                "brief_mode",
                "generation",
                "brief_markdown",
                "budget_report",
                "quality",
                "provenance",
                "cache",
            )
            if key in result
        }
        if include_diagnostics and isinstance(result.get("map"), dict):
            compact["map"] = result["map"]
        if isinstance(result.get("open_board_items"), list):
            compact["open_board_items"] = result["open_board_items"]
        if isinstance(result.get("task_context"), dict):
            compact["task_context"] = dict(result["task_context"])
        return _prune_heavy_payload(compact)

    if operation == "search_records":
        compact = {
            key: result.get(key)
            for key in ("ok", "error", "message", "query")
            if key in result
        }
        compact["results"] = [_compact_search_record_hit(item) for item in result.get("results", [])]
        return compact

    if operation == "important_memories":
        compact = {
            key: result.get(key)
            for key in ("ok", "error", "message", "query")
            if key in result
        }
        compact["important_memories"] = [
            _compact_memory_item(item) for item in result.get("important_memories", [])
        ]
        if "query_rewrite" in result:
            compact["query_rewrite"] = _compact_query_rewrite(result.get("query_rewrite"))
        if isinstance(result.get("task_context"), dict):
            compact["task_context"] = dict(result["task_context"])
        for key in ("task_id", "task_run_id", "user", "author", "agent_id"):
            if key in result:
                compact[key] = result.get(key)
        return _prune_heavy_payload(compact)

    if operation == "latest_memories":
        compact = {
            key: result.get(key)
            for key in ("ok", "error", "message")
            if key in result
        }
        compact["latest_memories"] = [_compact_memory_item(item) for item in result.get("latest_memories", [])]
        if isinstance(result.get("task_context"), dict):
            compact["task_context"] = dict(result["task_context"])
        for key in ("task_id", "task_run_id", "user", "author", "agent_id"):
            if key in result:
                compact[key] = result.get(key)
        return _prune_heavy_payload(compact)

    if operation == "retrieve_context":
        compact = {
            key: result.get(key)
            for key in ("ok", "error", "message", "query")
            if key in result
        }
        compact["context_items"] = [_compact_memory_item(item) for item in result.get("context_items", [])]
        for key in ("core_constraints", "relevant_rules", "key_evidence"):
            values = result.get(key)
            if values:
                compact[key] = [_compact_summary_item(item) for item in values]
        for key in ("open_conflicts", "next_steps"):
            if result.get(key):
                compact[key] = _prune_heavy_payload(result.get(key))
        if result.get("summary"):
            compact["summary"] = _compact_summary_result(result.get("summary"))
        if "query_rewrite" in result:
            compact["query_rewrite"] = _compact_query_rewrite(result.get("query_rewrite"))
        if isinstance(result.get("task_context"), dict):
            compact["task_context"] = dict(result["task_context"])
        if "shared_context" in result:
            compact["shared_context"] = _prune_heavy_payload(result.get("shared_context"))
        for key in ("task_id", "task_run_id", "user", "author", "agent_id"):
            if key in result:
                compact[key] = result.get(key)
        return _prune_heavy_payload(compact)

    return _prune_heavy_payload(result)


def _bounded_read_args(operation: str, args: dict[str, Any]) -> dict[str, Any]:
    """Apply server-side defaults and ceilings even when schema validation is bypassed."""
    bounded = dict(args)

    def clamp(name: str, default: int | None, maximum: int) -> None:
        raw = bounded.get(name)
        if raw is None and default is None:
            return
        try:
            value = int(raw if raw is not None else default)
        except (TypeError, ValueError):
            value = int(default or 1)
        bounded[name] = max(1, min(value, maximum))

    clamp("top_k", None, 50)
    clamp("max_items", None, 50)
    clamp("max_tokens", None, 8_000)
    clamp("summary_max_tokens", None, 2_000)
    clamp("summary_max_chars_per_record", None, 8_000)
    if operation in {"get", "runtime_digest"}:
        clamp("max_chars", 12_000, 32_000)
    elif bounded.get("max_chars") is not None:
        clamp("max_chars", None, 32_000)
    if operation == "board":
        clamp("max_items", 20, 50)
    if operation == "project_graph":
        clamp("depth", 1, 2)
        clamp("max_nodes", 50, 200)
        clamp("max_edges", 100, 400)
    return bounded


def _dispatch_memory_read(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    raw_operation = args.get("operation")
    operation = str(raw_operation or ("retrieve_context" if args.get("query") else "task_context"))
    args = _bounded_read_args(operation, args)
    include_diagnostics = bool(args.get("include_diagnostics"))
    if operation == "task_context":
        begin_args = {k: v for k, v in args.items() if k != "operation"}
        task = begin_or_resolve_task(config, **begin_args)
        if not task.get("ok"):
            return task
        token = str(task.get("context_token") or "")
        active = _dispatch_memory_read(
            config,
            {
                "operation": "get",
                "path": "memory-bank/activeContext.md",
                "context_token": token,
                "max_chars": args.get("max_chars"),
            },
        )
        current = _dispatch_memory_read(
            config,
            {
                "operation": "get",
                "path": task.get("current_task_path") or ".ai-context/current-task.md",
                "context_token": token,
                "max_chars": args.get("max_chars"),
            },
        )
        result = ok_result(
            "task context read",
            operation="task_context",
            context_token=token,
            status=task.get("status"),
            confidence=task.get("confidence"),
            matched_by=task.get("matched_by"),
            task_id=task.get("task_id"),
            task_run_id=task.get("task_run_id"),
            user=task.get("user"),
            agent_id=task.get("agent_id"),
            task_context=task,
            active_context=active,
            current_task=current,
        )
        board_items = _load_open_board_items_for_task(
            config,
            task_id=str(args.get("task_id") or task.get("task_id") or ""),
            max_items=int(args.get("board_max_items") or 8),
            max_tokens=int(args.get("board_max_tokens") or 500),
        )
        if board_items:
            result["open_board_items"] = board_items
        if bool(args.get("include_task_brief", True)):
            try:
                result["task_brief"] = build_task_brief(
                    config,
                    task_context=task,
                    current_task=current,
                    active_context=active,
                    user_goal=str(args.get("user_goal") or "") or None,
                    active_files=args.get("active_files"),
                    query=str(args.get("query") or "") or None,
                    preferred_tags=args.get("preferred_tags"),
                    skill_catalog=args.get("brief_skill_catalog"),
                    brief_mode=str(args.get("brief_mode") or "standard"),
                    max_chars=args.get("max_chars"),
                    max_tokens=args.get("max_tokens"),
                    recent_days=int(args.get("brief_recent_days") or 14),
                    use_llm=bool(args.get("brief_use_llm", True)),
                    refresh=bool(args.get("brief_refresh", False)),
                )
            except Exception as exc:  # noqa: BLE001 - 简报增强不得破坏基础 task_context
                logger.exception("task brief generation failed; task_context will continue")
                result["task_brief"] = error_result(
                    "task_brief_failed",
                    f"task context is available but task brief generation failed: {type(exc).__name__}: {exc}",
                )
        if board_items and isinstance(result.get("task_brief"), dict) and result["task_brief"].get("ok"):
            result["task_brief"]["open_board_items"] = board_items
        if args.get("query"):
            result["retrieved_context"] = _dispatch_memory_read(
                config,
                {
                    **args,
                    "operation": "retrieve_context",
                    "context_token": token,
                },
            )
        # §15.2-B opt-in metadata seed for the upcoming write. When the caller
        # passes ``llm_suggest_metadata=True``, synthesize a content seed from
        # ``user_goal`` + ``active_files`` and route through the soft
        # classifier so the agent receives recommended ``record_kind`` / tags
        # before any write attempt. Never blocks; LLM-unavailable returns
        # ``status="llm_unavailable"``.
        if bool(args.get("llm_suggest_metadata")):
            seed_parts: list[str] = []
            ug = str(args.get("user_goal") or "").strip()
            if ug:
                seed_parts.append(f"User goal: {ug}")
            files = args.get("active_files") or []
            if isinstance(files, (list, tuple)) and files:
                seed_parts.append("Active files:\n" + "\n".join(f"- {f}" for f in files))
            seed = "\n\n".join(seed_parts)
            result["suggested_metadata"] = _llm_normalize_metadata(
                config,
                content_markdown=seed,
                requested_tags=None,
                plugin_root=getattr(config, "plugin_root", None),
            )
        try:
            from .memory_shared_context import get_shared_context
            from .memory_sync_store import SyncStore
            result["shared_context"] = get_shared_context(SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db"), config.shared_memory, {**args, "task_id": task.get("task_id")})
        except Exception:
            result["shared_context"] = None
        return _compact_read_response(operation, result, include_diagnostics=include_diagnostics)

    if operation == "shared_context":
        try:
            from .memory_shared_context import get_shared_context
            from .memory_sync_store import SyncStore
            payload = get_shared_context(SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db"), config.shared_memory, args, force_refresh=bool(args.get("force_refresh")), active=True)
            return ok_result("shared context read", operation="shared_context", shared_context=payload)
        except Exception as exc:
            return error_result("shared_context_unavailable", type(exc).__name__)

    if operation == "project_graph":
        try:
            from .memory_shared_context import get_project_graph
            payload = get_project_graph(config.shared_memory, args)
            if payload is None:
                return error_result("shared_context_unavailable", "project graph read is disabled")
            if payload.get("status") == "unavailable":
                return error_result("project_graph_unavailable", str(payload.get("error") or "remote_unavailable"))
            return ok_result("project graph read", operation="project_graph", graph=payload)
        except Exception as exc:
            return error_result("project_graph_unavailable", type(exc).__name__)

    if operation == "get_task_context":
        return get_task_context(config, str(args.get("context_token") or ""))

    if operation == "task_brief":
        args, context_error = apply_task_context(config, args)
        if context_error is not None:
            return context_error
        ctx = args.get("_task_context")
        if not isinstance(ctx, dict):
            return error_result("context_token_required", "task_brief requires context_token")
        current = _dispatch_memory_read(
            config,
            {
                "operation": "get",
                "path": ctx.get("current_task_path") or ".ai-context/current-task.md",
                "context_token": args.get("context_token"),
                "max_chars": args.get("max_chars"),
            },
        )
        active = _dispatch_memory_read(
            config,
            {
                "operation": "get",
                "path": "memory-bank/activeContext.md",
                "context_token": args.get("context_token"),
                "max_chars": args.get("max_chars"),
            },
        )
        try:
            brief = build_task_brief(
                config,
                task_context=ctx,
                current_task=current,
                active_context=active,
                user_goal=str(args.get("user_goal") or "") or None,
                active_files=args.get("active_files"),
                query=str(args.get("query") or "") or None,
                preferred_tags=args.get("preferred_tags"),
                skill_catalog=args.get("brief_skill_catalog"),
                brief_mode=str(args.get("brief_mode") or "standard"),
                max_chars=args.get("max_chars"),
                max_tokens=args.get("max_tokens"),
                recent_days=int(args.get("brief_recent_days") or 14),
                use_llm=bool(args.get("brief_use_llm", True)),
                refresh=bool(args.get("brief_refresh", False)),
            )
        except Exception as exc:  # noqa: BLE001 - 返回结构化错误，不影响其它读能力
            brief = error_result("task_brief_failed", f"{type(exc).__name__}: {exc}")
        return _compact_read_response(operation, attach_task_context(brief, args), include_diagnostics=include_diagnostics)

    if operation in {"retrieve_context", "important_memories", "latest_memories"}:
        result = _dispatch_memory_context(config, {**args, "operation": operation})
        if operation == "retrieve_context" and bool(args.get("include_shared_context")) and isinstance(result, dict):
            try:
                from .memory_shared_context import get_shared_context
                from .memory_sync_store import SyncStore
                result["shared_context"] = get_shared_context(SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db"), config.shared_memory, args, active=True)
            except Exception:
                result["shared_context"] = None
        return _compact_read_response(operation, result, include_diagnostics=include_diagnostics)

    args, context_error = apply_task_context(config, args)
    if context_error is not None:
        return context_error
    if operation == "get":
        err = _check_required(args, "path")
        if err:
            return err
        return memory_get(
            config,
            path=str(args.get("path", "")),
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
            max_chars=args.get("max_chars"),
        )
    if operation == "search":
        err = _check_required(args, "query")
        if err:
            return err
        return memory_search(
            config,
            query=str(args.get("query", "")),
            scopes=args.get("scopes"),
            top_k=args.get("top_k"),
            include_paths=args.get("include_paths"),
            exclude_paths=args.get("exclude_paths"),
        )
    if operation == "search_records":
        err = _check_required(args, "query")
        if err:
            return err
        effective_user = _effective_retrieval_user(config, args)
        if isinstance(effective_user, dict):
            return effective_user
        result = memory_search_records(
            config,
            query=str(args.get("query", "")),
            user=effective_user,
            top_k=args.get("top_k"),
        )
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("user", effective_user)
        return _compact_read_response(operation, result, include_diagnostics=include_diagnostics)
    if operation == "board":
        action = str(args.get("action") or "query").strip().lower()
        if action != "query":
            return error_result("invalid_input", "board read action must be: query")
        remote_payload = {
            "filter": str(args["filter"]) if args.get("filter") is not None else "all",
            "user_id": _none_if_blank(args.get("user_id")),
            "agent_instance_id": _none_if_blank(args.get("agent_instance_id")),
            "task_id": _none_if_blank(args.get("task_id")),
            "status": _none_if_blank(args.get("status")),
            "post_type": _none_if_blank(args.get("post_type")),
            "thread_id": _none_if_blank(args.get("thread_id")),
            "max_items": int(args.get("max_items") or 20),
        }
        _schedule_board_sync(config)
        remote = remote_board_query(config, remote_payload)
        if remote.get("ok"):
            body = remote.get("remote") if isinstance(remote.get("remote"), dict) else {}
            local = board_query(
                config,
                user_id=remote_payload["user_id"],
                agent_instance_id=remote_payload["agent_instance_id"],
                task_id=remote_payload["task_id"],
                status=remote_payload["status"],
                post_type=remote_payload["post_type"],
                thread_id=remote_payload["thread_id"],
                filter_mode=remote_payload["filter"],
                max_items=remote_payload["max_items"],
            )
            remote_items = [dict(item) for item in body.get("items") or [] if isinstance(item, dict)]
            cache_remote_board_items(config, remote_items)
            local_items = [dict(item) for item in local.get("items") or [] if isinstance(item, dict)]
            merged_items = _merge_board_items(
                remote_items,
                local_items,
                max_items=remote_payload["max_items"],
            )
            result = ok_result(
                "board items queried",
                operation="board",
                action="query",
                filter=body.get("filter", remote_payload["filter"]),
                total=len(merged_items),
                items=merged_items,
                board_sync={
                    "remote": True,
                    "fallback": False,
                    "http_status": remote.get("http_status"),
                    "pending_sync_scheduled": True,
                },
            )
            return _compact_read_response(operation, attach_task_context(result, args), include_diagnostics=include_diagnostics)

        result = board_query(
            config,
            user_id=str(args["user_id"]) if args.get("user_id") is not None else None,
            agent_instance_id=(
                str(args["agent_instance_id"]) if args.get("agent_instance_id") is not None else None
            ),
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            status=str(args["status"]) if args.get("status") is not None else None,
            post_type=str(args["post_type"]) if args.get("post_type") is not None else None,
            thread_id=str(args["thread_id"]) if args.get("thread_id") is not None else None,
            filter_mode=str(args["filter"]) if args.get("filter") is not None else None,
            max_items=int(args.get("max_items") or 20),
        )
        if result.get("ok"):
            result["board_sync"] = {
                "remote": False,
                "fallback": True,
                "error": remote.get("error"),
                "message": remote.get("message"),
                "http_status": remote.get("http_status"),
            }
        return _compact_read_response(operation, attach_task_context(result, args), include_diagnostics=include_diagnostics)
    if operation == "runtime_digest":
        return attach_task_context(memory_get_runtime_digest(
            config,
            user=str(args["user"]) if args.get("user") is not None else None,
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            max_chars=args.get("max_chars"),
        ), args)
    return error_result(
        "invalid_input",
        "operation must be one of: task_context, task_brief, get_task_context, get, search, search_records, board, runtime_digest, retrieve_context, important_memories, latest_memories, shared_context, project_graph",
    )


def _enqueue_shared_event(config: MemoryConfig, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok") or not config.shared_memory.enabled:
        return result
    try:
        from .memory_sync_protocol import build_memory_event
        from .memory_sync_store import SyncStore
        canonical: dict[str, Any] = {}
        record_id = str(result.get("id") or "")
        record_path = str(result.get("path") or "")
        if record_id and record_path:
            from .memory_frontmatter import parse_record_pack_entries
            from .memory_reader import memory_get

            persisted = memory_get(config, record_path)
            if persisted.get("ok"):
                for metadata, content in parse_record_pack_entries(str(persisted.get("content") or "")):
                    if str(metadata.get("id") or "") == record_id:
                        canonical = {**metadata, "content_markdown": content}
                        break
        event = build_memory_event(args, result, canonical)
        if event["scope"] not in config.shared_memory.sync_scopes:
            return result
        queued = SyncStore(config.repo_root / ".ai-memory" / "shared-sync.db").enqueue(event["event_id"], event, event["content_hash"])
        if queued:
            from .memory_sync_worker import wake_sync_worker

            wake_sync_worker(config.repo_root)
        result["shared_sync"] = {"enabled": True, "queued": queued}
    except Exception as exc:  # synchronization must never change local write success
        logger.warning("shared event enqueue failed: %s", type(exc).__name__)
        result["shared_sync"] = {"enabled": True, "queued": False}
    return result


def _dispatch_memory_write(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    args, context_error = apply_task_context(config, args)
    if context_error is not None:
        recovered_args, recovery = recover_task_context_for_write(config, args, context_error)
        if recovery is None:
            return context_error
        args = recovered_args
        args["_context_recovery"] = recovery
    if args.get("operation") is not None:
        operation = str(args.get("operation"))
    elif args.get("content_markdown") is not None or args.get("content") is not None:
        operation = "record"
    else:
        operation = "record"

    if operation == "file":
        return error_result(
            "admin_cli_required",
            "memory_write no longer supports file writes through MCP; use the CLI write-file/backup/compact commands for file maintenance.",
        )
    if operation == "checkpoint":
        checkpoint_body = args.get("content_markdown")
        if checkpoint_body is None:
            checkpoint_body = args.get("content")
        phase = str(args.get("task_phase") or args.get("phase") or "").strip()
        if not phase:
            return error_result("invalid_input", "checkpoint operation requires task_phase")
        checkpoint_record: dict[str, Any] | None = None
        checkpoint_warnings: list[dict[str, str]] = []
        if checkpoint_body is not None and str(checkpoint_body).strip():
            default_record_kind = "validation_result" if phase in {"test_failed", "test_passed"} else "handoff"
            checkpoint_record_kind = str(args.get("record_kind") or default_record_kind)
            checkpoint_record = memory_write_record(
                config,
                content_markdown=str(checkpoint_body),
                schema_version=str(args["schema_version"]) if args.get("schema_version") is not None else None,
                record_kind=checkpoint_record_kind,
                scope=str(args.get("scope", "personal")),
                status=str(args["status"]) if args.get("status") is not None else None,
                author=str(args["author"]) if args.get("author") is not None else None,
                tags=args.get("tags"),
                confidence=args.get("confidence"),
                source_refs=args.get("source_refs"),
                task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
                branch=str(args["branch"]) if args.get("branch") is not None else None,
                validated_by=str(args["validated_by"]) if args.get("validated_by") is not None else None,
                tag_schema_version=str(args.get("tag_schema_version", "v1")),
                occurred_at=str(args["occurred_at"]) if args.get("occurred_at") is not None else None,
                valid_from=str(args["valid_from"]) if args.get("valid_from") is not None else None,
                valid_to=str(args["valid_to"]) if args.get("valid_to") is not None else None,
                memory_tier=str(args["memory_tier"]) if args.get("memory_tier") is not None else None,
                cognitive_level=str(args["cognitive_level"]) if args.get("cognitive_level") is not None else None,
                derived_from_record_ids=args.get("derived_from_record_ids"),
                derived_from_snapshot_ids=args.get("derived_from_snapshot_ids"),
                derived_from_revision_ids=args.get("derived_from_revision_ids"),
                supersedes=args.get("supersedes"),
                conflicts_with=args.get("conflicts_with"),
                related_artifact_ids=args.get("related_artifact_ids"),
                importance_score=args.get("importance_score"),
                asset_paths=args.get("asset_paths"),
                map_names=args.get("map_names"),
                plugin_names=args.get("plugin_names"),
                module_names=args.get("module_names"),
                class_names=args.get("class_names"),
                blueprint_paths=args.get("blueprint_paths"),
                system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
            )
            if not checkpoint_record.get("ok") and checkpoint_record.get("error") == "invalid_input":
                checkpoint_record = memory_write_record(
                    config,
                    content_markdown=str(checkpoint_body),
                    record_kind=default_record_kind,
                    scope="personal",
                    author=str(args["author"]) if args.get("author") is not None else None,
                    task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
                    branch=str(args["branch"]) if args.get("branch") is not None else None,
                )
                if checkpoint_record.get("ok"):
                    checkpoint_warnings.append(
                        {
                            "code": "checkpoint_content_metadata_fallback",
                            "message": "checkpoint content had invalid record metadata; the body was saved with safe default record metadata.",
                        }
                    )
            if not checkpoint_record.get("ok"):
                return checkpoint_record
            checkpoint_warnings.append(
                {
                    "code": "checkpoint_content_persisted_as_record",
                    "message": "checkpoint content was saved as a structured record; use operation=record for summaries before sending checkpoint.",
                }
            )
        result = ok_result(
            "checkpoint accepted; content persisted as record" if checkpoint_record else "checkpoint accepted",
            operation="checkpoint",
            task_phase=phase,
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            user=str(args["user"]) if args.get("user") is not None else None,
            author=str(args["author"]) if args.get("author") is not None else None,
        )
        task_ctx = args.get("_task_context")
        if isinstance(task_ctx, dict) and task_ctx.get("context_token"):
            try:
                task_state = mark_task_checkpoint(config, str(task_ctx["context_token"]), phase)
                if task_state.get("ok"):
                    result["task_state"] = task_state
                else:
                    checkpoint_warnings.append(
                        {
                            "code": "task_state_checkpoint_deferred",
                            "message": str(task_state.get("message") or task_state.get("error")),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - 任务索引增强不得改变 checkpoint 主结果
                checkpoint_warnings.append(
                    {
                        "code": "task_state_checkpoint_deferred",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
        if checkpoint_record is not None:
            result["persisted_record"] = checkpoint_record
        if checkpoint_warnings:
            result["warnings"] = checkpoint_warnings
        if isinstance(task_ctx, dict) and task_ctx.get("current_task_path"):
            result["current_task_path"] = task_ctx.get("current_task_path")
        trigger_phases = config.reflection.get("trigger_phases", ["task_done", "test_failed"])
        task_id = str(args.get("task_id") or "").strip()
        if (
            config.reflection.get("enabled", False)
            and isinstance(trigger_phases, list)
            and phase in {str(item) for item in trigger_phases}
            and task_id
        ):
            try:
                from .memory_reflection_jobs import enqueue_project_reflection

                result["background_reflection"] = enqueue_project_reflection(
                    config,
                    task_id=task_id,
                    user=(
                        canonical_identity(str(args.get("user") or args.get("author")))
                        if args.get("user") or args.get("author")
                        else None
                    ),
                    branch=str(args.get("branch") or "") or None,
                    trigger=phase,
                )
            except Exception as exc:  # noqa: BLE001 - background intent must never fail the checkpoint
                result["background_reflection"] = {
                    "ok": False,
                    "queued": False,
                    "error": "background_queue_unavailable",
                    "message": f"{type(exc).__name__}: {exc}",
                }
        if checkpoint_record is not None:
            result = _enqueue_shared_event(config, args, {**checkpoint_record, **result})
        return attach_task_context(_attach_key_document_autorun(config, operation="checkpoint", result=result, phase=phase), args)
    if operation == "record":
        if args.get("content_markdown") is None and args.get("content") is not None:
            args = {**args, "content_markdown": args.get("content")}
        err = _check_required(args, "content_markdown")
        if err:
            return err
        # §15.2-B opt-in LLM-assisted tag normalization. Soft preflight: when
        # the caller passes ``llm_normalize_tags=True`` and the requested tags
        # contain at least one value outside the controlled vocabulary, route
        # through ``classify_record`` to (a) keep the valid tags, (b) merge
        # additional in-vocabulary suggestions, and (c) park rejected business
        # words on ``system_area`` (only when caller didn't set it). The result
        # is always attached to the write envelope under ``metadata_suggestion``
        # so callers can self-correct even when LLM is unavailable.
        llm_metadata_suggestion: dict[str, Any] | None = None
        if bool(args.get("llm_normalize_tags")):
            from .memory_config import DEFAULT_ALLOWED_TAGS as _DEFAULT_TAGS

            requested_tags_raw = list(args.get("tags") or [])
            unknown_tags = sorted(
                set(str(t) for t in requested_tags_raw)
                - set(config.tag_allowed_tags or _DEFAULT_TAGS)
            )
            if unknown_tags:
                llm_metadata_suggestion = _llm_normalize_metadata(
                    config,
                    content_markdown=str(args.get("content_markdown") or ""),
                    requested_tags=requested_tags_raw,
                    plugin_root=getattr(config, "plugin_root", None),
                )
                if llm_metadata_suggestion.get("status") == "ok":
                    new_args = {**args, "tags": list(llm_metadata_suggestion.get("final_tags") or [])}
                    sys_area_suggestion = llm_metadata_suggestion.get("suggested_system_area")
                    if sys_area_suggestion and not new_args.get("system_area"):
                        new_args["system_area"] = sys_area_suggestion
                    if llm_metadata_suggestion.get("suggested_record_kind") and not args.get("record_kind"):
                        new_args["record_kind"] = llm_metadata_suggestion["suggested_record_kind"]
                    args = new_args
        write_result = memory_write_record(
            config,
            content_markdown=str(args.get("content_markdown", "")),
            schema_version=str(args["schema_version"]) if args.get("schema_version") is not None else None,
            record_kind=str(args.get("record_kind", "note")),
            scope=str(args.get("scope", "personal")),
            status=str(args["status"]) if args.get("status") is not None else None,
            author=str(args["author"]) if args.get("author") is not None else None,
            tags=args.get("tags"),
            confidence=args.get("confidence"),
            source_refs=args.get("source_refs"),
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            validated_by=str(args["validated_by"]) if args.get("validated_by") is not None else None,
            classifier_model=str(args["classifier_model"]) if args.get("classifier_model") is not None else None,
            classifier_prompt_version=(
                str(args["classifier_prompt_version"])
                if args.get("classifier_prompt_version") is not None
                else None
            ),
            tag_schema_version=str(args.get("tag_schema_version", "v1")),
            occurred_at=str(args["occurred_at"]) if args.get("occurred_at") is not None else None,
            valid_from=str(args["valid_from"]) if args.get("valid_from") is not None else None,
            valid_to=str(args["valid_to"]) if args.get("valid_to") is not None else None,
            memory_tier=str(args["memory_tier"]) if args.get("memory_tier") is not None else None,
            cognitive_level=str(args["cognitive_level"]) if args.get("cognitive_level") is not None else None,
            derived_from_record_ids=args.get("derived_from_record_ids"),
            derived_from_snapshot_ids=args.get("derived_from_snapshot_ids"),
            derived_from_revision_ids=args.get("derived_from_revision_ids"),
            supersedes=args.get("supersedes"),
            conflicts_with=args.get("conflicts_with"),
            related_artifact_ids=args.get("related_artifact_ids"),
            importance_score=args.get("importance_score"),
            asset_paths=args.get("asset_paths"),
            map_names=args.get("map_names"),
            plugin_names=args.get("plugin_names"),
            module_names=args.get("module_names"),
            class_names=args.get("class_names"),
            blueprint_paths=args.get("blueprint_paths"),
            system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
        )
        task_ctx = args.get("_task_context")
        if isinstance(task_ctx, dict) and task_ctx.get("current_task_path"):
            write_result["current_task_path"] = task_ctx.get("current_task_path")
        if args.get("task_phase") is not None:
            write_result["task_phase"] = str(args.get("task_phase"))
        if bool(args.get("distill")) and write_result.get("ok"):
            distill_outcome = _run_distill_for_write(config, args, write_result)
            write_result["distilled"] = distill_outcome
        if write_result.get("ok"):
            try:
                team_settlement = maybe_auto_settle_team_record(config, args=args, write_result=write_result)
            except Exception as exc:  # pragma: no cover - team settlement must not break primary writes
                team_settlement = {
                    "enabled": True,
                    "promoted": False,
                    "error": "auto_team_settlement_failed",
                    "message": str(exc),
                }
            if team_settlement.get("enabled"):
                write_result["auto_team_settlement"] = team_settlement
        if llm_metadata_suggestion is not None:
            write_result["metadata_suggestion"] = llm_metadata_suggestion
            if (
                write_result.get("ok")
                and llm_metadata_suggestion.get("status") == "ok"
                and llm_metadata_suggestion.get("rejected_tags")
            ):
                warnings_list = write_result.setdefault("warnings", [])
                if not isinstance(warnings_list, list):  # defensive
                    warnings_list = []
                    write_result["warnings"] = warnings_list
                warnings_list.append(
                    {
                        "code": "metadata_normalized_by_llm",
                        "from_tags": list(llm_metadata_suggestion.get("requested_tags") or []),
                        "to_tags": list(llm_metadata_suggestion.get("final_tags") or []),
                        "rejected_tags": list(llm_metadata_suggestion.get("rejected_tags") or []),
                        "system_area": llm_metadata_suggestion.get("suggested_system_area"),
                        "rationale": llm_metadata_suggestion.get("rationale"),
                    }
                )
        if args.get("_context_recovery"):
            write_result["context_recovery"] = dict(args["_context_recovery"])
            write_result.setdefault("task_id", args.get("task_id"))
            write_result.setdefault("user", args.get("user"))
            write_result.setdefault("author", args.get("author"))
            warnings_list = write_result.setdefault("warnings", [])
            if not isinstance(warnings_list, list):  # defensive
                warnings_list = []
                write_result["warnings"] = warnings_list
            warnings_list.append(
                {
                    "code": "context_token_invalid_rebound" if args["_context_recovery"].get("context_rebound") else "context_token_invalid_recovered",
                    "message": "invalid context_token was recovered; inspect context_recovery before treating task attribution as authoritative.",
                }
            )
        return _enqueue_shared_event(config, args, attach_task_context(_attach_key_document_autorun(
            config,
            operation="record",
            result=write_result,
            phase=str(args["task_phase"]) if args.get("task_phase") is not None else None,
        ), args))
    if operation == "board":
        action = str(args.get("action") or "post").strip().lower()
        if action == "post":
            content = args.get("content_markdown")
            if content is None and args.get("content") is not None:
                content = args.get("content")
            err = _check_required({"content_markdown": content}, "content_markdown")
            if err:
                return err
            result = board_post(
                config,
                post_type=str(args.get("post_type") or ""),
                content_markdown=str(content or ""),
                task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
                thread_id=str(args["thread_id"]) if args.get("thread_id") is not None else None,
                references_json=args.get("references_json") if isinstance(args.get("references_json"), list) else None,
                expires_at=str(args["expires_at"]) if args.get("expires_at") is not None else None,
                author_user_id=str(args.get("author") or args.get("user") or "") or None,
                author_agent_id=(
                    str(args.get("agent_id") or (args.get("_task_context") or {}).get("agent_id") or "")
                    or None
                ),
                author_agent_instance_id=(
                    str(args.get("agent_instance_id") or args.get("task_run_id") or "") or None
                ),
            )
            if result.get("ok"):
                local_post = result.get("post") if isinstance(result.get("post"), dict) else {}
                mark_board_post_pending(config, str(local_post.get("post_id") or ""))
                local_post["remote_sync"] = "pending"
                _schedule_board_sync(config)
                result["board_sync"] = {
                    "remote": False,
                    "fallback": False,
                    "queued": True,
                    "non_blocking": True,
                }
            return attach_task_context(result, args)
        if action == "reply":
            content = args.get("content_markdown")
            if content is None and args.get("content") is not None:
                content = args.get("content")
            err = _check_required({"content_markdown": content}, "content_markdown")
            if err:
                return err
            result = board_reply(
                config,
                content_markdown=str(content or ""),
                thread_id=str(args["thread_id"]) if args.get("thread_id") is not None else None,
                reply_to=str(args["reply_to"]) if args.get("reply_to") is not None else None,
                task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
                references_json=args.get("references_json") if isinstance(args.get("references_json"), list) else None,
                expires_at=str(args["expires_at"]) if args.get("expires_at") is not None else None,
                author_user_id=str(args.get("author") or args.get("user") or "") or None,
                author_agent_id=(
                    str(args.get("agent_id") or (args.get("_task_context") or {}).get("agent_id") or "")
                    or None
                ),
                author_agent_instance_id=(
                    str(args.get("agent_instance_id") or args.get("task_run_id") or "") or None
                ),
            )
            if result.get("ok"):
                local_post = result.get("post") if isinstance(result.get("post"), dict) else {}
                mark_board_post_pending(config, str(local_post.get("post_id") or ""))
                local_post["remote_sync"] = "pending"
                _schedule_board_sync(config)
                result["board_sync"] = {
                    "remote": False,
                    "fallback": False,
                    "queued": True,
                    "non_blocking": True,
                }
            return attach_task_context(result, args)
        if action == "resolve":
            err = _check_required(args, "post_id")
            if err:
                return err
            result = board_resolve(
                config,
                post_id=str(args.get("post_id") or ""),
                resolved_by=str(args.get("author") or args.get("user") or "") or None,
            )
            if result.get("ok"):
                mark_board_resolve_pending(config, str(args.get("post_id") or ""))
                local_post = result.get("post") if isinstance(result.get("post"), dict) else {}
                local_post["remote_resolve_sync"] = "pending"
                _schedule_board_sync(config)
                result["board_sync"] = {
                    "remote": False,
                    "fallback": False,
                    "queued": True,
                    "non_blocking": True,
                }
            return attach_task_context(result, args)
        return error_result("invalid_input", "board write action must be one of: post, reply, resolve")
    if operation == "observation":
        if args.get("content_markdown") is None and args.get("content") is not None:
            args = {**args, "content_markdown": args.get("content")}
        err = _check_required(args, "content_markdown")
        if err:
            return err
        result = memory_record_observation(
            config,
            content_markdown=str(args.get("content_markdown", "")),
            author=str(args["author"]) if args.get("author") is not None else None,
            tags=args.get("tags"),
            confidence=args.get("confidence"),
            source_refs=args.get("source_refs"),
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            occurred_at=str(args["occurred_at"]) if args.get("occurred_at") is not None else None,
            memory_tier=str(args["memory_tier"]) if args.get("memory_tier") is not None else "hot",
            cognitive_level=str(args["cognitive_level"]) if args.get("cognitive_level") is not None else "shu",
            related_artifact_ids=args.get("related_artifact_ids"),
            asset_paths=args.get("asset_paths"),
            map_names=args.get("map_names"),
            plugin_names=args.get("plugin_names"),
            module_names=args.get("module_names"),
            class_names=args.get("class_names"),
            blueprint_paths=args.get("blueprint_paths"),
            system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
        )
        task_ctx = args.get("_task_context")
        if isinstance(task_ctx, dict) and task_ctx.get("current_task_path"):
            result["current_task_path"] = task_ctx.get("current_task_path")
        if args.get("task_phase") is not None:
            result["task_phase"] = str(args.get("task_phase"))
        if args.get("_context_recovery"):
            result["context_recovery"] = dict(args["_context_recovery"])
            result.setdefault("task_id", args.get("task_id"))
            result.setdefault("user", args.get("user"))
            result.setdefault("author", args.get("author"))
            result["warnings"] = [
                {
                    "code": "context_token_invalid_rebound" if args["_context_recovery"].get("context_rebound") else "context_token_invalid_recovered",
                    "message": "invalid context_token was recovered; inspect context_recovery before treating task attribution as authoritative.",
                }
            ]
        return _enqueue_shared_event(config, args, attach_task_context(_attach_key_document_autorun(
            config,
            operation="observation",
            result=result,
            phase=str(args["task_phase"]) if args.get("task_phase") is not None else None,
        ), args))
    if operation == "link_artifact":
        return error_result(
            "admin_cli_required",
            "memory_write no longer links artifacts through MCP; use the CLI link-artifact command.",
        )
    return error_result("invalid_input", "operation must be one of: record, observation, checkpoint, board")


def _dispatch_memory_context(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    operation = str(args.get("operation") or "compile")
    if operation in {"begin_task", "resolve_task", "begin_or_resolve_task"}:
        return begin_or_resolve_task(config, **args)
    if operation == "get_task_context":
        return get_task_context(config, str(args.get("context_token") or ""))
    explicit_task_id = _explicit_task_filter(args)
    args, context_error = apply_task_context(config, args)
    if context_error is not None:
        return context_error
    if operation == "compile":
        return attach_task_context(memory_compile(
            config,
            target=str(args.get("target", "runtime_digest")),
            user=str(args["user"]) if args.get("user") is not None else None,
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            include_scopes=args.get("include_scopes"),
            include_statuses=args.get("include_statuses"),
            preferred_tags=args.get("preferred_tags"),
            body_mode=str(args["body_mode"]) if args.get("body_mode") is not None else None,
            as_of=str(args["as_of"]) if args.get("as_of") is not None else None,
            narrative=bool(args.get("narrative", False)),
        ), args)
    if operation == "runtime_digest":
        return attach_task_context(memory_get_runtime_digest(
            config,
            user=str(args["user"]) if args.get("user") is not None else None,
            task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            max_chars=args.get("max_chars"),
        ), args)
    if operation == "trace_lineage":
        err = _check_required(args, "record_id")
        if err:
            return err
        return memory_trace_lineage(config, str(args.get("record_id", "")), max_depth=args.get("max_depth"))
    if operation == "list_conflicts":
        return memory_list_conflicts(config, include_resolved=bool(args.get("include_resolved", False)))
    if operation == "compare_snapshots":
        err = _check_required(args, "path", "other_path")
        if err:
            return err
        return memory_compare_snapshots(
            config,
            path=str(args.get("path", "")),
            other_path=str(args.get("other_path", "")),
        )
    if operation == "retrieve_context":
        effective_user = _effective_retrieval_user(config, args)
        if isinstance(effective_user, dict):
            return effective_user
        rewrite_outcome: dict[str, Any] | None = None
        query_variants: list[str] = []
        if bool(args.get("rewrite_query")):
            rewrite_outcome = _run_query_rewrite(config, args)
            query_variants = rewrite_outcome.get("variants") or []
        result = memory_retrieve_context(
            config,
            query=str(args["query"]) if args.get("query") is not None else None,
            user=effective_user,
            task_id=explicit_task_id,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            include_scopes=args.get("include_scopes"),
            include_statuses=args.get("include_statuses"),
            preferred_tags=args.get("preferred_tags"),
            window_start=str(args["window_start"]) if args.get("window_start") is not None else None,
            window_end=str(args["window_end"]) if args.get("window_end") is not None else None,
            system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
            asset_paths=args.get("asset_paths"),
            map_names=args.get("map_names"),
            plugin_names=args.get("plugin_names"),
            module_names=args.get("module_names"),
            class_names=args.get("class_names"),
            blueprint_paths=args.get("blueprint_paths"),
            top_k=args.get("top_k"),
            max_chars=args.get("max_chars"),
            max_tokens=args.get("max_tokens"),
            max_items=args.get("max_items"),
            query_variants=query_variants or None,
            facet_mode=str(args.get("facet_mode") or "hard"),
            ranking_version=str(args.get("ranking_version") or "v2"),
        )
        if rewrite_outcome is not None and isinstance(result, dict):
            result["query_rewrite"] = rewrite_outcome
        if bool(args.get("summarize")) and result.get("ok"):
            summary_outcome = _run_recall_summarize(
                config,
                args,
                result.get("context_items") or result.get("selected_records") or [],
            )
            result["summary"] = summary_outcome
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("user", effective_user)
        return attach_task_context(result, args)
    if operation == "important_memories":
        effective_user = _effective_retrieval_user(config, args)
        if isinstance(effective_user, dict):
            return effective_user
        rewrite_outcome: dict[str, Any] | None = None
        query_variants: list[str] = []
        if bool(args.get("rewrite_query")):
            rewrite_outcome = _run_query_rewrite(config, args)
            query_variants = rewrite_outcome.get("variants") or []
        result = memory_get_important_memories(
            config,
            query=str(args["query"]) if args.get("query") is not None else None,
            user=effective_user,
            task_id=explicit_task_id,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            include_scopes=args.get("include_scopes"),
            include_statuses=args.get("include_statuses"),
            preferred_tags=args.get("preferred_tags"),
            window_start=str(args["window_start"]) if args.get("window_start") is not None else None,
            window_end=str(args["window_end"]) if args.get("window_end") is not None else None,
            system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
            asset_paths=args.get("asset_paths"),
            map_names=args.get("map_names"),
            plugin_names=args.get("plugin_names"),
            module_names=args.get("module_names"),
            class_names=args.get("class_names"),
            blueprint_paths=args.get("blueprint_paths"),
            top_k=args.get("top_k"),
            max_chars=args.get("max_chars"),
            max_tokens=args.get("max_tokens"),
            max_items=args.get("max_items"),
            query_variants=query_variants or None,
            facet_mode=str(args.get("facet_mode") or "hard"),
            ranking_version=str(args.get("ranking_version") or "v2"),
        )
        if rewrite_outcome is not None and isinstance(result, dict):
            result["query_rewrite"] = rewrite_outcome
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("user", effective_user)
        return attach_task_context(result, args)
    if operation == "latest_memories":
        effective_user = _effective_retrieval_user(config, args)
        if isinstance(effective_user, dict):
            return effective_user
        result = memory_get_latest_memories(
            config,
            user=effective_user,
            task_id=explicit_task_id,
            branch=str(args["branch"]) if args.get("branch") is not None else None,
            include_scopes=args.get("include_scopes"),
            include_statuses=args.get("include_statuses"),
            preferred_tags=args.get("preferred_tags"),
            window_start=str(args["window_start"]) if args.get("window_start") is not None else None,
            window_end=str(args["window_end"]) if args.get("window_end") is not None else None,
            system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
            asset_paths=args.get("asset_paths"),
            map_names=args.get("map_names"),
            plugin_names=args.get("plugin_names"),
            module_names=args.get("module_names"),
            class_names=args.get("class_names"),
            blueprint_paths=args.get("blueprint_paths"),
            top_k=args.get("top_k"),
            max_chars=args.get("max_chars"),
            max_tokens=args.get("max_tokens"),
            max_items=args.get("max_items"),
        )
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("user", effective_user)
        return attach_task_context(result, args)
    if operation == "config_diagnose":
        from .memory_diagnose import config_diagnose
        return config_diagnose(config)
    if operation == "rebuild_key_documents":
        raw_targets = args.get("targets")
        if raw_targets is not None and not isinstance(raw_targets, list):
            return error_result("invalid_input", "targets must be a list of key document names")
        return rebuild_key_documents(
            config,
            targets=[str(t) for t in raw_targets] if raw_targets else None,
            user=str(args["user"]) if args.get("user") is not None else None,
            renderer=str(args.get("renderer") or "deterministic"),
        )
    return error_result(
        "invalid_input",
        "operation must be one of: begin_task, get_task_context, compile, runtime_digest, trace_lineage, list_conflicts, compare_snapshots, retrieve_context, important_memories, latest_memories, config_diagnose, rebuild_key_documents",
    )


# ── memory_enhance dispatch (LLM-backed soft enhancements, opt-in) ─────────


_ENHANCE_OPS = {
    "classify_record",
    "extract_candidates",
    "merge_candidates",
    "generate_skill_candidate",
    "explain_conflict",
    "generate_handoff",
}


def _dispatch_memory_enhance(config: MemoryConfig, args: dict[str, Any]) -> dict[str, Any]:
    op = str(args.get("operation") or "").strip()
    if op not in _ENHANCE_OPS:
        return error_result(
            "invalid_input",
            f"operation must be one of: {', '.join(sorted(_ENHANCE_OPS))}",
        )

    plugin_root = getattr(config, "plugin_root", None)
    client, err = _build_llm_client(plugin_root)
    if err is not None:
        return err

    # Local import to keep top-of-file lean.
    from . import memory_llm_enhance as enh
    from .memory_records import ALLOWED_RECORD_KINDS, ALLOWED_SCOPES, ALLOWED_TAGS

    try:
        if op == "classify_record":
            content = str(args.get("content_markdown") or args.get("content") or "")
            allowed_kinds = args.get("allowed_kinds") or sorted(ALLOWED_RECORD_KINDS)
            allowed_scopes = args.get("allowed_scopes") or sorted(ALLOWED_SCOPES)
            allowed_tags = args.get("allowed_tags") or sorted(ALLOWED_TAGS)
            return enh.classify_record(
                client,
                content=content,
                allowed_kinds=list(allowed_kinds),
                allowed_scopes=list(allowed_scopes),
                allowed_tags=list(allowed_tags),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
            )
        if op == "extract_candidates":
            return enh.extract_candidates(
                client,
                content=str(args.get("content_markdown") or args.get("content") or ""),
                source_record_id=(str(args["source_record_id"]) if args.get("source_record_id") else None),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
            )
        if op == "merge_candidates":
            return enh.merge_candidates(
                client,
                candidates=list(args.get("candidates") or []),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
            )
        if op == "generate_skill_candidate":
            return enh.generate_skill_candidate(
                client,
                records=list(args.get("records") or []),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
                max_chars_per_record=int(args.get("max_chars_per_record") or 4000),
            )
        if op == "explain_conflict":
            return enh.explain_conflict(
                client,
                record_a=dict(args.get("record_a") or {}),
                record_b=dict(args.get("record_b") or {}),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
            )
        if op == "generate_handoff":
            return enh.generate_handoff(
                client,
                records=list(args.get("records") or []),
                task_id=(str(args["task_id"]) if args.get("task_id") else None),
                branch=(str(args["branch"]) if args.get("branch") else None),
                max_tokens=args.get("max_tokens"),
                thinking=args.get("thinking"),
                reasoning_effort=args.get("reasoning_effort"),
                max_chars_per_record=int(args.get("max_chars_per_record") or 4000),
            )
    except Exception as exc:  # noqa: BLE001 — surface as in-band structured error
        return error_result(f"enhance_failed:{op}", str(exc))
    return error_result("invalid_input", f"unhandled enhance operation: {op}")


# ── Public allow-list for the MCP surface ───────────────────────────────
#
# The MCP surface is intentionally exactly two tools; everything else
# routes through the CLI (`python -m servers.memory_server.cli ...`).
# Migration hints map well-known legacy tool names to the closest
# replacement so an agent caller can self-correct from a single error
# response. The map is intentionally exhaustive for the legacy names that
# used to exist on the MCP surface; novel `memory_*` names get a generic
# CLI hint.
ALLOWED_TOOLS: frozenset[str] = frozenset({"memory_read", "memory_write"})

LEGACY_TOOL_MIGRATION_HINTS: dict[str, str] = {
    "memory_get": "memory_read(operation='get', path=...)",
    "memory_search": "memory_read(operation='search', query=...)",
    "memory_search_records": "memory_read(operation='search_records', query=...)",
    "memory_get_runtime_digest": "memory_read(operation='runtime_digest')",
    "memory_retrieve_context": "memory_read(operation='retrieve_context', query=...)",
    "memory_get_important_memories": "memory_read(operation='important_memories')",
    "memory_remember": "memory_write(operation='record', content_markdown=..., context_token=...)",
    "memory_write_record": "memory_write(operation='record', content_markdown=..., context_token=...)",
    "memory_record_observation": "memory_write(operation='observation', content_markdown=..., context_token=...)",
    "memory_context": (
        "memory_read(operation='task_context', ...) for begin/resolve task; "
        "CLI compile / runtime-digest / trace-lineage / list-conflicts / compare-snapshots / "
        "rebuild-key-docs / config-diagnose for the rest"
    ),
    "memory_compile": "CLI: python -m servers.memory_server.cli compile --target ...",
    "memory_rebuild_index": "CLI: python -m servers.memory_server.cli rebuild-index",
    "memory_update_index": "CLI: python -m servers.memory_server.cli rebuild-index",
    "memory_health_check": "CLI: python -m servers.memory_server.cli health",
    "memory_migrate_records": "CLI: python -m servers.memory_server.cli migrate",
    "memory_validate_candidate": "CLI: python -m servers.memory_server.cli validate --record-id ...",
    "memory_publish_candidate": "CLI: python -m servers.memory_server.cli publish --record-id ...",
    "memory_archive_record": "CLI: python -m servers.memory_server.cli archive --record-id ...",
    "memory_delete_record": "CLI: python -m servers.memory_server.cli delete --record-id ...",
    "memory_link_artifact": "CLI: python -m servers.memory_server.cli link-artifact --record-id ...",
    "memory_trace_lineage": "CLI: python -m servers.memory_server.cli trace-lineage --record-id ...",
    "memory_list_conflicts": "CLI: python -m servers.memory_server.cli list-conflicts",
    "memory_compare_snapshots": "CLI: python -m servers.memory_server.cli compare-snapshots --a ... --b ...",
    "memory_backup": "CLI: python -m servers.memory_server.cli backup --path ...",
    "memory_compact": "CLI: python -m servers.memory_server.cli compact --path ...",
    "memory_guard_check": "CLI: python -m servers.memory_server.cli guard",
    "memory_enhance": "CLI: python -m servers.memory_server.cli enhance ...",
}

_GENERIC_CLI_HINT = (
    "MCP exposes only memory_read and memory_write; use the CLI for admin, sync, rebuild, "
    "diagnose, lineage, and LLM-enhance operations."
)


def _migration_hint_for(name: str) -> str:
    """Return a per-name migration hint, falling back to a generic CLI hint."""
    return LEGACY_TOOL_MIGRATION_HINTS.get(name, _GENERIC_CLI_HINT)


def _dispatch_tool(config: MemoryConfig, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call and return the result dict.

    The MCP surface is locked to ``ALLOWED_TOOLS`` (memory_read / memory_write).
    Any other tool name — including legacy ``memory_*`` names that used to be
    part of the MCP surface — returns ``error="unknown_tool"`` with a
    ``migration_hint`` pointing at the equivalent CLI command (or the matching
    facade ``operation``).
    """
    try:
        if name == "memory_read":
            return _dispatch_memory_read(config, args)
        if name == "memory_write":
            return _dispatch_memory_write(config, args)
        result = error_result(
            "unknown_tool",
            f"unknown tool: {name}. {_GENERIC_CLI_HINT}",
        )
        result["migration_hint"] = _migration_hint_for(name)
        return result
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return error_result("internal_error", f"{exc}")


__all__ = [
    "ALLOWED_TOOLS",
    "LEGACY_TOOL_MIGRATION_HINTS",
    "_check_required",
    "_dispatch_memory_read",
    "_dispatch_memory_write",
    "_dispatch_memory_context",
    "_dispatch_memory_enhance",
    "_dispatch_tool",
    "_migration_hint_for",
]
