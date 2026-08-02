"""LLM-backed enhancement helpers (classify / extract / merge / skill / conflict / handoff).

These are opt-in, soft-enhancement layers on top of the deterministic memory
core. None of them write to disk; they return structured dicts that callers can
then choose to persist (typically as ``record_kind=*_candidate`` records via
``memory_write_record``).

Hard rules (mirroring §2.0 / §2.1.A):

- Never mutate raw records.
- Never feed an LLM-produced suggestion back as authoritative without an
  explicit human/agent validation step (status stays ``candidate``).
- All entry points require an ``LLMClient`` and respect its budget caps; failures
  surface as :class:`LLMEnhanceError` for in-band reporting.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

from .memory_llm import LLMClient, LLMError

__all__ = [
    "LLMEnhanceError",
    "classify_record",
    "extract_candidates",
    "merge_candidates",
    "generate_skill_candidate",
    "explain_conflict",
    "generate_handoff",
]


class LLMEnhanceError(LLMError):
    """Raised when an enhancement helper cannot produce a usable result."""


# ── JSON parsing helpers ────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_json_response(text: str, *, expected_top: type | tuple[type, ...] = dict) -> Any:
    """Parse an LLM response that should be JSON.

    Strips ```json``` fences if present, then attempts ``json.loads``. Raises
    :class:`LLMEnhanceError` with the offending text on any failure or when
    the top-level type does not match ``expected_top``.
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMEnhanceError("empty LLM response")
    cleaned = _FENCE_RE.sub("", text).strip()
    # If a fenced block lives inside surrounding chatter, try to isolate it.
    if not cleaned.startswith(("{", "[")):
        # Heuristic: take the substring from the first { or [ to the matching last } or ]
        first = min((cleaned.find(c) for c in "{[" if cleaned.find(c) != -1), default=-1)
        last_obj = max(cleaned.rfind("}"), cleaned.rfind("]"))
        if first != -1 and last_obj > first:
            cleaned = cleaned[first : last_obj + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMEnhanceError(f"LLM response is not valid JSON: {exc}; got: {text[:200]!r}") from exc
    if not isinstance(parsed, expected_top):
        raise LLMEnhanceError(
            f"LLM response top-level type {type(parsed).__name__} does not match "
            f"expected {expected_top!r}; got: {text[:200]!r}"
        )
    return parsed


def _require_keys(payload: dict[str, Any], keys: Sequence[str], *, op: str) -> None:
    missing = [k for k in keys if k not in payload]
    if missing:
        raise LLMEnhanceError(f"{op}: LLM response missing required keys: {missing}")


def _llm_json_call(
    client: LLMClient,
    *,
    system: str,
    user: str,
    max_tokens: int | None,
    thinking: bool | None,
    reasoning_effort: str | None,
    op: str,
    expected_top: type | tuple[type, ...] = dict,
) -> tuple[Any, dict[str, Any]]:
    """Run a single chat completion that must return JSON, return (parsed, meta)."""
    pre_prompt = client.total_prompt_tokens
    pre_completion = client.total_completion_tokens
    pre_cost = client.total_estimated_cost_cny
    try:
        text = client.complete_text(
            user,
            system=system,
            max_tokens=max_tokens,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
        )
    except LLMError as exc:
        raise LLMEnhanceError(f"{op}: LLM call failed: {exc}") from exc
    parsed = _parse_json_response(text, expected_top=expected_top)
    meta = {
        "model": client.config.model,
        "usage_delta": {
            "prompt_tokens": client.total_prompt_tokens - pre_prompt,
            "completion_tokens": client.total_completion_tokens - pre_completion,
            "estimated_cost_cny": round(client.total_estimated_cost_cny - pre_cost, 6),
        },
    }
    return parsed, meta


# ── 1. classify_record ──────────────────────────────────────────────────────

CLASSIFY_SYSTEM_PROMPT = (
    "You are a memory triage assistant. Given a free-form note, classify it for"
    " a structured memory store. Reply ONLY with a single compact JSON object."
    " Fields: record_kind (one of the allowed kinds), scope (one of the allowed"
    " scopes), tags (array of strings, only from allowed_tags, may be empty),"
    " confidence (0..1 float), rationale (short string, <=200 chars)."
)


def classify_record(
    client: LLMClient,
    *,
    content: str,
    allowed_kinds: Sequence[str],
    allowed_scopes: Sequence[str],
    allowed_tags: Sequence[str],
    max_tokens: int | None = 400,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise LLMEnhanceError("classify_record: content is empty")
    if not allowed_kinds or not allowed_scopes:
        raise LLMEnhanceError("classify_record: allowed_kinds and allowed_scopes must be non-empty")
    user = (
        "allowed_kinds = " + json.dumps(sorted(set(allowed_kinds))) + "\n"
        "allowed_scopes = " + json.dumps(sorted(set(allowed_scopes))) + "\n"
        "allowed_tags = " + json.dumps(sorted(set(allowed_tags))) + "\n\n"
        "content:\n" + content
    )
    parsed, meta = _llm_json_call(
        client,
        system=CLASSIFY_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="classify_record",
    )
    _require_keys(parsed, ["record_kind", "scope", "tags", "confidence"], op="classify_record")
    kind = str(parsed["record_kind"])
    scope = str(parsed["scope"])
    if kind not in set(allowed_kinds):
        raise LLMEnhanceError(f"classify_record: kind {kind!r} not in allowed_kinds")
    if scope not in set(allowed_scopes):
        raise LLMEnhanceError(f"classify_record: scope {scope!r} not in allowed_scopes")
    raw_tags = parsed.get("tags") or []
    if not isinstance(raw_tags, list):
        raise LLMEnhanceError("classify_record: tags must be an array")
    allowed_tags_set = set(allowed_tags)
    tags = [str(t) for t in raw_tags if str(t) in allowed_tags_set]
    try:
        confidence = float(parsed["confidence"])
    except (TypeError, ValueError) as exc:
        raise LLMEnhanceError(f"classify_record: confidence not a number: {exc}") from exc
    confidence = max(0.0, min(1.0, confidence))
    return {
        "ok": True,
        "record_kind": kind,
        "scope": scope,
        "tags": tags,
        "confidence": confidence,
        "rationale": str(parsed.get("rationale") or "")[:500],
        **meta,
    }


# ── 2. extract_candidates ───────────────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = (
    "You extract claim/rule candidates from a free-form note for a structured"
    " memory store. Reply ONLY with a JSON object {\"candidates\": [...]} where"
    " each candidate has: kind ('claim_candidate' or 'rule_candidate'),"
    " content_markdown (concise statement, MUST start with a level-1 heading),"
    " confidence (0..1 float), tags (array, optional), rationale (short string)."
    " If nothing meaningful can be extracted, return {\"candidates\": []}."
)


def extract_candidates(
    client: LLMClient,
    *,
    content: str,
    source_record_id: str | None = None,
    max_tokens: int | None = 1024,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise LLMEnhanceError("extract_candidates: content is empty")
    user = "content:\n" + content
    if source_record_id:
        user = f"source_record_id = {source_record_id!r}\n\n" + user
    parsed, meta = _llm_json_call(
        client,
        system=EXTRACT_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="extract_candidates",
    )
    _require_keys(parsed, ["candidates"], op="extract_candidates")
    raw_list = parsed["candidates"]
    if not isinstance(raw_list, list):
        raise LLMEnhanceError("extract_candidates: candidates must be an array")
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise LLMEnhanceError(f"extract_candidates: candidates[{i}] is not an object")
        kind = str(raw.get("kind") or "")
        if kind not in {"claim_candidate", "rule_candidate"}:
            raise LLMEnhanceError(f"extract_candidates: candidates[{i}].kind {kind!r} invalid")
        content_md = str(raw.get("content_markdown") or "").strip()
        if not content_md:
            raise LLMEnhanceError(f"extract_candidates: candidates[{i}].content_markdown empty")
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        tags = [str(t) for t in (raw.get("tags") or []) if isinstance(t, (str, int, float))]
        out.append(
            {
                "kind": kind,
                "content_markdown": content_md,
                "confidence": conf,
                "tags": tags,
                "rationale": str(raw.get("rationale") or "")[:500],
                "source_record_id": source_record_id,
            }
        )
    return {"ok": True, "candidates": out, **meta}


# ── 3. merge_candidates ─────────────────────────────────────────────────────

MERGE_SYSTEM_PROMPT = (
    "You deduplicate and merge similar candidate records. Input is a JSON list"
    " of candidates each with id and content_markdown. Reply ONLY with a JSON"
    " object {\"groups\": [...]} where each group has: candidate_ids (array of"
    " input ids that should be merged), merged_content (markdown that subsumes"
    " them, MUST start with a level-1 heading), rationale (short string)."
    " Singletons (no duplicate) MUST also appear as a group of length 1 so the"
    " output partitions the input."
)


def merge_candidates(
    client: LLMClient,
    *,
    candidates: Sequence[dict[str, Any]],
    max_tokens: int | None = 2048,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if not candidates:
        raise LLMEnhanceError("merge_candidates: candidates is empty")
    cleaned: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise LLMEnhanceError(f"merge_candidates: candidates[{i}] is not an object")
        cid = str(raw.get("id") or raw.get("record_id") or f"cand-{i}")
        if cid in seen_ids:
            raise LLMEnhanceError(f"merge_candidates: duplicate candidate id {cid!r}")
        seen_ids.add(cid)
        body = str(raw.get("content_markdown") or raw.get("content") or "").strip()
        if not body:
            raise LLMEnhanceError(f"merge_candidates: candidates[{i}].content_markdown empty")
        cleaned.append({"id": cid, "content_markdown": body})
    user = "candidates = " + json.dumps(cleaned, ensure_ascii=False)
    parsed, meta = _llm_json_call(
        client,
        system=MERGE_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="merge_candidates",
    )
    _require_keys(parsed, ["groups"], op="merge_candidates")
    raw_groups = parsed["groups"]
    if not isinstance(raw_groups, list) or not raw_groups:
        raise LLMEnhanceError("merge_candidates: groups must be a non-empty array")
    out_groups: list[dict[str, Any]] = []
    covered: set[str] = set()
    valid_ids = {c["id"] for c in cleaned}
    for i, g in enumerate(raw_groups):
        if not isinstance(g, dict):
            raise LLMEnhanceError(f"merge_candidates: groups[{i}] is not an object")
        ids = g.get("candidate_ids") or []
        if not isinstance(ids, list) or not ids:
            raise LLMEnhanceError(f"merge_candidates: groups[{i}].candidate_ids must be non-empty")
        ids_norm: list[str] = []
        for cid in ids:
            scid = str(cid)
            if scid not in valid_ids:
                raise LLMEnhanceError(f"merge_candidates: groups[{i}] references unknown id {scid!r}")
            if scid in covered:
                raise LLMEnhanceError(f"merge_candidates: id {scid!r} appears in multiple groups")
            covered.add(scid)
            ids_norm.append(scid)
        merged = str(g.get("merged_content") or "").strip()
        if not merged:
            raise LLMEnhanceError(f"merge_candidates: groups[{i}].merged_content empty")
        out_groups.append(
            {
                "candidate_ids": ids_norm,
                "merged_content": merged,
                "rationale": str(g.get("rationale") or "")[:500],
            }
        )
    if covered != valid_ids:
        missing = sorted(valid_ids - covered)
        raise LLMEnhanceError(f"merge_candidates: groups do not partition input; missing {missing}")
    return {"ok": True, "groups": out_groups, **meta}


# ── 4. generate_skill_candidate ─────────────────────────────────────────────

SKILL_SYSTEM_PROMPT = (
    "You distil a reusable skill / procedure from a list of related observation"
    " records. Reply ONLY with a JSON object containing: title (short string),"
    " content_markdown (full procedure starting with a level-1 heading and"
    " including a Steps section), tags (array, optional), confidence (0..1"
    " float), rationale (short string). The output will be persisted as"
    " record_kind='skill_candidate' for human validation."
)


def _records_to_text(records: Iterable[dict[str, Any]], *, max_chars_per_record: int = 4000) -> tuple[str, int]:
    parts: list[str] = []
    n = 0
    for r in records:
        if not isinstance(r, dict):
            raise LLMEnhanceError("records must be a sequence of dicts")
        rid = str(r.get("id") or r.get("record_id") or f"r-{n}")
        body = str(r.get("content_markdown") or r.get("content") or "")
        if len(body) > max_chars_per_record:
            body = body[:max_chars_per_record] + "\n...[truncated]"
        parts.append(f"## record {rid}\n{body}")
        n += 1
    if n == 0:
        raise LLMEnhanceError("records must be non-empty")
    return "\n\n".join(parts), n


def generate_skill_candidate(
    client: LLMClient,
    *,
    records: Sequence[dict[str, Any]],
    max_tokens: int | None = 2048,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    max_chars_per_record: int = 4000,
) -> dict[str, Any]:
    body, n = _records_to_text(records, max_chars_per_record=max_chars_per_record)
    user = f"There are {n} related records below. Distil a reusable skill.\n\n" + body
    parsed, meta = _llm_json_call(
        client,
        system=SKILL_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="generate_skill_candidate",
    )
    _require_keys(parsed, ["title", "content_markdown", "confidence"], op="generate_skill_candidate")
    title = str(parsed["title"]).strip()
    content_md = str(parsed["content_markdown"]).strip()
    if not title or not content_md:
        raise LLMEnhanceError("generate_skill_candidate: title or content_markdown empty")
    try:
        conf = max(0.0, min(1.0, float(parsed["confidence"])))
    except (TypeError, ValueError) as exc:
        raise LLMEnhanceError(f"generate_skill_candidate: confidence not a number: {exc}") from exc
    tags = [str(t) for t in (parsed.get("tags") or []) if isinstance(t, (str, int, float))]
    return {
        "ok": True,
        "title": title,
        "content_markdown": content_md,
        "tags": tags,
        "confidence": conf,
        "rationale": str(parsed.get("rationale") or "")[:500],
        "source_record_count": n,
        **meta,
    }


# ── 5. explain_conflict ─────────────────────────────────────────────────────

CONFLICT_SYSTEM_PROMPT = (
    "You analyse two records that may conflict. Reply ONLY with a JSON object"
    " containing: conflict_type ('contradiction'|'overlap'|'scope_mismatch'|"
    "'no_conflict'|'unclear'), severity ('low'|'medium'|'high'), explanation"
    " (short paragraph), resolution_options (array of short strings, may be"
    " empty when no_conflict)."
)


def explain_conflict(
    client: LLMClient,
    *,
    record_a: dict[str, Any],
    record_b: dict[str, Any],
    max_tokens: int | None = 1024,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    if not isinstance(record_a, dict) or not isinstance(record_b, dict):
        raise LLMEnhanceError("explain_conflict: record_a and record_b must be objects")
    body_a = str(record_a.get("content_markdown") or record_a.get("content") or "").strip()
    body_b = str(record_b.get("content_markdown") or record_b.get("content") or "").strip()
    if not body_a or not body_b:
        raise LLMEnhanceError("explain_conflict: both records must have content")
    id_a = str(record_a.get("id") or record_a.get("record_id") or "A")
    id_b = str(record_b.get("id") or record_b.get("record_id") or "B")
    user = (
        f"## record {id_a}\n{body_a}\n\n## record {id_b}\n{body_b}\n\n"
        "Identify whether these records conflict and how to resolve."
    )
    parsed, meta = _llm_json_call(
        client,
        system=CONFLICT_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="explain_conflict",
    )
    _require_keys(parsed, ["conflict_type", "severity", "explanation"], op="explain_conflict")
    ct = str(parsed["conflict_type"])
    if ct not in {"contradiction", "overlap", "scope_mismatch", "no_conflict", "unclear"}:
        raise LLMEnhanceError(f"explain_conflict: invalid conflict_type {ct!r}")
    sev = str(parsed["severity"]).lower()
    if sev not in {"low", "medium", "high"}:
        raise LLMEnhanceError(f"explain_conflict: invalid severity {sev!r}")
    options = parsed.get("resolution_options") or []
    if not isinstance(options, list):
        raise LLMEnhanceError("explain_conflict: resolution_options must be an array")
    return {
        "ok": True,
        "record_ids": [id_a, id_b],
        "conflict_type": ct,
        "severity": sev,
        "explanation": str(parsed["explanation"])[:2000],
        "resolution_options": [str(o)[:300] for o in options],
        **meta,
    }


# ── 6. generate_handoff ─────────────────────────────────────────────────────

HANDOFF_SYSTEM_PROMPT = (
    "You produce a handoff note for the next session. Reply ONLY with a JSON"
    " object containing: summary_markdown (level-1 heading + concise summary),"
    " key_points (array of short strings, 3-7 items), open_questions (array of"
    " strings), next_actions (array of short imperative strings)."
)


def generate_handoff(
    client: LLMClient,
    *,
    records: Sequence[dict[str, Any]],
    task_id: str | None = None,
    branch: str | None = None,
    max_tokens: int | None = 2048,
    thinking: bool | None = None,
    reasoning_effort: str | None = None,
    max_chars_per_record: int = 4000,
) -> dict[str, Any]:
    body, n = _records_to_text(records, max_chars_per_record=max_chars_per_record)
    header = []
    if task_id:
        header.append(f"task_id = {task_id!r}")
    if branch:
        header.append(f"branch = {branch!r}")
    user = "\n".join(header)
    if user:
        user += "\n\n"
    user += f"There are {n} session records below. Produce a handoff for the next session.\n\n" + body
    parsed, meta = _llm_json_call(
        client,
        system=HANDOFF_SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
        op="generate_handoff",
    )
    _require_keys(
        parsed,
        ["summary_markdown", "key_points", "open_questions", "next_actions"],
        op="generate_handoff",
    )
    summary = str(parsed["summary_markdown"]).strip()
    if not summary:
        raise LLMEnhanceError("generate_handoff: summary_markdown empty")
    def _str_list(key: str) -> list[str]:
        v = parsed.get(key) or []
        if not isinstance(v, list):
            raise LLMEnhanceError(f"generate_handoff: {key} must be an array")
        return [str(x).strip() for x in v if str(x).strip()]
    return {
        "ok": True,
        "task_id": task_id,
        "branch": branch,
        "summary_markdown": summary,
        "key_points": _str_list("key_points"),
        "open_questions": _str_list("open_questions"),
        "next_actions": _str_list("next_actions"),
        "source_record_count": n,
        **meta,
    }
