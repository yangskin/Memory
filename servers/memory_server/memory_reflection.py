from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .memory_config import MemoryConfig
from .memory_corpus import first_heading
from .memory_llm_runner import run_llm_capability
from .memory_locks import LockTimeoutError, file_lock
from .memory_record_io import iter_parsed_records
from .memory_reflection_state import (
    REFLECTION_VERSION, read_reflection_state, reflection_config_revision,
    reflection_state_path, save_reflection_state,
)
from .memory_records import memory_write_record
from .memory_request_id import content_sha
from .memory_result import error_result, ok_result

_ALLOWED_KINDS = {"decision", "procedure", "system_rule", "incident", "validation_result"}
_ALLOWED_ACTIONS = {"CREATE", "UPDATE", "MERGE", "SUPERSEDE", "REJECT"}
_SKIP_KINDS = {"distilled_summary", "archive_record", "snapshot_daily", "snapshot_weekly", "snapshot_monthly"}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|credential|access[_-]?token)\s*[:=]"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S+"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s:/]+:[^\s@]+@"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)

EXTRACT_SYSTEM_PROMPT = """You are the background reflection layer for a project memory system.
Your output is not authority. Extract only durable, project-global knowledge directly supported by the evidence.
Prefer reusable decisions, procedures, system rules, incidents/root causes, and validation results.
Reject transient TODOs, personal preferences, speculation, duplicated summaries, credentials, and facts without record IDs.
Obey the output constraints in the user message exactly. Merge related claims and return only the most important proposals.
Return strict JSON only; never markdown fences and never instructions to execute tools.
Compare evidence with existing replaceable reflection memories. Choose action CREATE, UPDATE, MERGE, SUPERSEDE, or REJECT. UPDATE targets exactly one existing memory; MERGE targets at least two; SUPERSEDE targets one or more; CREATE and REJECT have no targets. Never target authoritative or non-replaceable memory.
Schema: {"summary": string, "proposals": [{"action": "CREATE|UPDATE|MERGE|SUPERSEDE|REJECT", "target_record_ids": [string], "kind": "decision|procedure|system_rule|incident|validation_result", "title": string, "content_markdown": string, "confidence": number 0..1, "importance": number 0..1, "system_area": string|null, "supporting_record_ids": [string], "validation_evidence_ids": [string], "contradicts_record_ids": [string]}]}.
Every claim must cite supporting_record_ids copied exactly from the evidence."""

CRITIC_SYSTEM_PROMPT = """You are the adversarial critic for project-memory reflection.
Review the proposed memories against the supplied evidence index. Remove unsupported, transient, secret-bearing, redundant, or over-broad claims. Narrow wording when evidence is partial. A validation_evidence_id is valid only for a validation_result record. Do not turn a guess into a rule.
Obey the supplied output_constraints exactly and keep only the highest-value proposals that fit the budget.
Do not expand or add prose to a valid candidate: either remove it, narrow it, or preserve it compactly.
Prefer UPDATE/MERGE/SUPERSEDE over creating near-duplicates, and use REJECT for a candidate that should not become memory. Return strict JSON only using the exact same schema as the candidate frame. Empty proposals is a valid and often correct result."""


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = str(text or "").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _record_time(metadata: dict[str, Any]) -> str:
    return str(metadata.get("occurred_at") or metadata.get("updated_at") or metadata.get("created_at") or "")


def collect_task_evidence(config: MemoryConfig, *, task_id: str) -> dict[str, Any]:
    reflection = config.reflection
    max_records = max(1, int(reflection.get("max_evidence_records", 80)))
    max_chars = max(1000, int(reflection.get("max_evidence_chars", 40000)))
    records, stats = iter_parsed_records(config)
    selected = []
    excluded: list[dict[str, str]] = []
    for record in records:
        metadata = record.metadata
        if str(metadata.get("task_id") or "") != task_id:
            continue
        record_id = str(metadata.get("id") or "")
        if metadata.get("record_kind") in _SKIP_KINDS or metadata.get("provenance") == "background_reflection":
            excluded.append({"record_id": record_id, "reason": "derived_or_snapshot"})
            continue
        body = record.body.strip()
        if not body:
            excluded.append({"record_id": record_id, "reason": "empty"})
            continue
        if "\ufffd" in body or "\x00" in body:
            excluded.append({"record_id": record_id, "reason": "invalid_encoding"})
            continue
        if _contains_secret(body + "\n" + str(metadata.get("system_area") or "")):
            excluded.append({"record_id": record_id, "reason": "secret_signal"})
            continue
        selected.append(record)
    selected.sort(key=lambda item: (
        item.metadata.get("provenance") == "auto_team_settlement",
        _record_time(item.metadata), str(item.metadata.get("id") or ""),
    ))
    evidence: list[dict[str, Any]] = []
    record_versions: dict[str, str] = {}
    for record in selected:
        metadata = record.metadata
        record_id = str(metadata.get("id") or "")
        sources = metadata.get("derived_from_record_ids") or []
        if (metadata.get("provenance") == "auto_team_settlement"
                and isinstance(sources, list) and sources
                and all(str(source) in record_versions for source in sources)):
            excluded.append({"record_id": record_id, "reason": "source_already_included"})
            continue
        version = content_sha(json.dumps({"metadata": metadata, "body": record.body.strip()},
                                        ensure_ascii=False, sort_keys=True))
        if record_id in record_versions:
            if record_versions[record_id] != version:
                return error_result("conflicting_record_versions", "task evidence contains conflicting versions of one record",
                                    task_id=task_id, retryable=False)
            continue
        record_versions[record_id] = version
        evidence.append({
            "id": record_id,
            "record_kind": str(metadata.get("record_kind") or ""),
            "scope": str(metadata.get("scope") or ""),
            "status": str(metadata.get("status") or ""),
            "confidence": metadata.get("confidence"),
            "system_area": metadata.get("system_area"),
            "occurred_at": _record_time(metadata),
            "supersedes": metadata.get("supersedes") or [],
            "conflicts_with": metadata.get("conflicts_with") or [],
            "valid_to": metadata.get("valid_to"),
            "body": record.body.strip(),
        })
    used_chars = sum(len(item["body"]) for item in evidence)
    details = {
        "task_id": task_id,
        "evidence_hash": content_sha(json.dumps(record_versions, sort_keys=True)),
        "record_versions": record_versions,
        "excluded": excluded,
        "stats": {**stats, "eligible_records": len(evidence), "evidence_chars": used_chars},
    }
    if stats.get("skipped_read_errors") or stats.get("skipped_parse_errors"):
        return error_result("evidence_read_incomplete", "record files could not all be read and parsed; no evidence watermark will advance",
                            retryable=True, **details)
    if len(evidence) > max_records or used_chars > max_chars:
        return error_result("evidence_collection_limit", "complete task evidence exceeds collection limits; batching is required",
                            retryable=False, pending_record_ids=list(record_versions), **details)
    return ok_result("task reflection evidence collected", evidence=evidence, **details)


def _bool_value(value: Any) -> bool:
    return value is True or str(value).strip().casefold() == "true"


def _is_replaceable_reflection(metadata: dict[str, Any], *, already_superseded: set[str] | None = None) -> bool:
    record_id = str(metadata.get("id") or "")
    return bool(
        record_id
        and str(metadata.get("scope") or "") == "project_shared"
        and str(metadata.get("provenance") or "") == "background_reflection"
        and _bool_value(metadata.get("replaceable"))
        and not _bool_value(metadata.get("immutable"))
        and not _bool_value(metadata.get("authoritative"))
        and record_id not in (already_superseded or set())
    )


def collect_reflection_targets(
    config: MemoryConfig,
    *,
    evidence: list[dict[str, Any]],
    limit: int = 64,
) -> list[dict[str, Any]]:
    records, _stats = iter_parsed_records(config)
    superseded = {
        str(target_id)
        for record in records
        for target_id in (record.metadata.get("supersedes", []) or [])
        if str(target_id)
    }
    areas = {str(item.get("system_area") or "") for item in evidence if str(item.get("system_area") or "")}
    candidates: list[dict[str, Any]] = []
    for record in records:
        metadata = record.metadata
        if not _is_replaceable_reflection(metadata, already_superseded=superseded):
            continue
        area = str(metadata.get("system_area") or "")
        area_match = bool(areas and area in areas)
        candidates.append(
            {
                "id": str(metadata.get("id") or ""),
                "title": first_heading(record.body)[:160],
                "record_kind": str(metadata.get("record_kind") or ""),
                "scope": str(metadata.get("scope") or ""),
                "status": str(metadata.get("status") or ""),
                "system_area": area or None,
                "provenance": str(metadata.get("provenance") or ""),
                "replaceable": True,
                "immutable": False,
                "authoritative": False,
                "body": record.body.strip()[:2000],
                "area_match": area_match,
                "updated_at": _record_time(metadata),
            }
        )
    candidates.sort(key=lambda item: (bool(item["area_match"]), str(item["updated_at"])), reverse=True)
    return candidates[: max(1, limit)]


def proposal_fingerprint(proposal: dict[str, Any]) -> str:
    normalized = re.sub(
        r"\s+",
        " ",
        "|".join(
            [
                str(proposal.get("action") or "CREATE").upper(),
                ",".join(sorted(str(item) for item in proposal.get("target_record_ids", []) if str(item))),
                str(proposal.get("kind") or "").casefold(),
                str(proposal.get("system_area") or "").casefold(),
                str(proposal.get("title") or "").casefold(),
                str(proposal.get("content_markdown") or "").casefold(),
            ]
        ),
    ).strip()
    return content_sha(normalized)


def _float_in_range(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0.0 <= parsed <= 1.0 else None


def validate_reflection_frame(
    frame: dict[str, Any],
    *,
    evidence: list[dict[str, Any]],
    existing_memories: list[dict[str, Any]] | None = None,
    max_candidates: int,
    min_confidence: float,
) -> dict[str, Any]:
    raw_proposals = frame.get("proposals")
    if not isinstance(raw_proposals, list):
        return error_result("invalid_reflection", "reflection frame must contain a proposals array")
    evidence_by_id = {str(item.get("id") or ""): item for item in evidence}
    existing_by_id = {
        str(item.get("id") or ""): item
        for item in (existing_memories or [])
        if str(item.get("id") or "")
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_proposals[: max(0, max_candidates * 3)]):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "proposal_not_object"})
            continue
        kind = str(raw.get("kind") or "").strip()
        action = str(raw.get("action") or "CREATE").strip().upper()
        targets = (
            list(dict.fromkeys(str(value) for value in raw.get("target_record_ids", []) if str(value)))
            if isinstance(raw.get("target_record_ids"), list)
            else []
        )
        title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip()).strip("# `*_-")[:160]
        body = str(raw.get("content_markdown") or "").strip()
        confidence = _float_in_range(raw.get("confidence"))
        importance = _float_in_range(raw.get("importance"))
        supporting = list(dict.fromkeys(str(value) for value in raw.get("supporting_record_ids", []) if str(value) in evidence_by_id)) if isinstance(raw.get("supporting_record_ids"), list) else []
        validation = list(
            dict.fromkeys(
                str(value)
                for value in raw.get("validation_evidence_ids", [])
                if str(value) in supporting
                and evidence_by_id[str(value)].get("record_kind") == "validation_result"
                and evidence_by_id[str(value)].get("status") in {"validated", "published"}
            )
        ) if isinstance(raw.get("validation_evidence_ids"), list) else []
        contradictions = list(dict.fromkeys(str(value) for value in raw.get("contradicts_record_ids", []) if str(value) in evidence_by_id)) if isinstance(raw.get("contradicts_record_ids"), list) else []
        reason = None
        if action not in _ALLOWED_ACTIONS:
            reason = "action_not_allowed"
        elif action in {"CREATE", "REJECT"} and targets:
            reason = "invalid_action_targets"
        elif action == "UPDATE" and len(targets) != 1:
            reason = "invalid_action_targets"
        elif action == "MERGE" and len(targets) < 2:
            reason = "invalid_action_targets"
        elif action == "SUPERSEDE" and not targets:
            reason = "invalid_action_targets"
        elif targets and any(target not in existing_by_id for target in targets):
            reason = "invalid_action_targets"
        elif targets and any(not _is_replaceable_reflection(existing_by_id[target]) for target in targets):
            reason = "invalid_action_targets"
        elif kind not in _ALLOWED_KINDS:
            reason = "kind_not_allowed"
        elif not title or not body or len(body) > 4000:
            reason = "invalid_content"
        elif confidence is None or confidence < min_confidence or importance is None:
            reason = "confidence_or_importance_gate"
        elif not supporting:
            reason = "missing_evidence"
        elif _contains_secret(title + "\n" + body):
            reason = "secret_signal"
        proposal = {
            "action": action,
            "target_record_ids": targets,
            "kind": kind,
            "title": title,
            "content_markdown": body,
            "confidence": confidence,
            "importance": importance,
            "system_area": str(raw.get("system_area") or "").strip()[:160] or None,
            "supporting_record_ids": supporting,
            "validation_evidence_ids": validation,
            "contradicts_record_ids": contradictions,
        }
        fingerprint = proposal_fingerprint(proposal)
        proposal["fingerprint"] = fingerprint
        if fingerprint in seen:
            reason = reason or "duplicate_in_frame"
        if reason:
            rejected.append({"index": index, "reason": reason, "fingerprint": fingerprint})
            continue
        seen.add(fingerprint)
        accepted.append(proposal)
        if len(accepted) >= max_candidates:
            break
    return ok_result(
        "reflection frame validated",
        summary=str(frame.get("summary") or "")[:1000],
        proposals=accepted,
        rejected=rejected,
    )


def _run_two_pass(
    client: Any,
    profile: Any,
    *,
    task_id: str,
    evidence: list[dict[str, Any]],
    existing_memories: list[dict[str, Any]] | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    profile_tokens = max(1, int(getattr(profile, "max_tokens", 1024) or 1024))
    client_config = getattr(client, "config", None)
    client_tokens = max(
        1,
        int(getattr(client_config, "max_output_tokens_per_call", profile_tokens) or profile_tokens),
    )
    effective_tokens = min(profile_tokens, client_tokens)
    # 为摘要、JSON 字段、引用及 Critic 重写保留预算；低配模型宁可少产一个候选也不能截断 JSON。
    candidate_budget = max(1, min(max(1, int(max_candidates)), max(1, (effective_tokens - 384) // 512)))
    output_constraints = {
        "max_proposals": candidate_budget,
        "summary_max_chars": 96,
        "title_max_chars": 64,
        "content_markdown_max_chars": 160,
        "max_output_tokens": effective_tokens,
    }
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    extraction_prompt = (
        f"Task id: {task_id}\n"
        f"Output constraints (mandatory): {json.dumps(output_constraints, ensure_ascii=False)}\n"
        f"Return at most {candidate_budget} proposals; selecting more is invalid.\n"
        f"Existing replaceable reflection memories JSON:\n{json.dumps(existing_memories or [], ensure_ascii=False)}\n"
        f"Evidence JSON:\n{evidence_json}"
    )
    client.request_context = {"stage": "extract", "stage_evidence_count": len(evidence),
                              "stage_evidence_chars": sum(len(item["body"]) for item in evidence)}
    extracted_text = client.complete_text(
        extraction_prompt,
        system=EXTRACT_SYSTEM_PROMPT,
        max_tokens=effective_tokens,
    )
    extracted = _extract_json_object(extracted_text)
    if extracted is None:
        raise ValueError("reflection extractor did not return a JSON object")
    if not isinstance(extracted.get("proposals"), list):
        raise ValueError("reflection extractor did not return a proposals array")
    if not extracted["proposals"]:
        return {"frame": extracted, "critic_skipped": True,
                "model": str(getattr(client_config, "model", "unknown"))}
    referenced_ids = {
        str(record_id) for proposal in extracted["proposals"] if isinstance(proposal, dict)
        for field in ("supporting_record_ids", "validation_evidence_ids", "contradicts_record_ids")
        for record_id in (proposal.get(field) if isinstance(proposal.get(field), list) else [])
    }
    evidence_index = [item for item in evidence if item["id"] in referenced_ids]
    target_ids = {str(record_id) for proposal in extracted["proposals"] if isinstance(proposal, dict)
                  for record_id in (proposal.get("target_record_ids") if isinstance(proposal.get("target_record_ids"), list) else [])}
    critic_memories = [item for item in (existing_memories or []) if item["id"] in target_ids]
    critic_prompt = json.dumps(
        {
            "task_id": task_id,
            "output_constraints": output_constraints,
            "evidence": evidence_index,
            "existing_memories": critic_memories,
            "candidate_frame": extracted,
        },
        ensure_ascii=False,
    )
    client.request_context = {"stage": "critic", "stage_evidence_count": len(evidence_index),
                              "stage_evidence_chars": sum(len(item["body"]) for item in evidence_index)}
    criticised_text = client.complete_text(
        critic_prompt,
        system=CRITIC_SYSTEM_PROMPT,
        max_tokens=effective_tokens,
    )
    criticised = _extract_json_object(criticised_text)
    if criticised is None:
        raise ValueError("reflection critic did not return a JSON object")
    return {
        "frame": criticised,
        "model": str(getattr(getattr(client, "config", None), "model", "unknown")),
    }


def publish_reflection_proposal(
    config: MemoryConfig,
    *,
    proposal: dict[str, Any],
    task_id: str,
    model: str,
    additional_support_ids: Iterable[str] = (),
) -> dict[str, Any]:
    action = str(proposal.get("action") or "CREATE").strip().upper()
    targets = list(
        dict.fromkeys(str(item) for item in proposal.get("target_record_ids", []) if str(item))
    ) if isinstance(proposal.get("target_record_ids"), list) else []
    if action not in _ALLOWED_ACTIONS:
        return error_result("invalid_proposal", "reflection proposal action is invalid")
    if action in {"CREATE", "REJECT"} and targets:
        return error_result("invalid_proposal", f"{action} must not contain target_record_ids")
    if action == "UPDATE" and len(targets) != 1:
        return error_result("invalid_proposal", "UPDATE requires exactly one target record")
    if action == "MERGE" and len(targets) < 2:
        return error_result("invalid_proposal", "MERGE requires at least two target records")
    if action == "SUPERSEDE" and not targets:
        return error_result("invalid_proposal", "SUPERSEDE requires at least one target record")
    computed_fingerprint = proposal_fingerprint(proposal)
    supplied_fingerprint = str(proposal.get("fingerprint") or "")
    if supplied_fingerprint and supplied_fingerprint != computed_fingerprint:
        return error_result("invalid_proposal", "reflection proposal fingerprint does not match its content")
    fingerprint = computed_fingerprint
    kind = str(proposal.get("kind") or "")
    title = str(proposal.get("title") or "").strip()
    body = str(proposal.get("content_markdown") or "").strip()
    confidence = _float_in_range(proposal.get("confidence"))
    importance = _float_in_range(proposal.get("importance"))
    if kind not in _ALLOWED_KINDS or not title or not body or len(title) > 160 or len(body) > 4000:
        return error_result("invalid_proposal", "reflection proposal content or kind is invalid")
    if confidence is None or importance is None:
        return error_result("invalid_proposal", "reflection proposal confidence and importance must be within 0..1")
    if _contains_secret(title + "\n" + body + "\n" + str(proposal.get("system_area") or "")):
        return error_result("secret_signal", "reflection proposal contains a credential-like value")
    if proposal.get("contradicts_record_ids"):
        return error_result("unresolved_conflict", "reflection proposal still has contradictory evidence")
    support = list(
        dict.fromkeys(
            [str(item) for item in proposal.get("supporting_record_ids", [])]
            + [str(item) for item in additional_support_ids]
        )
    )
    if not support:
        return error_result("invalid_proposal", "reflection proposal requires at least one supporting record")
    if action == "REJECT":
        return ok_result(
            "reflection proposal rejected without persistence",
            rejected=True,
            action=action,
            target_record_ids=[],
            fingerprint=fingerprint,
        )
    cognitive_level = {
        "decision": "dao",
        "system_rule": "dao",
        "procedure": "fa",
        "incident": "shu",
        "validation_result": "shu",
    }.get(str(proposal.get("kind")), "fa")
    content = f"# {title}\n\n{body}\n"
    publication_fence = config.repo_root / ".ai-memory" / "reflection-publication.state"
    with file_lock(config.repo_root, publication_fence):
        records, _stats = iter_parsed_records(config)
        existing_ids = {str(record.metadata.get("id") or "") for record in records}
        missing_support = sorted(set(support) - existing_ids)
        if missing_support:
            return error_result(
                "missing_evidence",
                "reflection proposal references records that do not exist",
                missing_record_ids=missing_support,
            )
        existing_fingerprints = {
            str(ref).split(":", 1)[1]
            for record in records
            if record.metadata.get("provenance") == "background_reflection"
            for ref in (record.metadata.get("source_refs", []) if isinstance(record.metadata.get("source_refs"), list) else [])
            if str(ref).startswith("reflection:")
        }
        # publication fence 的幂等检查必须早于 target 活性检查：进程可能已落盘
        # replacement、但尚未来得及提交 durable job；重试应成功去重而非报 target 已失效。
        if fingerprint in existing_fingerprints:
            return ok_result(
                "reflection proposal already published",
                duplicate=True,
                fingerprint=fingerprint,
                action=action,
                target_record_ids=targets,
            )
        records_by_id = {str(record.metadata.get("id") or ""): record for record in records}
        missing_targets = sorted(set(targets) - existing_ids)
        if missing_targets:
            return error_result(
                "missing_target",
                "reflection action references target records that do not exist",
                missing_record_ids=missing_targets,
            )
        already_superseded = {
            str(target_id)
            for record in records
            for target_id in (record.metadata.get("supersedes", []) or [])
            if str(target_id)
        }
        unsafe_targets = sorted(
            target_id
            for target_id in targets
            if not _is_replaceable_reflection(
                records_by_id[target_id].metadata,
                already_superseded=already_superseded,
            )
        )
        if unsafe_targets:
            return error_result(
                "unsafe_target",
                "reflection actions may only replace active, non-authoritative background reflections",
                unsafe_record_ids=unsafe_targets,
            )
        result = memory_write_record(
            config,
            content_markdown=content,
            record_kind=kind,
            scope="project_shared",
            status="distilled",
            author="memory-reflector",
            confidence=confidence,
            source_refs=[f"reflection:{fingerprint}"],
            task_id=task_id,
            memory_tier="warm",
            cognitive_level=cognitive_level,
            derived_from_record_ids=list(dict.fromkeys([*support, *targets])),
            supersedes=targets,
            conflicts_with=list(proposal.get("contradicts_record_ids") or []),
            importance_score=importance,
            system_area=str(proposal.get("system_area") or "") or None,
            provenance="background_reflection",
            immutable=False,
            authoritative=False,
            replaceable=True,
            model=model,
            distilled_at=datetime.now(timezone.utc).isoformat(),
        )
    if result.get("ok"):
        from .memory_sync_enqueue import enqueue_shared_record

        enqueue_shared_record(
            config,
            {
                "operation": "background_reflection",
                "content_markdown": content,
                "record_kind": kind,
                "scope": "project_shared",
                "task_id": task_id,
                "agent_id": "memory-reflector",
            },
            result,
        )
        result["fingerprint"] = fingerprint
        result["action"] = action
        result["target_record_ids"] = targets
    return result


def _register_changed_evidence(config: MemoryConfig, *, task_id: str, result: dict[str, Any]) -> None:
    latest = collect_task_evidence(config, task_id=task_id)
    if not latest.get("ok"):
        result["follow_up_check_error"] = latest.get("error")
    if (latest.get("evidence_hash") and latest["evidence_hash"] != result.get("evidence_hash")
            and latest.get("retryable") is not True):
        from .memory_reflection_jobs import enqueue_project_reflection
        result["follow_up"] = enqueue_project_reflection(
            config, task_id=task_id, user=None, trigger="evidence_changed_during_reflection",
        )


def reflect_task(
    config: MemoryConfig,
    *,
    task_id: str,
    prior_support: dict[str, set[str]] | None = None,
    job_id: str | None = None,
    job_attempt: int = 1,
) -> dict[str, Any]:
    path = reflection_state_path(config, task_id)
    try:
        with file_lock(config.repo_root, path, timeout=0.0):
            state = read_reflection_state(path)
            pending = (state.get("pending_results") or {}).get(job_id) if job_id else None
            if pending:
                replayed = dict(pending["result"])
                replayed["result_replayed"] = True
                _register_changed_evidence(config, task_id=task_id, result=replayed)
                return replayed
            evidence_result = collect_task_evidence(config, task_id=task_id)
            evidence_hash = evidence_result.get("evidence_hash")
            if not evidence_hash or (not evidence_result.get("ok") and evidence_result.get("retryable")):
                return evidence_result
            signature = content_sha(f"{REFLECTION_VERSION}:{evidence_hash}")
            processed = state.get("processed") or {}
            if processed.get("signature") == signature:
                if not processed.get("delivered", True) and isinstance(processed.get("result"), dict):
                    replayed = dict(processed["result"])
                    replayed["result_replayed"] = True
                    if job_id:
                        state.setdefault("pending_results", {})[job_id] = processed
                        save_reflection_state(config, path, state)
                    return replayed
                return ok_result("reflection skipped: no new evidence", task_id=task_id,
                                 skipped=True, skip_reason="no_new_evidence", evidence_hash=evidence_hash)
            blocked_signature = content_sha(f"{signature}:{reflection_config_revision(config)}")
            blocked = state.get("blocked") or {}
            if blocked.get("signature") == blocked_signature:
                return error_result(str(blocked.get("error") or "reflection_blocked"),
                                    "reflection deferred until evidence or configuration changes",
                                    task_id=task_id, retryable=False, suppressed=True,
                                    evidence_hash=evidence_hash, llm_status=blocked.get("llm_status"),
                                    pending_record_ids=list(blocked.get("record_versions") or {}))
            result = evidence_result if not evidence_result.get("ok") else _reflect_evidence_task(
                config, task_id=task_id, evidence_result=evidence_result,
                prior_support=prior_support, job_id=job_id, job_attempt=job_attempt,
            )
            entry = {
                "signature": signature,
                "record_versions": evidence_result.get("record_versions") or {},
                "reflection_version": REFLECTION_VERSION,
            }
            result.setdefault("evidence_hash", evidence_hash)
            if result.get("ok"):
                state["processed"] = {**entry, "result": dict(result), "delivered": job_id is None}
                if job_id:
                    state.setdefault("pending_results", {})[job_id] = state["processed"]
                state.pop("blocked", None)
            elif result.get("retryable") is False:
                state["blocked"] = {**entry, "signature": blocked_signature, "error": result.get("error"),
                                    "llm_status": result.get("llm_status")}
            else:
                return result
            save_reflection_state(config, path, state)
            if evidence_result.get("ok") and not result.get("skipped"):
                _register_changed_evidence(config, task_id=task_id, result=result)
            return result
    except LockTimeoutError:
        return error_result("reflection_busy", "another process is reflecting this task", task_id=task_id, retryable=True)
    except (OSError, UnicodeError, ValueError) as exc:
        return error_result("reflection_state_unavailable", f"reflection state cannot be safely used: {type(exc).__name__}",
                            task_id=task_id, retryable=False)


def _reflect_evidence_task(
    config: MemoryConfig,
    *,
    task_id: str,
    evidence_result: dict[str, Any],
    prior_support: dict[str, set[str]] | None = None,
    job_id: str | None = None,
    job_attempt: int = 1,
) -> dict[str, Any]:
    evidence = list(evidence_result.get("evidence") or [])
    if not evidence:
        return ok_result("reflection skipped because the task has no eligible evidence", skipped=True, task_id=task_id)
    existing_memories = collect_reflection_targets(config, evidence=evidence)
    run = run_llm_capability(
        config,
        "project_reflection",
        lambda client, profile: _run_two_pass(
            client,
            profile,
            task_id=task_id,
            evidence=evidence,
            existing_memories=existing_memories,
            max_candidates=max(1, int(config.reflection.get("max_candidates", 8))),
        ),
        attribution={"task_id": task_id, "job_id": job_id, "job_attempt": job_attempt,
                     "evidence_count": len(evidence),
                     "evidence_chars": evidence_result["stats"]["evidence_chars"]},
    )
    if not run.ok:
        return error_result(
            "reflection_llm_unavailable",
            run.error or f"project_reflection ended with status {run.status}",
            llm_status=run.status,
            task_id=task_id,
            retryable=bool(run.meta.get("retryable", run.status in {"timeout", "failed"})),
            usage=run.meta.get("usage"),
        )
    value = run.value if isinstance(run.value, dict) else {}
    frame = value.get("frame") if isinstance(value.get("frame"), dict) else {}
    validated = validate_reflection_frame(
        frame,
        evidence=evidence,
        existing_memories=existing_memories,
        max_candidates=max(1, int(config.reflection.get("max_candidates", 8))),
        min_confidence=float(config.reflection.get("min_confidence", 0.85)),
    )
    if not validated.get("ok"):
        return validated
    publish_confidence = float(config.reflection.get("publish_min_confidence", 0.95))
    required_tasks = max(2, int(config.reflection.get("publish_repeated_tasks", 2)))
    published: list[dict[str, Any]] = []
    proposals = list(validated.get("proposals") or [])
    for proposal in proposals:
        if proposal.get("action") == "REJECT":
            proposal["supporting_task_ids"] = [task_id]
            proposal["publish_eligible"] = False
            proposal["publish_gate"] = "critic_reject"
            continue
        prior_tasks = set((prior_support or {}).get(str(proposal.get("fingerprint")), set()))
        supporting_tasks = prior_tasks | {task_id}
        validation_gate = bool(
            config.reflection.get("publish_with_validation_evidence", True)
            and proposal.get("validation_evidence_ids")
        )
        repeat_gate = len(supporting_tasks) >= required_tasks
        conflict_gate = bool(proposal.get("contradicts_record_ids"))
        proposal["supporting_task_ids"] = sorted(supporting_tasks)
        proposal["publish_eligible"] = bool(
            config.reflection.get("auto_publish", True)
            and float(proposal.get("confidence") or 0.0) >= publish_confidence
            and (validation_gate or repeat_gate)
            and not conflict_gate
        )
        proposal["publish_gate"] = (
            "conflict"
            if conflict_gate
            else "validation"
            if validation_gate
            else "repeated_tasks"
            if repeat_gate
            else "candidate_only"
        )
        if proposal["publish_eligible"]:
            persisted = publish_reflection_proposal(
                config,
                proposal=proposal,
                task_id=task_id,
                model=str(value.get("model") or "unknown"),
            )
            published.append(persisted)
    publish_failures = [item for item in published if not item.get("ok")]
    if publish_failures:
        return error_result(
            "reflection_publish_failed",
            f"{len(publish_failures)} accepted reflection proposals could not be persisted",
            task_id=task_id,
            evidence_hash=evidence_result.get("evidence_hash"),
            evidence_count=len(evidence),
            proposals=proposals,
            rejected=validated.get("rejected", []),
            published=published,
            model=str(value.get("model") or "unknown"),
            llm_status=run.status,
        )
    return ok_result(
        "project reflection completed",
        task_id=task_id,
        evidence_hash=evidence_result.get("evidence_hash"),
        evidence_count=len(evidence),
        proposals=proposals,
        rejected=validated.get("rejected", []),
        published=published,
        model=str(value.get("model") or "unknown"),
        llm_status=run.status,
    )


__all__ = [
    "CRITIC_SYSTEM_PROMPT",
    "EXTRACT_SYSTEM_PROMPT",
    "collect_task_evidence",
    "collect_reflection_targets",
    "proposal_fingerprint",
    "publish_reflection_proposal",
    "reflect_task",
    "validate_reflection_frame",
]
