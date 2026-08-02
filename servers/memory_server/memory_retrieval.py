from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from .memory_budget import (  # P1-D: shared budget primitives
    IMPORTANT_MEMORY_DEFAULT_MAX_CHARS,
    IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS,
    IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS,
    IMPORTANT_MEMORY_FALLBACK_BODY,
    IMPORTANT_MEMORY_MIN_BODY_CHARS,
    fit_text_to_budget as _fit_text_to_budget,
    validate_budget_inputs as _validate_budget_inputs,
)
from .memory_compiler import load_compile_cache_entries
from .memory_config import MemoryConfig
from .memory_identity import canonical_identity
from .memory_corpus import CompilableRecord, compact_body as _compact_body, iter_compilable_records as _iter_records
from .memory_events import append_event
from .memory_lineage import memory_list_conflicts
from .memory_paths import PathSecurityError
from .memory_record_index import prefilter_record_paths
from .memory_result import error_result, ok_result
from .memory_scoring import build_reference_counts, load_usage_stats, parse_timestamp, score_record
from .memory_task_context import get_task_ids_for_user
from .memory_vector_search import vector_search
from .token_estimator import estimate_tokens

DEFAULT_RETRIEVAL_SCOPES = ["shared", "personal", "session", "task_or_branch", "project_shared", "org_shared"]


# ── P5 Phase 2b — vector supplement tunables (see DesignDoc §15.4) ────
#
# ⚠️  The vector tier (`memory_vector_search`) is FROZEN at v0.11.1
# (DesignDoc §15.5).  This supplement is opt-in and a no-op when
# `embeddings.enabled=False` (default).  Do not add new tuning knobs
# unless a §15.5 activation threshold is hit.
#
# Conservative numbers so the FTS ranking continues to dominate.  These
# are not user-facing config keys yet; once we have ONNX recall data we
# can promote them to MemoryConfig.
_VECTOR_RECALL_MIN_SCORE = 0.20  # below this, treat as "no semantic match"
_VECTOR_SUPPLEMENT_WEIGHT = 0.25  # weight when ONLY the vector tier hit
_VECTOR_SUPPLEMENT_BOOST = 0.10   # additive boost when both FTS and vector hit
_VECTOR_SUPPLEMENT_CEILING = 0.5  # absolute cap to keep FTS dominant
_VECTOR_SUPPLEMENT_TOP_K = 50     # how many vector neighbours we look at


def _vector_supplement(
    config: MemoryConfig,
    query: str | None,
    records: list[CompilableRecord],
) -> dict[str, float]:
    """Run the optional vector tier and project hits back to ``record_id``.

    Returns ``{record_id: best_chunk_score}`` for records present in the
    candidate set.  Always safe to call: missing index, disabled tier, or
    any embedding failure yields an empty mapping so the caller's ranking
    falls back to FTS-only behaviour (per \u00a715.4.1 "\u53ef\u9009 + \u53ef\u964d\u7ea7").
    """

    if not getattr(config, "embeddings_enabled", False):
        return {}
    if not isinstance(query, str) or not query.strip():
        return {}
    candidate_ids = {str(r.metadata.get("id", "")) for r in records}
    if not candidate_ids:
        return {}
    try:
        result = vector_search(config, query, top_k=_VECTOR_SUPPLEMENT_TOP_K)
    except Exception as exc:
        # \u00a715.1-D: never silently swallow \u2014 leave a breadcrumb so health
        # surface can show how often the optional tier is being skipped.
        try:
            append_event(
                config,
                "vector_supplement_skipped",
                {
                    "reason": f"{type(exc).__name__}: {exc}",
                    "query_preview": query[:80],
                },
                status="warn",
            )
        except Exception:
            # Event logging is best-effort \u2014 never let it mask the original
            # silent-degrade contract of the vector tier.
            pass
        return {}
    if not result.get("ok"):
        try:
            append_event(
                config,
                "vector_supplement_skipped",
                {
                    "reason": str(result.get("error") or result.get("status") or "vector_search_not_ok"),
                    "query_preview": query[:80],
                },
                status="warn",
            )
        except Exception:
            pass
        return {}
    best: dict[str, float] = {}
    for hit in result.get("hits", []):
        rid = str(hit.get("record_id", ""))
        if rid not in candidate_ids:
            continue
        score = float(hit.get("score", 0.0))
        if score > best.get(rid, 0.0):
            best[rid] = score
    return best


def _normalize_list(value: list[str] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _record_time(record: CompilableRecord) -> datetime | None:
    for key in ("occurred_at", "valid_from", "updated_at", "created_at"):
        parsed = parse_timestamp(record.metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_time_sort_value(record: CompilableRecord) -> datetime:
    return _record_time(record) or datetime.min.replace(tzinfo=timezone.utc)


def _is_private_record_visible_to_user(
    record: CompilableRecord,
    *,
    user: str | None,
    user_task_ids: set[str],
) -> bool:
    if not user:
        return True
    metadata = record.metadata
    if canonical_identity(metadata.get("author")) == canonical_identity(user):
        return True
    task_id = str(metadata.get("task_id") or "").strip()
    return bool(task_id and task_id in user_task_ids)


def _text_blob(record: CompilableRecord) -> str:
    metadata = record.metadata
    parts = [
        record.title,
        record.body,
        str(metadata.get("record_kind", "")),
        str(metadata.get("scope", "")),
        str(metadata.get("status", "")),
        str(metadata.get("system_area", "")),
        " ".join(str(item) for item in metadata.get("tags", []) if str(item)),
        " ".join(str(item) for item in metadata.get("source_refs", []) if str(item)),
    ]
    return "\n".join(parts).lower()


_QUERY_TOKEN_RE = re.compile(r"[a-z0-9_+.#/-]+|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
_WEAK_QUERY_TERMS = {
    "ue",
    "ue5",
    "unreal",
    "engine",
    "cpp",
    "c++",
    "mcp",
    "memory",
    "记忆",
    "项目",
    "系统",
    "任务",
    "开发",
    "继续开发",
    "复核",
    "验证",
    "给出",
    "出下",
    "下一",
    "一步",
    "步代",
    "代码",
    "码和",
    "验收入口",
    "continue",
    "develop",
    "review",
    "verify",
    "validate",
    "validation",
    "code",
    "next",
    "step",
    "acceptance",
    "entry",
    "implement",
    "implementation",
}

_MEMORY_META_MARKERS = (
    "memory mcp",
    "memory system",
    "memory retrieval",
    "memory recall",
    "memory governance",
    "memory reflection",
    "task brief",
    "retrieval evaluation",
    "recall evaluation",
    "context engineering",
    "记忆mcp",
    "记忆系统",
    "记忆检索",
    "记忆召回",
    "记忆治理",
    "记忆反思",
    "任务简报",
    "检索评估",
    "召回评估",
    "上下文工程",
)


def _query_role(query: str | None) -> str:
    """区分记忆系统自身查询和业务领域查询，避免自评记录污染业务事实。"""
    normalized = str(query or "").casefold().replace("-", " ").replace("_", " ")
    context_words = r"mcp|context|brief|retrieval|retrieve|recall|authority|map|routing|governance|reflection|agent|persistent|project"
    is_memory_query = bool(
        re.search(rf"\bmemory\b.{{0,48}}\b(?:{context_words})\b", normalized)
        or re.search(rf"\b(?:{context_words})\b.{{0,48}}\bmemory\b", normalized)
    ) or "记忆" in normalized
    return "memory_meta" if is_memory_query or any(marker in normalized for marker in _MEMORY_META_MARKERS) else "domain"


def _record_memory_role(record: CompilableRecord) -> str:
    descriptor = "\n".join(
        [record.title, str(record.metadata.get("system_area") or "")]
    ).casefold().replace("-", " ").replace("_", " ")
    if any(marker in descriptor for marker in _MEMORY_META_MARKERS):
        return "memory_meta"
    refs = [str(item).replace("\\", "/").casefold() for item in record.metadata.get("source_refs", []) or []]
    if refs and all(ref.startswith("mcp/memory/") or "/mcp/memory/" in ref for ref in refs):
        return "memory_meta"
    return "domain"


def _role_alignment(query_role: str, memory_role: str) -> int:
    if query_role == "memory_meta":
        return 2 if memory_role == "memory_meta" else 1
    return 2 if memory_role == "domain" else 0


def _query_terms(query: str | None) -> list[str]:
    if not query:
        return []
    terms: list[str] = []
    for token in _QUERY_TOKEN_RE.findall(query.casefold()):
        value = token.strip("-_/.")
        if value and value not in terms:
            terms.append(value)
        if value and re.fullmatch(r"[\u4e00-\u9fff]+", value) and len(value) >= 4:
            # 无空格中文任务描述不能被当成一个超长精确词；有限 bigram 与 FTS
            # 的 CJK 路径保持一致，同时限制数量避免长 prompt 放大 CPU 成本。
            for index in range(min(len(value) - 1, 24)):
                gram = value[index : index + 2]
                if gram not in terms:
                    terms.append(gram)
    return terms


def _query_match_score_v1(record: CompilableRecord, query: str | None) -> float:
    if not query:
        return 0.0
    terms = [term for term in re.split(r"\s+", query.lower().strip()) if term]
    if not terms:
        return 0.0
    blob = _text_blob(record)
    hits = sum(1 for term in terms if term in blob)
    if hits == 0:
        return -1.0
    title_bonus = 0.2 if any(term in record.title.lower() for term in terms) else 0.0
    return min(0.4, hits / len(terms) * 0.25 + title_bonus)


def _query_match_score(record: CompilableRecord, query: str | None) -> float:
    """返回 0..1 的词法相关性；强查询词覆盖率优先于通用项目词。"""
    terms = _query_terms(query)
    if not terms:
        return 0.0
    title = record.title.casefold()
    body = record.body.casefold()
    system_area = str(record.metadata.get("system_area", "")).casefold()
    metadata_blob = "\n".join(
        [
            str(record.metadata.get("record_kind", "")),
            " ".join(str(item) for item in record.metadata.get("tags", []) if str(item)),
            " ".join(str(item) for item in record.metadata.get("source_refs", []) if str(item)),
        ]
    ).casefold()
    fields = (title, system_area, body, metadata_blob)
    hits = [term for term in terms if any(term in field for field in fields)]
    if not hits:
        return -1.0

    strong = [term for term in terms if term not in _WEAK_QUERY_TERMS and len(term) > 1]
    strong_hits = [term for term in strong if term in hits]
    weak_hits = [term for term in hits if term not in strong]
    if strong:
        coverage = len(strong_hits) / len(strong)
        if not strong_hits:
            return min(0.18, 0.04 + 0.03 * len(weak_hits))
        # 长任务通常包含很多限定词。旧公式给任意单个强词固定 0.42 起分，
        # 会让只命中 chunk/验证/代码的泛记录全部进入 band 2。这里同时考虑
        # 绝对命中数和覆盖率；单词查询仍可凭 100% 覆盖进入 band 2。
        hit_strength = min(1.0, len(strong_hits) / 6.0)
        title_coverage = sum(1 for term in strong_hits if term in title) / len(strong_hits)
        area_coverage = sum(1 for term in strong_hits if term in system_area) / len(strong_hits)
        phrase_bonus = 0.08 if query and query.casefold().strip() in "\n".join(fields) else 0.0
        return min(
            1.0,
            0.05
            + hit_strength * 0.55
            + coverage * 0.20
            + title_coverage * 0.08
            + area_coverage * 0.04
            + phrase_bonus,
        )

    coverage = len(hits) / len(terms)
    title_bonus = 0.05 if any(term in title for term in hits) else 0.0
    return min(0.22, 0.05 + coverage * 0.12 + title_bonus)


def _relevance_band(match_score: float) -> int:
    if match_score >= 0.75:
        return 3
    if match_score >= 0.35:
        return 2
    if match_score > 0:
        return 1
    return 0


def _matches_facets(
    record: CompilableRecord,
    *,
    system_area: str | None,
    facet_filters: dict[str, list[str]],
) -> bool:
    if system_area and str(record.metadata.get("system_area", "")) != system_area:
        return False
    for field, expected_values in facet_filters.items():
        if not expected_values:
            continue
        current = record.metadata.get(field)
        if not isinstance(current, list):
            return False
        current_values = {str(item) for item in current if str(item).strip()}
        if not current_values.intersection(expected_values):
            return False
    return True


def _facet_boost(
    record: CompilableRecord,
    *,
    system_area: str | None,
    facet_filters: dict[str, list[str]],
) -> float:
    metadata = record.metadata
    boost = 0.0
    if system_area:
        expected = system_area.casefold()
        current = str(metadata.get("system_area", "")).casefold()
        if expected == current:
            boost += 0.08
        elif expected in current or current in expected:
            boost += 0.04
    for field, expected_values in facet_filters.items():
        if not expected_values:
            continue
        current = metadata.get(field)
        if not isinstance(current, list):
            continue
        current_values = {str(item).casefold() for item in current if str(item).strip()}
        expected = {str(item).casefold() for item in expected_values if str(item).strip()}
        if current_values.intersection(expected):
            boost += 0.03
    return min(boost, 0.2)


def _summary(record: CompilableRecord, score_data: dict[str, Any], *, include_body: bool = False) -> dict[str, Any]:
    result = {
        "id": str(record.metadata.get("id", "")),
        "title": record.title,
        "path": record.path,
        "record_kind": record.metadata.get("record_kind"),
        "scope": record.metadata.get("scope"),
        "status": record.metadata.get("status"),
        "cognitive_level": record.metadata.get("cognitive_level"),
        "memory_tier": score_data.get("effective_memory_tier"),
        "importance_score": score_data.get("total"),
        "system_area": record.metadata.get("system_area"),
        "relevance_band": score_data.get("relevance_band"),
        "memory_role": score_data.get("memory_role"),
        "query_role": score_data.get("query_role"),
        "role_alignment": score_data.get("role_alignment"),
        "collapsed_best_record_id": score_data.get("collapsed_best_record_id"),
        "collapsed_record_ids": [
            str(item) for item in score_data.get("collapsed_record_ids", []) if str(item).strip()
        ],
    }
    if include_body:
        result["body"] = _compact_body(record)
    return result


def _parse_window(
    window_start: str | None,
    window_end: str | None,
) -> tuple[datetime | None, datetime | None] | dict[str, Any]:
    parsed_start = parse_timestamp(window_start) if window_start else None
    parsed_end = parse_timestamp(window_end) if window_end else None
    if window_start and parsed_start is None:
        return error_result("invalid_input", f"invalid window_start: {window_start}")
    if window_end and parsed_end is None:
        return error_result("invalid_input", f"invalid window_end: {window_end}")
    if parsed_start and parsed_end and parsed_start > parsed_end:
        return error_result("invalid_input", "window_start must be <= window_end")
    return parsed_start, parsed_end


def _selected_text(item: dict[str, Any]) -> str:
    return "\n".join(
        [
            item.get("title", ""),
            item.get("body", ""),
            " ".join(str(reason) for reason in item.get("reason_selected", []) if str(reason).strip()),
        ]
    ).strip()


def _selection_reasons(record: CompilableRecord, score_data: dict[str, Any], match_score: float) -> list[str]:
    metadata = record.metadata
    reasons: list[str] = []
    if match_score > 0:
        reasons.append("matched_query")
    kind = str(metadata.get("record_kind", "")).strip()
    if kind:
        reasons.append(f"kind:{kind}")
    level = str(metadata.get("cognitive_level", "")).strip()
    if level in {"dao", "fa", "shu"}:
        reasons.append(f"level:{level}")
    if float(score_data.get("total", 0.0)) >= 0.6:
        reasons.append("high_importance")
    if int(score_data.get("usage", {}).get("compile_hit_count", 0) or 0) > 0:
        reasons.append("recently_reused")
    return reasons[:4]


def _build_memory_item(
    record: CompilableRecord,
    score_data: dict[str, Any],
    *,
    match_score: float,
    body_text: str,
    rank: int,
    degraded: bool,
) -> dict[str, Any]:
    metadata = record.metadata
    body = body_text.strip()
    text_for_budget = "\n".join(part for part in (record.title, body) if part).strip()
    return {
        "id": str(metadata.get("id", "")),
        "title": record.title,
        "path": record.path,
        "record_kind": metadata.get("record_kind"),
        "scope": metadata.get("scope"),
        "status": metadata.get("status"),
        "cognitive_level": metadata.get("cognitive_level"),
        "memory_tier": score_data.get("effective_memory_tier"),
        "importance_score": score_data.get("total"),
        "system_area": metadata.get("system_area"),
        # 任务简报等服务端装配器需要这些字段做任务级聚合与标签排序。
        # MCP facade 仍会通过 _compact_memory_item 裁掉非必要字段，因此不扩大
        # 默认外部响应，也不绕过这里已经完成的用户/范围可见性过滤。
        "task_id": metadata.get("task_id"),
        "branch": metadata.get("branch"),
        "author": metadata.get("author"),
        "tags": [str(item) for item in metadata.get("tags", []) if str(item).strip()],
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "occurred_at": metadata.get("occurred_at"),
        "valid_from": metadata.get("valid_from"),
        "valid_to": metadata.get("valid_to"),
        "conflicts_with": [str(item) for item in metadata.get("conflicts_with", []) if str(item).strip()],
        "body": body,
        "reason_selected": _selection_reasons(record, score_data, match_score),
        "source_refs": [str(item) for item in metadata.get("source_refs", []) if str(item).strip()],
        "related_artifact_ids": [
            str(item) for item in metadata.get("related_artifact_ids", []) if str(item).strip()
        ],
        "query_match_score": round(match_score, 4),
        "relevance_band": score_data.get("relevance_band"),
        "memory_role": score_data.get("memory_role"),
        "query_role": score_data.get("query_role"),
        "role_alignment": score_data.get("role_alignment"),
        "collapsed_best_record_id": score_data.get("collapsed_best_record_id"),
        "collapsed_record_ids": [
            str(item) for item in score_data.get("collapsed_record_ids", []) if str(item).strip()
        ],
        "rank": rank,
        "degraded": degraded,
        "chars": len(text_for_budget),
        "tokens_est": estimate_tokens(text_for_budget),
    }


def _collect_records(
    config: MemoryConfig,
    *,
    user: str | None,
    task_id: str | None,
    branch: str | None,
    include_scopes: list[str] | None,
    include_statuses: list[str] | None,
    preferred_tags: list[str] | None,
    window_start: str | None,
    window_end: str | None,
    system_area: str | None,
    asset_paths: list[str] | None,
    map_names: list[str] | None,
    plugin_names: list[str] | None,
    module_names: list[str] | None,
    class_names: list[str] | None,
    blueprint_paths: list[str] | None,
    facet_mode: str = "hard",
) -> dict[str, Any]:
    if facet_mode not in {"hard", "boost"}:
        return error_result("invalid_input", "facet_mode must be 'hard' or 'boost'")
    window = _parse_window(window_start, window_end)
    if isinstance(window, dict):
        return window
    parsed_start, parsed_end = window

    scopes_list = [str(item) for item in (include_scopes or DEFAULT_RETRIEVAL_SCOPES)]
    statuses_list = [str(item) for item in (include_statuses or ["raw", "candidate", "validated", "published", "degraded"])]
    facet_filters = {
        "asset_paths": _normalize_list(asset_paths),
        "map_names": _normalize_list(map_names),
        "plugin_names": _normalize_list(plugin_names),
        "module_names": _normalize_list(module_names),
        "class_names": _normalize_list(class_names),
        "blueprint_paths": _normalize_list(blueprint_paths),
    }

    include_rel_paths: set[str] | None = None
    # boost 模式仅把 facet 作为排序信号，不能在索引层提前删除精确命中。
    hard_system_area = system_area if facet_mode == "hard" else None
    hard_facet_filters = facet_filters if facet_mode == "hard" else {key: [] for key in facet_filters}
    prefilter = prefilter_record_paths(
        config,
        include_scopes=scopes_list,
        include_statuses=statuses_list,
        user=user,
        task_id=task_id,
        branch=branch,
        system_area=hard_system_area,
        facet_filters=hard_facet_filters,
    )
    if prefilter.get("ok"):
        include_rel_paths = {str(path) for path in prefilter.get("paths", [])}
        prefilter_stats = {
            "enabled": True,
            "candidate_paths": len(include_rel_paths),
            **(prefilter.get("stats") or {}),
        }
    else:
        prefilter_stats = {
            "enabled": False,
            "fallback_reason": prefilter.get("error"),
            "message": prefilter.get("message"),
        }

    try:
        records, scan_stats = _iter_records(config, include_rel_paths=include_rel_paths)
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))

    scopes = set(scopes_list)
    statuses = set(statuses_list)
    tags = set(_normalize_list(preferred_tags))
    # Author isolation: user-scoped recall must not surface another user's
    # personal, session, or schema v2 private records.
    private_scopes = {"personal", "session", "user_private"}
    user_task_ids = get_task_ids_for_user(config, user)
    scoped = [
        record
        for record in records
        if str(record.metadata.get("scope", "")) in scopes
        and str(record.metadata.get("status", "")) in statuses
        and (
            str(record.metadata.get("scope", "")) not in private_scopes
            or _is_private_record_visible_to_user(record, user=user, user_task_ids=user_task_ids)
        )
        and (not task_id or record.metadata.get("task_id") in (None, task_id))
        and (not branch or record.metadata.get("branch") in (None, branch))
        and (not tags or tags.intersection({str(item) for item in record.metadata.get("tags", []) if str(item)}))
    ]

    time_filtered = []
    for record in scoped:
        timestamp = _record_time(record)
        if parsed_start and (timestamp is None or timestamp < parsed_start):
            continue
        if parsed_end and (timestamp is None or timestamp > parsed_end):
            continue
        time_filtered.append(record)

    if facet_mode == "hard":
        facet_filtered = [
            record
            for record in time_filtered
            if _matches_facets(record, system_area=system_area, facet_filters=facet_filters)
        ]
    else:
        facet_filtered = list(time_filtered)
    return ok_result(
        "records collected",
        records=records,
        scan_stats=scan_stats,
        prefilter_stats=prefilter_stats,
        scoped=scoped,
        time_filtered=time_filtered,
        facet_filtered=facet_filtered,
        facet_filters=facet_filters,
        system_area=system_area,
        facet_mode=facet_mode,
    )


def _rank_records(
    config: MemoryConfig,
    *,
    records: list[CompilableRecord],
    corpus_records: list[CompilableRecord],
    query: str | None,
    extra_queries: list[str] | None = None,
    ranking_version: str = "v2",
    facet_mode: str = "hard",
    system_area: str | None = None,
    facet_filters: dict[str, list[str]] | None = None,
) -> list[tuple[CompilableRecord, dict[str, Any], float, float]]:
    if ranking_version not in {"v1", "v2"}:
        raise ValueError("ranking_version must be 'v1' or 'v2'")
    # P5 Phase 2b: optional vector supplement (§15.4).  Only consulted when
    # the user has explicitly opted into the embedding tier; any failure
    # downgrades silently to FTS-only ranking so the main path stays alive.
    vector_recall = _vector_supplement(config, query, records)

    # v0.10.0 — query_rewrite supplement.  ``extra_queries`` holds variants
    # produced by :mod:`memory_query_rewrite`.  We score each variant the
    # same way as the primary query and take the per-record maximum so a
    # synonym hit can rescue a record that the original phrasing missed.
    variant_queries = [q for q in (extra_queries or []) if isinstance(q, str) and q.strip()]

    scored_candidates: list[tuple[CompilableRecord, float]] = []
    for record in records:
        scorer = _query_match_score_v1 if ranking_version == "v1" else _query_match_score
        primary_score = scorer(record, query)
        match_score = primary_score
        for variant in variant_queries:
            variant_score = scorer(record, variant)
            if variant_score > match_score:
                match_score = variant_score
        record_id = str(record.metadata.get("id", ""))
        vec_score = vector_recall.get(record_id, 0.0)
        if match_score < 0:
            # No lexical hit; promote into the candidate set only when the
            # vector tier produced a meaningful similarity.
            if vec_score >= _VECTOR_RECALL_MIN_SCORE:
                match_score = vec_score * _VECTOR_SUPPLEMENT_WEIGHT
        elif vec_score >= _VECTOR_RECALL_MIN_SCORE:
            # Lexical hit AND vector hit → small additive boost.  Capped so
            # the FTS signal still dominates ranking.
            match_score = min(
                match_score + vec_score * _VECTOR_SUPPLEMENT_BOOST,
                _VECTOR_SUPPLEMENT_CEILING,
            )
        if match_score >= 0 and ranking_version == "v2" and facet_mode == "boost":
            match_score = min(
                1.0,
                match_score
                + _facet_boost(
                    record,
                    system_area=system_area,
                    facet_filters=facet_filters or {},
                ),
            )
        scored_candidates.append((record, match_score))

    superseded_ids: set[str] = set()
    superseded_lineage: dict[str, set[str]] = {}
    if ranking_version == "v2":
        score_by_id = {
            str(record.metadata.get("id") or ""): score
            for record, score in scored_candidates
            if str(record.metadata.get("id") or "")
        }
        # 新记录即使没有复述旧记录的关键词，也继承被替代记录的检索命中；旧记录
        # 随后隐藏，避免 supersede 后召回到过时结论。
        for _pass in range(max(1, len(scored_candidates))):
            changed = False
            for record, _score in scored_candidates:
                record_id = str(record.metadata.get("id") or "")
                for target_id in (str(item) for item in record.metadata.get("supersedes", []) or []):
                    if target_id not in score_by_id or record_id not in score_by_id:
                        continue
                    superseded_ids.add(target_id)
                    superseded_lineage.setdefault(record_id, {record_id}).update(
                        superseded_lineage.get(target_id, {target_id})
                    )
                    if score_by_id[target_id] > score_by_id[record_id]:
                        score_by_id[record_id] = score_by_id[target_id]
                        changed = True
            if not changed:
                break
        scored_candidates = [
            (record, score_by_id.get(str(record.metadata.get("id") or ""), score))
            for record, score in scored_candidates
        ]
    recall = [
        (record, match_score)
        for record, match_score in scored_candidates
        if match_score >= 0 and str(record.metadata.get("id") or "") not in superseded_ids
    ]

    intent_role = _query_role(query) if ranking_version == "v2" else None
    usage_stats = load_usage_stats(config)
    reference_counts = build_reference_counts(corpus_records)
    now = datetime.now(timezone.utc)
    ranked: list[tuple[CompilableRecord, dict[str, Any], float, float]] = []
    for record, match_score in recall:
        record_id = str(record.metadata.get("id", ""))
        score_data = score_record(
            record.metadata,
            usage_entry=usage_stats.get(record_id, {}),
            reference_count=reference_counts.get(record_id, 0),
            now=now,
        )
        combined = float(score_data.get("total", 0.0)) + match_score
        score_data["relevance_band"] = _relevance_band(match_score) if ranking_version == "v2" else None
        if ranking_version == "v2":
            memory_role = _record_memory_role(record)
            score_data["memory_role"] = memory_role
            score_data["query_role"] = intent_role
            score_data["role_alignment"] = _role_alignment(str(intent_role), memory_role)
        if ranking_version == "v2" and record_id in superseded_lineage:
            score_data["collapsed_record_ids"] = sorted(superseded_lineage[record_id])
        ranked.append((record, score_data, match_score, combined))
    if ranking_version == "v1":
        ranked.sort(key=lambda item: (-item[3], item[0].title.lower(), str(item[0].metadata.get("id", ""))))
    else:
        # 相关性分带是第一序：精确任务事实不能被通用但高重要度的项目记录压过。
        ranked.sort(
            key=lambda item: (
                -int(item[1].get("role_alignment", 0) or 0),
                -int(item[1].get("relevance_band", 0) or 0),
                -item[2],
                -float(item[1].get("total", 0.0)),
                item[0].title.lower(),
                str(item[0].metadata.get("id", "")),
            )
        )
    return ranked


def _canonical_body_key(record: CompilableRecord) -> str | None:
    body = re.sub(r"\s+", " ", record.body).strip().casefold()
    if not body:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _collapse_ranked_records(
    ranked: list[tuple[CompilableRecord, dict[str, Any], float, float]],
) -> list[tuple[CompilableRecord, dict[str, Any], float, float]]:
    """合并跨 scope 镜像与显式 supersede 链，保留完整可审计 ID。"""
    if len(ranked) < 2:
        return ranked
    by_id = {str(item[0].metadata.get("id", "")): item for item in ranked if item[0].metadata.get("id")}
    parent = {record_id: record_id for record_id in by_id}

    def find(record_id: str) -> str:
        while parent[record_id] != record_id:
            parent[record_id] = parent[parent[record_id]]
            record_id = parent[record_id]
        return record_id

    def union(left: str, right: str) -> None:
        if left not in parent or right not in parent:
            return
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    body_owners: dict[str, list[str]] = {}
    for record_id, item in by_id.items():
        record = item[0]
        metadata = record.metadata
        # 自动团队沉淀是个人记录的共享镜像；只折叠这种明确 lineage，避免把
        # background_reflection 与其原始证据错误合并。
        if str(metadata.get("provenance", "")) == "auto_team_settlement":
            for source_id in metadata.get("derived_from_record_ids", []) or []:
                union(record_id, str(source_id))
        for target_id in metadata.get("supersedes", []) or []:
            union(record_id, str(target_id))
        body_key = _canonical_body_key(record)
        if body_key:
            owners = body_owners.setdefault(body_key, [])
            current_scope = str(metadata.get("scope") or "")
            current_task = str(metadata.get("task_id") or "")
            for owner in owners:
                owner_metadata = by_id[owner][0].metadata
                if (
                    str(owner_metadata.get("scope") or "") != current_scope
                    or (current_task and str(owner_metadata.get("task_id") or "") == current_task)
                ):
                    union(record_id, owner)
                    break
            owners.append(record_id)

    groups: dict[str, list[str]] = {}
    for record_id in by_id:
        groups.setdefault(find(record_id), []).append(record_id)
    if all(len(group) == 1 for group in groups.values()):
        return ranked

    order = {str(item[0].metadata.get("id", "")): index for index, item in enumerate(ranked)}
    collapsed: list[tuple[int, tuple[CompilableRecord, dict[str, Any], float, float]]] = []
    for member_ids in groups.values():
        group_ids = set(member_ids)
        superseding = [
            record_id
            for record_id in member_ids
            if group_ids.intersection(str(item) for item in by_id[record_id][0].metadata.get("supersedes", []) or [])
        ]
        if superseding:
            representative_id = min(superseding, key=lambda record_id: order[record_id])
        else:
            representative_id = min(
                member_ids,
                key=lambda record_id: (
                    0 if str(by_id[record_id][0].metadata.get("scope", "")) == "project_shared" else 1,
                    order[record_id],
                ),
            )
        representative_record, representative_score, _representative_match, _representative_combined = by_id[representative_id]
        best_id = min(member_ids, key=lambda record_id: order[record_id])
        best_item = by_id[best_id]
        group_match = max(float(by_id[record_id][2]) for record_id in member_ids)
        group_score = dict(representative_score)
        group_score["relevance_band"] = max(
            int(by_id[record_id][1].get("relevance_band", 0) or 0) for record_id in member_ids
        )
        group_score["role_alignment"] = max(
            int(by_id[record_id][1].get("role_alignment", 0) or 0) for record_id in member_ids
        )
        group_score["query_role"] = best_item[1].get("query_role")
        group_score["memory_role"] = best_item[1].get("memory_role")
        group_score["collapsed_best_record_id"] = best_id
        group_ids.update(
            str(item)
            for item in group_score.get("collapsed_record_ids", [])
            if str(item)
        )
        group_score["collapsed_record_ids"] = sorted(group_ids)
        representative = (
            representative_record,
            group_score,
            group_match,
            float(group_score.get("total", 0.0)) + group_match,
        )
        collapsed.append((min(order[record_id] for record_id in member_ids), representative))

    collapsed.sort(key=lambda item: item[0])
    return [item for _index, item in collapsed]


def _rank_latest_records(
    config: MemoryConfig,
    *,
    records: list[CompilableRecord],
    corpus_records: list[CompilableRecord],
) -> list[tuple[CompilableRecord, dict[str, Any], float, float]]:
    usage_stats = load_usage_stats(config)
    reference_counts = build_reference_counts(corpus_records)
    now = datetime.now(timezone.utc)
    ranked: list[tuple[CompilableRecord, dict[str, Any], float, float]] = []
    for record in records:
        record_id = str(record.metadata.get("id", ""))
        score_data = score_record(
            record.metadata,
            usage_entry=usage_stats.get(record_id, {}),
            reference_count=reference_counts.get(record_id, 0),
            now=now,
        )
        ranked.append((record, score_data, 0.0, float(score_data.get("total", 0.0))))
    ranked.sort(
        key=lambda item: (
            -_record_time_sort_value(item[0]).timestamp(),
            -float(item[1].get("total", 0.0)),
            item[0].title.lower(),
            str(item[0].metadata.get("id", "")),
        )
    )
    return ranked


def _pack_ranked_records(
    ranked: list[tuple[CompilableRecord, dict[str, Any], float, float]],
    *,
    max_chars: int | None,
    max_tokens: int | None,
    max_items: int | None,
    default_items: int,
) -> tuple[
    list[tuple[CompilableRecord, dict[str, Any]]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    effective_max_items = max_items or default_items
    selected_pairs: list[tuple[CompilableRecord, dict[str, Any]]] = []
    important_memories: list[dict[str, Any]] = []
    dropped_candidates: list[dict[str, Any]] = []
    used_chars = 0
    used_tokens = 0

    def remember_drop(record: CompilableRecord, score_data: dict[str, Any], reason: str) -> None:
        if len(dropped_candidates) >= 10:
            return
        dropped_candidates.append(
            {
                "id": str(record.metadata.get("id", "")),
                "title": record.title,
                "path": record.path,
                "importance_score": score_data.get("total"),
                "drop_reason": reason,
            }
        )

    for rank, (record, score_data, match_score, _combined) in enumerate(ranked, start=1):
        if effective_max_items is not None and len(important_memories) >= effective_max_items:
            remember_drop(record, score_data, "max_items_reached")
            continue

        remaining_chars = None if max_chars is None else max_chars - used_chars
        remaining_tokens = None if max_tokens is None else max_tokens - used_tokens
        if remaining_chars is not None and remaining_chars <= 0:
            remember_drop(record, score_data, "max_chars_reached")
            continue
        if remaining_tokens is not None and remaining_tokens <= 0:
            remember_drop(record, score_data, "max_tokens_reached")
            continue

        body_text = _compact_body(record)
        fitted_body, degraded = _fit_text_to_budget(
            body_text,
            remaining_chars=remaining_chars,
            remaining_tokens=remaining_tokens,
        )
        if not fitted_body and remaining_chars != 0 and remaining_tokens != 0:
            fitted_body, degraded = _fit_text_to_budget(
                IMPORTANT_MEMORY_FALLBACK_BODY,
                remaining_chars=remaining_chars,
                remaining_tokens=remaining_tokens,
            )

        if not fitted_body:
            remember_drop(record, score_data, "budget_exhausted")
            continue
        if (
            len(fitted_body) < IMPORTANT_MEMORY_MIN_BODY_CHARS
            and len(body_text.strip()) >= IMPORTANT_MEMORY_MIN_BODY_CHARS
            and fitted_body != IMPORTANT_MEMORY_FALLBACK_BODY
        ):
            remember_drop(record, score_data, "insufficient_body_budget")
            continue

        item = _build_memory_item(
            record,
            score_data,
            match_score=match_score,
            body_text=fitted_body,
            rank=rank,
            degraded=degraded,
        )
        item_text = _selected_text(item)
        item_chars = len(item_text)
        item_tokens = estimate_tokens(item_text)
        if max_chars is not None and used_chars + item_chars > max_chars:
            remember_drop(record, score_data, "max_chars_reached")
            continue
        if max_tokens is not None and used_tokens + item_tokens > max_tokens:
            remember_drop(record, score_data, "max_tokens_reached")
            continue

        item["chars"] = item_chars
        item["tokens_est"] = item_tokens
        important_memories.append(item)
        selected_pairs.append((record, score_data))
        used_chars += item_chars
        used_tokens += item_tokens

    budget_report = {
        "max_chars": max_chars,
        "max_tokens": max_tokens,
        "max_items": effective_max_items,
        "used_chars": used_chars,
        "used_tokens_est": used_tokens,
        "used_items": len(important_memories),
        "dropped_candidates": len(dropped_candidates),
    }
    return selected_pairs, important_memories, dropped_candidates, budget_report


def _next_steps(records: list[tuple[CompilableRecord, dict[str, Any]]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for record, score_data in records:
        body = record.body
        lowered = body.lower()
        if "next step" not in lowered and "next steps" not in lowered:
            continue
        lines = [line.strip("- ").strip() for line in body.splitlines() if line.strip().startswith("-")]
        steps.append(
            {
                "record": _summary(record, score_data),
                "steps": lines[:5] if lines else [_compact_body(record)],
            }
        )
        if len(steps) >= 5:
            break
    return steps


def _recent_snapshots(config: MemoryConfig, *, limit: int = 5) -> list[dict[str, Any]]:
    entries = load_compile_cache_entries(config, targets={"daily_snapshot", "weekly_snapshot", "monthly_snapshot"})
    entries.sort(key=lambda item: str(item.get("window_end", "")), reverse=True)
    return [
        {
            "snapshot_id": entry.get("snapshot_id"),
            "target": entry.get("target"),
            "path": entry.get("path"),
            "window_start": entry.get("window_start"),
            "window_end": entry.get("window_end"),
            "record_count": len(entry.get("included_record_ids", []) or []),
        }
        for entry in entries[:limit]
    ]


def _section_summary(record: CompilableRecord, score_data: dict[str, Any]) -> dict[str, Any]:
    result = _summary(record, score_data, include_body=False)
    result["context_item_id"] = str(record.metadata.get("id", ""))
    return result


def memory_get_important_memories(
    config: MemoryConfig,
    *,
    query: str | None = None,
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    include_scopes: list[str] | None = None,
    include_statuses: list[str] | None = None,
    preferred_tags: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    system_area: str | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    top_k: int | None = None,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    max_items: int | None = None,
    query_variants: list[str] | None = None,
    facet_mode: str = "hard",
    ranking_version: str = "v2",
) -> dict[str, Any]:
    if ranking_version not in {"v1", "v2"}:
        return error_result("invalid_input", "ranking_version must be 'v1' or 'v2'")
    budget_error = _validate_budget_inputs(max_chars=max_chars, max_tokens=max_tokens, max_items=max_items)
    if budget_error:
        return budget_error

    effective_max_chars = max_chars if max_chars is not None else IMPORTANT_MEMORY_DEFAULT_MAX_CHARS
    effective_max_tokens = max_tokens if max_tokens is not None else IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS
    effective_max_items = max_items if max_items is not None else (top_k or IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS)

    collected = _collect_records(
        config,
        user=user,
        task_id=task_id,
        branch=branch,
        include_scopes=include_scopes,
        include_statuses=include_statuses,
        preferred_tags=preferred_tags,
        window_start=window_start,
        window_end=window_end,
        system_area=system_area,
        asset_paths=asset_paths,
        map_names=map_names,
        plugin_names=plugin_names,
        module_names=module_names,
        class_names=class_names,
        blueprint_paths=blueprint_paths,
        facet_mode=facet_mode,
    )
    if not collected.get("ok"):
        return collected

    records = collected["records"]
    facet_filtered = collected["facet_filtered"]
    ranking_used = ranking_version
    ranking_fallback: str | None = None
    try:
        ranked = _rank_records(
            config,
            records=facet_filtered,
            corpus_records=records,
            query=query,
            extra_queries=query_variants,
            ranking_version=ranking_version,
            facet_mode=facet_mode,
            system_area=system_area,
            facet_filters=collected["facet_filters"],
        )
    except Exception as exc:  # pragma: no cover - defensive base-service fence
        if ranking_version != "v2":
            return error_result("ranking_failed", str(exc))
        ranking_used = "v1"
        ranking_fallback = f"v2_failed:{type(exc).__name__}"
        ranked = _rank_records(
            config,
            records=facet_filtered,
            corpus_records=records,
            query=query,
            extra_queries=query_variants,
            ranking_version="v1",
        )
    recalled_count = len(ranked)
    if ranking_used == "v2":
        ranked = _collapse_ranked_records(ranked)
    selected_pairs, important_memories, dropped_candidates, budget_report = _pack_ranked_records(
        ranked,
        max_chars=effective_max_chars,
        max_tokens=effective_max_tokens,
        max_items=effective_max_items,
        default_items=IMPORTANT_MEMORY_DEFAULT_MAX_ITEMS,
    )

    evidence_refs = sorted(
        {
            ref
            for item in important_memories
            for ref in (
                # P1-E: aggregate every observable provenance signal so callers
                # can audit the digest without re-fetching each record.
                *(str(r).strip() for r in item.get("source_refs", [])),
                *(str(r).strip() for r in item.get("related_artifact_ids", [])),
                str(item.get("path") or "").strip(),
                str(item.get("id") or "").strip(),
                *(str(r).strip() for r in item.get("collapsed_record_ids", [])),
            )
            if ref
        }
    )
    suggested_externalization = [
        {
            "id": item["id"],
            "title": item["title"],
            "importance_score": item["importance_score"],
            "reason": "stable_high_value_memory",
        }
        for item in important_memories
        if float(item.get("importance_score", 0.0) or 0.0) >= 0.6
        and str(item.get("status", "")) in {"validated", "published"}
    ]

    return ok_result(
        "important memories retrieved",
        query=query,
        important_memories=important_memories,
        evidence_refs=evidence_refs,
        suggested_externalization=suggested_externalization,
        dropped_candidates=dropped_candidates,
        budget_report=budget_report,
        selected_records=[_summary(record, score_data) for record, score_data in selected_pairs],
        pipeline={
            "scope_filter": len(collected["scoped"]),
            "time_window_filter": len(collected["time_filtered"]),
            "facet_filter": len(facet_filtered),
            "metadata_fts_recall": recalled_count,
            "canonical_collapse": len(ranked),
            "importance_rerank": len(ranked),
            "budget_first_packing": len(important_memories),
            "facet_mode": facet_mode,
            "ranking_version": ranking_used,
            "ranking_fallback": ranking_fallback,
        },
        stats={
            **collected["scan_stats"],
            "prefilter": collected["prefilter_stats"],
            "returned_records": len(important_memories),
        },
    )


def memory_get_latest_memories(
    config: MemoryConfig,
    *,
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    include_scopes: list[str] | None = None,
    include_statuses: list[str] | None = None,
    preferred_tags: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    system_area: str | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    top_k: int | None = None,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    max_items: int | None = None,
) -> dict[str, Any]:
    limit = top_k or 10
    if limit <= 0:
        return error_result("invalid_input", "top_k must be >= 1")
    budget_error = _validate_budget_inputs(max_chars=max_chars, max_tokens=max_tokens, max_items=max_items)
    if budget_error:
        return budget_error

    collected = _collect_records(
        config,
        user=user,
        task_id=task_id,
        branch=branch,
        include_scopes=include_scopes,
        include_statuses=include_statuses,
        preferred_tags=preferred_tags,
        window_start=window_start,
        window_end=window_end,
        system_area=system_area,
        asset_paths=asset_paths,
        map_names=map_names,
        plugin_names=plugin_names,
        module_names=module_names,
        class_names=class_names,
        blueprint_paths=blueprint_paths,
    )
    if not collected.get("ok"):
        return collected

    records = collected["records"]
    facet_filtered = collected["facet_filtered"]
    ranked = _rank_latest_records(config, records=facet_filtered, corpus_records=records)
    effective_max_chars = max_chars if max_chars is not None else IMPORTANT_MEMORY_DEFAULT_MAX_CHARS
    effective_max_tokens = max_tokens if max_tokens is not None else IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS
    effective_max_items = max_items if max_items is not None else limit
    selected, latest_memories, dropped_candidates, budget_report = _pack_ranked_records(
        ranked,
        max_chars=effective_max_chars,
        max_tokens=effective_max_tokens,
        max_items=effective_max_items,
        default_items=limit,
    )
    for item, (record, _score_data) in zip(latest_memories, selected):
        timestamp = _record_time(record)
        if timestamp is not None:
            item["timestamp"] = timestamp.isoformat()

    return ok_result(
        "latest memories retrieved",
        latest_memories=latest_memories,
        dropped_candidates=dropped_candidates,
        budget_report=budget_report,
        selected_records=[_summary(record, score_data) for record, score_data in selected],
        pipeline={
            "scope_filter": len(collected["scoped"]),
            "time_window_filter": len(collected["time_filtered"]),
            "facet_filter": len(facet_filtered),
            "recency_sort": len(ranked),
            "budget_first_packing": len(latest_memories),
        },
        stats={
            **collected["scan_stats"],
            "prefilter": collected["prefilter_stats"],
            "returned_records": len(latest_memories),
        },
    )


def memory_retrieve_context(
    config: MemoryConfig,
    *,
    query: str | None = None,
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    include_scopes: list[str] | None = None,
    include_statuses: list[str] | None = None,
    preferred_tags: list[str] | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    system_area: str | None = None,
    asset_paths: list[str] | None = None,
    map_names: list[str] | None = None,
    plugin_names: list[str] | None = None,
    module_names: list[str] | None = None,
    class_names: list[str] | None = None,
    blueprint_paths: list[str] | None = None,
    top_k: int | None = None,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    max_items: int | None = None,
    query_variants: list[str] | None = None,
    facet_mode: str = "hard",
    ranking_version: str = "v2",
) -> dict[str, Any]:
    if ranking_version not in {"v1", "v2"}:
        return error_result("invalid_input", "ranking_version must be 'v1' or 'v2'")
    limit = top_k or 10
    if limit <= 0:
        return error_result("invalid_input", "top_k must be >= 1")
    budget_error = _validate_budget_inputs(max_chars=max_chars, max_tokens=max_tokens, max_items=max_items)
    if budget_error:
        return budget_error

    collected = _collect_records(
        config,
        user=user,
        task_id=task_id,
        branch=branch,
        include_scopes=include_scopes,
        include_statuses=include_statuses,
        preferred_tags=preferred_tags,
        window_start=window_start,
        window_end=window_end,
        system_area=system_area,
        asset_paths=asset_paths,
        map_names=map_names,
        plugin_names=plugin_names,
        module_names=module_names,
        class_names=class_names,
        blueprint_paths=blueprint_paths,
        facet_mode=facet_mode,
    )
    if not collected.get("ok"):
        return collected

    records = collected["records"]
    facet_filtered = collected["facet_filtered"]
    ranking_used = ranking_version
    ranking_fallback: str | None = None
    try:
        ranked = _rank_records(
            config,
            records=facet_filtered,
            corpus_records=records,
            query=query,
            extra_queries=query_variants,
            ranking_version=ranking_version,
            facet_mode=facet_mode,
            system_area=system_area,
            facet_filters=collected["facet_filters"],
        )
    except Exception as exc:  # pragma: no cover - defensive base-service fence
        if ranking_version != "v2":
            return error_result("ranking_failed", str(exc))
        ranking_used = "v1"
        ranking_fallback = f"v2_failed:{type(exc).__name__}"
        ranked = _rank_records(
            config,
            records=facet_filtered,
            corpus_records=records,
            query=query,
            extra_queries=query_variants,
            ranking_version="v1",
        )
    recalled_count = len(ranked)
    if ranking_used == "v2":
        ranked = _collapse_ranked_records(ranked)
    effective_max_chars = max_chars if max_chars is not None else IMPORTANT_MEMORY_DEFAULT_MAX_CHARS
    effective_max_tokens = max_tokens if max_tokens is not None else IMPORTANT_MEMORY_DEFAULT_MAX_TOKENS
    effective_max_items = max_items if max_items is not None else limit
    selected, context_items, dropped_candidates, budget_report = _pack_ranked_records(
        ranked,
        max_chars=effective_max_chars,
        max_tokens=effective_max_tokens,
        max_items=effective_max_items,
        default_items=limit,
    )

    core_constraints = [
        _section_summary(record, score_data)
        for record, score_data in selected
        if str(record.metadata.get("cognitive_level", "")) in {"dao", "fa"}
        or str(record.metadata.get("record_kind", "")) in {"decision", "system_rule"}
    ][:limit]
    relevant_rules = [
        _section_summary(record, score_data)
        for record, score_data in selected
        if str(record.metadata.get("record_kind", "")) in {"decision", "procedure", "system_rule"}
    ][:limit]
    key_evidence = [
        _section_summary(record, score_data)
        for record, score_data in selected
        if str(record.metadata.get("record_kind", "")) in {"observation", "incident", "note", "event"}
    ][:limit]
    selected_ids = {str(record.metadata.get("id", "")) for record, _score_data in selected}
    open_conflicts = []
    selected_have_conflicts = any(record.metadata.get("conflicts_with") for record, _score_data in selected)
    if selected_have_conflicts:
        conflicts_result = memory_list_conflicts(config)
        if conflicts_result.get("ok"):
            for conflict in conflicts_result.get("conflicts", []):
                ids = {
                    str(conflict.get("source", {}).get("id", "")),
                    str(conflict.get("target", {}).get("id", "")),
                }
                if not selected_ids or selected_ids.intersection(ids):
                    open_conflicts.append(conflict)
    evidence_refs = sorted(
        {
            ref
            for item in context_items
            for ref in (
                *(str(r).strip() for r in item.get("source_refs", [])),
                *(str(r).strip() for r in item.get("related_artifact_ids", [])),
                str(item.get("path") or "").strip(),
                str(item.get("id") or "").strip(),
                *(str(r).strip() for r in item.get("collapsed_record_ids", [])),
            )
            if ref
        }
    )

    return ok_result(
        "context retrieved",
        query=query,
        context_items=context_items,
        core_constraints=core_constraints,
        relevant_rules=relevant_rules,
        recent_snapshots=_recent_snapshots(config),
        key_evidence=key_evidence,
        open_conflicts=open_conflicts[:limit],
        next_steps=_next_steps(selected),
        evidence_refs=evidence_refs,
        dropped_candidates=dropped_candidates,
        budget_report=budget_report,
        selected_records=[_summary(record, score_data) for record, score_data in selected],
        pipeline={
            "scope_filter": len(collected["scoped"]),
            "time_window_filter": len(collected["time_filtered"]),
            "facet_filter": len(facet_filtered),
            "metadata_fts_recall": recalled_count,
            "canonical_collapse": len(ranked),
            "importance_rerank": len(ranked),
            "budget_first_packing": len(context_items),
            "context_assembly": len(selected),
            "facet_mode": facet_mode,
            "ranking_version": ranking_used,
            "ranking_fallback": ranking_fallback,
        },
        stats={
            **collected["scan_stats"],
            "prefilter": collected["prefilter_stats"],
            "returned_records": len(context_items),
        },
    )
