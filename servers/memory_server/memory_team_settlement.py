from __future__ import annotations

import json
import re
from typing import Any

from .memory_config import MemoryConfig
from .memory_records import memory_write_record


_TEAM_RECORD_KINDS = {
    "decision",
    "incident",
    "procedure",
    "validation_result",
    "handoff",
    "system_rule",
}
_TEAM_TAGS = {"high_value", "handoff_ready", "mcp", "workflow", "validation", "build"}
_SKIP_RECORD_KINDS = {"distilled_summary", "archive_record", "snapshot_daily", "snapshot_weekly", "snapshot_monthly"}
_SKIP_SCOPES = {"session", "user_private", "local", "archive"}
_SHARED_SCOPES = {"shared", "project_shared", "org_shared"}
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|credential|token)\s*[:=]"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
]


def _clean_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        token = str(item).strip()
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if "\n" in stripped:
            stripped = stripped.split("\n", 1)[1]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _has_secret_signal(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:120]
    return ""


def _deterministic_summary(content: str, *, max_chars: int) -> str:
    heading = _first_heading(content) or "Team-relevant memory"
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in {"---"}:
            continue
        lines.append(stripped)
        if len(lines) >= 8:
            break
    body = "\n".join(lines) if lines else content.strip()
    text = f"# {heading}\n\n{body}".strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _clean_heading_text(value: Any, *, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = text.strip("# `*_")
    if not text:
        text = fallback
    return text[:120].rstrip() or fallback


def _stable_team_heading(args: dict[str, Any], write_result: dict[str, Any], content: str, raw_id: str) -> str:
    base = _first_heading(content)
    if not base:
        system_area = str(args.get("system_area") or "").strip()
        if system_area:
            base = system_area.replace(".", " / ")
    if not base:
        kind = str(write_result.get("record_kind") or args.get("record_kind") or "memory").strip()
        base = f"Team {kind}"
    heading = _clean_heading_text(base, fallback="Team memory")
    suffix = raw_id[-8:] if raw_id else ""
    return f"{heading} [{suffix}]" if suffix and suffix not in heading else heading


def _ensure_stable_summary_heading(summary: str, *, heading: str) -> str:
    body = summary.strip()
    if not body:
        return f"# {heading}"
    first_line = body.splitlines()[0].strip()
    if first_line.startswith("#") and first_line.lstrip("#").strip() == heading:
        return body
    return f"# {heading}\n\n{body}"


def _deterministic_decision(args: dict[str, Any], write_result: dict[str, Any], content: str) -> dict[str, Any]:
    record_kind = str(write_result.get("record_kind") or args.get("record_kind") or "note").strip()
    scope = str(write_result.get("scope") or args.get("scope") or "personal").strip()
    status = str(write_result.get("status") or args.get("status") or "raw").strip()
    tags = _clean_tags(args.get("tags"))

    if scope in _SHARED_SCOPES or status == "published":
        return {"promote": False, "reason": "already_shared"}
    if scope in _SKIP_SCOPES:
        return {"promote": False, "reason": "private_or_transient_scope"}
    if record_kind in _SKIP_RECORD_KINDS or record_kind.endswith("_candidate"):
        return {"promote": False, "reason": "non_settleable_record_kind"}
    if status in {"archived", "distilled", "degraded"}:
        return {"promote": False, "reason": "non_raw_status"}
    if not content.strip():
        return {"promote": False, "reason": "empty_content"}
    if _has_secret_signal(content):
        return {"promote": False, "reason": "secret_signal"}

    tag_hit = bool(set(tags) & _TEAM_TAGS)
    kind_hit = record_kind in _TEAM_RECORD_KINDS
    important = False
    try:
        important = float(args.get("importance_score")) >= 0.65
    except (TypeError, ValueError):
        important = False

    if kind_hit or tag_hit or important:
        return {
            "promote": True,
            "reason": "deterministic_high_signal",
            "summary": "",
            "tags": tags,
        }
    return {"promote": False, "reason": "low_signal"}


def _llm_decision(
    config: MemoryConfig,
    *,
    settings: Any,
    args: dict[str, Any],
    write_result: dict[str, Any],
    content: str,
    deterministic: dict[str, Any],
) -> dict[str, Any]:
    llm_gate = str(getattr(settings, "llm_gate", "when_available") or "when_available").strip().lower()
    source_scope = str(write_result.get("scope") or args.get("scope") or "personal").strip()
    if llm_gate == "off":
        return {"used": False, "status": "off", **deterministic}

    from .memory_llm import extract_text
    from .memory_llm_runner import run_llm_capability

    payload = {
        "write_result": {
            key: write_result.get(key)
            for key in ("id", "path", "record_kind", "scope", "status", "author", "task_id", "branch")
            if write_result.get(key) is not None
        },
        "tags": _clean_tags(args.get("tags")),
        "task_phase": args.get("task_phase"),
        "content_excerpt": content[:4000],
        "deterministic": deterministic,
    }
    messages = [
        {
            "role": "system",
            "content": (
                "You are a conservative Memory MCP team-settlement gate. Return only compact JSON. "
                "Decide whether a personal/task memory should also get a derived project_shared summary for team key documents. "
                "Never promote secrets, credentials, private preferences, scratch notes, or uncertain claims."
            ),
        },
        {
            "role": "user",
            "content": (
                "Input JSON:\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n\n"
                "Return JSON schema: {\"promote\": boolean, \"reason\": string, "
                "\"summary\": string, \"tags\": [string]}. Summary must be factual, concise, and safe for team context."
            ),
        },
    ]

    def _invoke(client, profile):
        response = client.chat(messages, max_tokens=profile.max_tokens, temperature=0, thinking=False)
        parsed = _extract_json_object(extract_text(response))
        if parsed is None:
            if source_scope == "personal":
                return {"promote": False, "reason": "personal_scope_llm_output_unparseable"}
            return {**deterministic, "reason": "llm_output_unparseable"}
        return parsed

    result = run_llm_capability(
        config,
        "auto_memory_gate",
        _invoke,
        force_enabled=llm_gate in {"when_available", "always"},
    )
    if not result.ok:
        fallback = llm_gate != "always"
        fallback_decision = deterministic if fallback else {"promote": False, "reason": "llm_required_unavailable"}
        # personal 记录的正文默认只属于当前用户。LLM 不可用或失败时，
        # 确定性规则无法证明其适合团队共享，因此必须保守地拒绝自动提升。
        # 只有显式配置 llm_gate=off 才表示消费项目主动接受确定性提升。
        if fallback and source_scope == "personal":
            fallback_decision = {
                **deterministic,
                "promote": False,
                "reason": "personal_scope_requires_llm",
            }
        return {
            "used": False,
            "status": result.status,
            "error": result.error,
            "fallback_used": fallback,
            **fallback_decision,
        }
    value = result.value if isinstance(result.value, dict) else {}
    return {
        "used": True,
        "status": result.status,
        "promote": bool(value.get("promote")),
        "reason": str(value.get("reason") or ""),
        "summary": str(value.get("summary") or ""),
        "tags": _clean_tags(value.get("tags")),
        "meta": result.meta,
    }


def maybe_auto_settle_team_record(
    config: MemoryConfig,
    *,
    args: dict[str, Any],
    write_result: dict[str, Any],
) -> dict[str, Any]:
    """Create a derived shared record when a fresh write is team-relevant.

    The original raw record stays untouched. This helper only adds a second
    project_shared/shared/org_shared record with ``derived_from_record_ids``
    pointing back to the original, so generated team key documents can consume
    it without leaking all personal notes.
    """

    settings = getattr(config, "key_documents_auto_team_settlement", None)
    if settings is None or not settings.enabled:
        return {"enabled": False, "promoted": False, "reason": "disabled"}
    if getattr(config, "key_documents_mode", "auto") != "auto":
        return {"enabled": True, "promoted": False, "reason": "key_documents_mode_not_auto"}
    if not bool(write_result.get("ok")):
        return {"enabled": True, "promoted": False, "reason": "write_not_ok"}

    raw_id = str(write_result.get("id") or "").strip()
    if not raw_id:
        return {"enabled": True, "promoted": False, "reason": "missing_record_id"}

    content = str(args.get("content_markdown") or args.get("content") or "")
    deterministic = _deterministic_decision(args, write_result, content)
    if deterministic.get("reason") in {
        "already_shared",
        "private_or_transient_scope",
        "non_settleable_record_kind",
        "non_raw_status",
        "empty_content",
        "secret_signal",
    }:
        return {
            "enabled": True,
            "promoted": False,
            "reason": str(deterministic.get("reason") or "not_selected"),
            "gate": {"used": False, "status": "hard_skip", **deterministic},
        }
    decision = _llm_decision(
        config,
        settings=settings,
        args=args,
        write_result=write_result,
        content=content,
        deterministic=deterministic,
    )
    if not bool(decision.get("promote")):
        return {
            "enabled": True,
            "promoted": False,
            "reason": str(decision.get("reason") or deterministic.get("reason") or "not_selected"),
            "gate": decision,
        }

    max_chars = int(getattr(settings, "max_summary_chars", 1200) or 1200)
    summary = str(decision.get("summary") or "").strip()
    if not summary:
        summary = _deterministic_summary(content, max_chars=max_chars)
    elif len(summary) > max_chars:
        summary = summary[: max(0, max_chars - 3)].rstrip() + "..."
    summary = _ensure_stable_summary_heading(
        summary,
        heading=_stable_team_heading(args, write_result, content, raw_id),
    )
    if len(summary) > max_chars:
        summary = summary[: max(0, max_chars - 3)].rstrip() + "..."

    tags = _clean_tags(decision.get("tags")) or _clean_tags(args.get("tags"))
    tags = [tag for tag in tags if tag in set(config.tag_allowed_tags or [])]
    if "mcp" in (config.tag_allowed_tags or []) and "mcp" not in tags:
        tags.append("mcp")

    promoted = memory_write_record(
        config,
        content_markdown=summary,
        record_kind=str(write_result.get("record_kind") or args.get("record_kind") or "note"),
        scope=str(getattr(settings, "target_scope", "project_shared") or "project_shared"),
        status="raw",
        author=str(write_result.get("author") or args.get("author") or "system"),
        tags=tags,
        confidence=_float_or_none(args.get("confidence")),
        source_refs=args.get("source_refs"),
        task_id=str(args["task_id"]) if args.get("task_id") is not None else None,
        branch=str(args["branch"]) if args.get("branch") is not None else None,
        tag_schema_version=str(args.get("tag_schema_version", "v1")),
        occurred_at=str(args["occurred_at"]) if args.get("occurred_at") is not None else None,
        memory_tier=str(args["memory_tier"]) if args.get("memory_tier") is not None else "warm",
        cognitive_level=str(args["cognitive_level"]) if args.get("cognitive_level") is not None else "fa",
        derived_from_record_ids=[raw_id],
        related_artifact_ids=args.get("related_artifact_ids"),
        asset_paths=args.get("asset_paths"),
        map_names=args.get("map_names"),
        plugin_names=args.get("plugin_names"),
        module_names=args.get("module_names"),
        class_names=args.get("class_names"),
        blueprint_paths=args.get("blueprint_paths"),
        system_area=str(args["system_area"]) if args.get("system_area") is not None else None,
        provenance="auto_team_settlement",
        immutable=False,
        authoritative=False,
        replaceable=True,
    )
    return {
        "enabled": True,
        "promoted": bool(promoted.get("ok")),
        "reason": str(decision.get("reason") or "selected"),
        "source_record_id": raw_id,
        "promoted_record_id": promoted.get("id"),
        "promoted_path": promoted.get("path"),
        "persist_result": promoted,
        "gate": decision,
    }
