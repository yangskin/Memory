"""Tool schema builder for the memory MCP server.

Extracted from `server.py` (P1-A). Pure schema construction; no dispatch.
The tool list returned by `_build_tools` is what FastMCP advertises to
clients. The default MCP surface contains general memory read/write tools and
dedicated Project Board read/write tools. Maintenance/admin schemas remain
here for historical reference, but CLI is the supported surface for those flows.
"""

from __future__ import annotations

from mcp.types import Tool

from .memory_config import DEFAULT_ALLOWED_TAGS, MemoryConfig
from .server_descriptions import _BASE_DESCRIPTIONS


def _build_file_roles(config: MemoryConfig) -> str:
    """Build a dynamic file-roles suffix from config guard targets."""
    roles = []
    for t in config.guard_targets:
        if t.role:
            roles.append(f"{t.path} ({t.role})")
        else:
            roles.append(t.path)
    if roles:
        return " Available memory files: " + "; ".join(roles) + "."
    return ""


def _tag_schema(allowed_tags: list[str] | tuple[str, ...] | None = None) -> dict:
    tags = sorted(set(allowed_tags or DEFAULT_ALLOWED_TAGS))
    return {
        "type": "array",
        "items": {
            "type": "string",
            "enum": tags,
        },
        "description": (
            "Controlled vocabulary only. Put business-domain words, asset names, "
            "module names, and feature names in system_area or typed metadata "
            "fields instead of tags. Omit tags when unsure."
        ),
    }


def _build_facade_tools(
    file_roles: str,
    path_hint: str,
    allowed_tags: list[str] | tuple[str, ...] | None = None,
) -> list[Tool]:
    tag_schema = _tag_schema(allowed_tags)
    return [
        Tool(
            name="memory_read",
            description=_BASE_DESCRIPTIONS["memory_read"] + file_roles,
            inputSchema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": [
                            "task_context",
                            "task_brief",
                            "get",
                            "search",
                            "search_records",
                            "board",
                            "runtime_digest",
                            "retrieve_context",
                            "important_memories",
                            "latest_memories",
                            "shared_context",
                        ],
                        "default": "task_context",
                        "description": "Read operation to perform.",
                    },
                    "context_token": {
                        "type": "string",
                        "description": "Task context token returned by memory_read(operation=task_context). Injects current user/branch and returns task attribution; recall only filters by task_id when task_id is explicitly passed.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "operation=task_context: caller identity such as codex, copilot, cursor-agent.",
                    },
                    "client_session_id": {
                        "type": "string",
                        "description": "operation=task_context: stable client chat/session id when available.",
                    },
                    "client_name": {"type": "string"},
                    "client_version": {"type": "string"},
                    "workspace_id": {"type": "string"},
                    "roots": {"type": "array", "items": {"type": "string"}},
                    "active_files": {"type": "array", "items": {"type": "string"}},
                    "user_goal": {"type": "string"},
                    "external_ref": {"type": "string"},
                    "include_task_brief": {
                        "type": "boolean",
                        "default": True,
                        "description": "operation=task_context: attach the fixed-slot task brief. Brief failure never fails task_context.",
                    },
                    "brief_mode": {
                        "type": "string",
                        "enum": ["compact", "standard", "deep"],
                        "default": "standard",
                    },
                    "brief_use_llm": {
                        "type": "boolean",
                        "default": True,
                        "description": "Use the configured generate_task_brief LLM capability; missing/failed LLM always falls back to deterministic tag-aware generation.",
                    },
                    "brief_recent_days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 90,
                        "default": 14,
                    },
                    "board_max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 8,
                        "description": "operation=task_context: maximum unresolved board items to inject.",
                    },
                    "board_max_tokens": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 2000,
                        "default": 500,
                        "description": "operation=task_context: token budget for injected unresolved board items.",
                    },
                    "brief_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Bypass the frozen task-brief snapshot and rebuild the intent/authority map from current sources.",
                    },
                    "brief_skill_catalog": {
                        "type": "array",
                        "description": "Optional host skill index. Only name/description/path metadata is used; full skill bodies stay on-demand.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "path": {"type": "string"},
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                    "path": {
                        "type": "string",
                        "description": f"Target file path for operation=get. Recommended: {path_hint}.",
                    },
                    "query": {"type": "string", "description": "Search query for search operations."},
                    "action": {
                        "type": "string",
                        "enum": ["query", "post", "reply", "resolve"],
                        "description": "Board action selector. Read path supports query only. task_context automatically injects unresolved task items.",
                    },
                    "filter": {
                        "type": "string",
                        "enum": ["all", "unresolved"],
                        "description": "operation=board query filter.",
                    },
                    "user_id": {"type": "string", "description": "operation=board filter by author user id."},
                    "agent_instance_id": {"type": "string", "description": "operation=board filter by author agent instance id."},
                    "post_type": {
                        "type": "string",
                        "enum": ["note", "question", "request", "warning", "handoff", "proposal", "reply"],
                        "description": "operation=board filter by post type.",
                    },
                    "thread_id": {"type": "string", "description": "operation=board filter by thread id."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "max_chars": {"type": "integer", "minimum": 0, "maximum": 32000},
                    "scopes": {"type": "array", "items": {"type": "string"}},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "include_paths": {"type": "array", "items": {"type": "string"}},
                    "exclude_paths": {"type": "array", "items": {"type": "string"}},
                    "user": {"type": "string"},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "include_scopes": {"type": "array", "items": {"type": "string"}},
                    "include_statuses": {"type": "array", "items": {"type": "string"}},
                    "preferred_tags": {"type": "array", "items": {"type": "string"}},
                    "facet_mode": {
                        "type": "string",
                        "enum": ["hard", "boost"],
                        "default": "hard",
                        "description": "Retrieval facets either filter strictly or contribute a ranking boost.",
                    },
                    "ranking_version": {
                        "type": "string",
                        "enum": ["v1", "v2"],
                        "default": "v2",
                        "description": "v2 uses relevance bands and canonical collapse; v1 is the rollback path.",
                    },
                    "max_tokens": {"type": "integer", "minimum": 0, "maximum": 8000},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
                    "window_start": {"type": "string"},
                    "window_end": {"type": "string"},
                    "system_area": {"type": "string"},
                    "asset_paths": {"type": "array", "items": {"type": "string"}},
                    "map_names": {"type": "array", "items": {"type": "string"}},
                    "plugin_names": {"type": "array", "items": {"type": "string"}},
                    "module_names": {"type": "array", "items": {"type": "string"}},
                    "class_names": {"type": "array", "items": {"type": "string"}},
                    "blueprint_paths": {"type": "array", "items": {"type": "string"}},
                    "summarize": {
                        "type": "boolean",
                        "default": False,
                        "description": "operation=retrieve_context: optionally attach an LLM summary when configured.",
                    },
                    "summary_query": {"type": "string"},
                    "summary_max_tokens": {"type": "integer", "minimum": 1, "maximum": 2000},
                    "summary_max_chars_per_record": {"type": "integer", "minimum": 256, "maximum": 8000, "default": 4000},
                    "rewrite_query": {"type": "boolean", "default": False},
                    "rewrite_max_variants": {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
                    "rewrite_context_hint": {"type": "string"},
                    "include_diagnostics": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Return retrieval diagnostics, pipeline stats, provenance lists, "
                            "and other non-context debug fields. Default false keeps MCP responses compact."
                        ),
                    },
                    "include_shared_context": {
                        "type": "boolean",
                        "default": False,
                        "description": "operation=retrieve_context: include optional cached or remote shared Hub context.",
                    },
                    "include": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["user_brief", "project_brief", "same_task_agents", "my_other_agents", "other_tasks", "project_activity"]},
                    },
                    "max_age_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                    "force_refresh": {"type": "boolean", "default": False},
                    "llm_suggest_metadata": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Opt-in for operation=task_context. When an LLM is configured, attach "
                            "suggested_metadata with in-vocabulary record_kind/scope/tags guidance."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_write",
            description=_BASE_DESCRIPTIONS["memory_write"] + file_roles,
            inputSchema={
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["record", "observation", "checkpoint", "board"],
                        "default": "record",
                        "description": "Write operation to perform. MCP writes are structured memory only; use CLI for file/admin maintenance.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["query", "post", "reply", "resolve"],
                        "description": (
                            "operation=board action selector. Board messages are best-effort advisory coordination and must never "
                            "gate local work or cause the agent to wait for a reply. Post blockers, open questions, handoffs, or "
                            "cross-agent risks when useful; reply when useful; resolve after the outcome is locally observed or "
                            "validated. Continue safely when the service is unavailable. Do not post routine progress noise."
                        ),
                    },
                    "context_token": {
                        "type": "string",
                        "description": "Task context token returned by memory_read(operation=task_context). Injects author/task_id/branch and returns task_context metadata.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Alias for content_markdown when writing record/observation entries. If provided with operation=checkpoint, the body is persisted as a structured record and the response includes a warning.",
                    },
                    "content_markdown": {
                        "type": "string",
                        "description": "Markdown body for record/observation writes. If provided with operation=checkpoint, the body is persisted as a structured record and the response includes a warning.",
                    },
                    "schema_version": {"type": "string", "enum": ["1.0", "2.0"]},
                    "record_kind": {"type": "string"},
                    "post_type": {
                        "type": "string",
                        "enum": ["note", "question", "request", "warning", "handoff", "proposal", "reply"],
                        "description": "operation=board post type. Use question/request/warning/handoff for required collaboration; use reply via action=reply.",
                    },
                    "thread_id": {"type": "string", "description": "operation=board optional thread id; omit or send blank for a new post."},
                    "reply_to": {"type": "string", "description": "operation=board optional parent post id for reply; blank is treated as omitted."},
                    "post_id": {"type": "string", "description": "operation=board target post id for resolve."},
                    "expires_at": {"type": "string", "description": "operation=board optional expiration timestamp (ISO-8601)."},
                    "references_json": {
                        "type": "array",
                        "description": "operation=board optional references payload.",
                        "items": {"type": ["string", "number", "boolean", "object", "array", "null"]},
                    },
                    "scope": {"type": "string"},
                    "status": {"type": "string"},
                    "author": {"type": "string"},
                    "tags": tag_schema,
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "task_phase": {
                        "type": "string",
                        "enum": [
                            "exploration",
                            "plan_confirmed",
                            "implementation",
                            "test_failed",
                            "test_passed",
                            "stable_pattern_found",
                            "task_done",
                        ],
                        "description": "Agent checkpoint phase. With operation=checkpoint, can trigger automatic key-document settling without waiting for the write-count threshold. Prefer writing summaries with operation=record first; checkpoint content is accepted for compatibility, persisted as a structured record, and returned with a warning.",
                    },
                    "validated_by": {"type": "string"},
                    "classifier_model": {"type": "string"},
                    "classifier_prompt_version": {"type": "string"},
                    "tag_schema_version": {"type": "string"},
                    "occurred_at": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                    "memory_tier": {"type": "string", "enum": ["hot", "warm", "cold", "fossil"]},
                    "cognitive_level": {"type": "string", "enum": ["dao", "fa", "shu"]},
                    "derived_from_record_ids": {"type": "array", "items": {"type": "string"}},
                    "derived_from_snapshot_ids": {"type": "array", "items": {"type": "string"}},
                    "derived_from_revision_ids": {"type": "array", "items": {"type": "string"}},
                    "supersedes": {"type": "array", "items": {"type": "string"}},
                    "conflicts_with": {"type": "array", "items": {"type": "string"}},
                    "related_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "importance_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "asset_paths": {"type": "array", "items": {"type": "string"}},
                    "map_names": {"type": "array", "items": {"type": "string"}},
                    "plugin_names": {"type": "array", "items": {"type": "string"}},
                    "module_names": {"type": "array", "items": {"type": "string"}},
                    "class_names": {"type": "array", "items": {"type": "string"}},
                    "blueprint_paths": {"type": "array", "items": {"type": "string"}},
                    "system_area": {"type": "string"},
                    "distill": {
                        "type": "boolean",
                        "default": False,
                        "description": "Opt-in: after writing the raw record, run an LLM map-reduce distill and persist the summary as a derived `distilled_summary` record (derived_from_record_ids → raw id). Result attached as `distilled` on the response. Requires LLM config; on missing config returns `llm_unavailable` without losing the primary write.",
                    },
                    "distill_user_instruction": {
                        "type": "string",
                        "description": "Optional instruction prepended to the distillation user message (e.g. 'Focus on action items').",
                    },
                    "distill_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags to apply to the derived distilled record (defaults to `tags`).",
                    },
                    "distill_max_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Per-call max output tokens for the distill pass (clamped by LLMConfig.max_output_tokens_per_call).",
                    },
                    "llm_normalize_tags": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Opt-in for operation=record. If tags include values outside the controlled "
                            "vocabulary and an LLM is configured, keep valid tags, merge suggested valid "
                            "tags, and park rejected business words on system_area."
                        ),
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_board_read",
            description=_BASE_DESCRIPTIONS["memory_board_read"],
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "enum": ["all", "unresolved"], "default": "unresolved"},
                    "user_id": {"type": "string", "description": "Filter by author user id."},
                    "agent_instance_id": {"type": "string", "description": "Filter by author agent instance id."},
                    "task_id": {"type": "string", "description": "Filter by task id."},
                    "status": {"type": "string", "description": "Filter by post status."},
                    "post_type": {
                        "type": "string",
                        "enum": ["note", "question", "request", "warning", "handoff", "proposal", "reply"],
                    },
                    "thread_id": {"type": "string", "description": "Filter by thread id."},
                    "max_items": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
                    "context_token": {"type": "string", "description": "Optional task context token for attribution."},
                    "include_diagnostics": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_board_write",
            description=_BASE_DESCRIPTIONS["memory_board_write"],
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["post", "reply", "resolve"], "default": "post"},
                    "content": {"type": "string", "description": "Post or reply content."},
                    "post_type": {
                        "type": "string",
                        "enum": ["note", "question", "request", "warning", "handoff", "proposal"],
                        "description": "Required for post; replies are assigned type reply automatically.",
                    },
                    "post_id": {"type": "string", "description": "Target post id for resolve."},
                    "thread_id": {"type": "string", "description": "Target thread id for reply."},
                    "reply_to": {"type": "string", "description": "Parent post id for reply."},
                    "task_id": {"type": "string"},
                    "references_json": {
                        "type": "array",
                        "items": {"type": ["string", "number", "boolean", "object", "array", "null"]},
                    },
                    "expires_at": {"type": "string", "description": "Optional ISO-8601 expiration timestamp."},
                    "author": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "agent_instance_id": {"type": "string"},
                    "context_token": {"type": "string", "description": "Optional task context token for attribution."},
                },
                "additionalProperties": False,
            },
        ),
    ]


def _build_legacy_tools(file_roles: str, path_hint: str) -> list[Tool]:
    """Legacy/admin schema set retained for reference.

    ⚠️  This bucket is the runtime surface of the DEPRECATED governance
    link (DesignDoc §10 / §15.4) plus other admin-only ops.  `_build_tools`
    no longer registers these schemas for MCP; they exist for historical-data
    migration reference and CLI parity checks. Do not add new product
    features here; ordinary agent capabilities belong on memory_read or
    memory_write, and admin/sync capabilities belong on CLI.
    """
    return [
        Tool(
            name="memory_get",
            description=_BASE_DESCRIPTIONS["memory_get"] + file_roles,
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Target file path. Recommended: {path_hint}. Must stay within allowed_roots.",
                    },
                    "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-based, optional)"},
                    "end_line": {"type": "integer", "minimum": 1, "description": "End line (1-based, optional)"},
                    "max_chars": {"type": "integer", "minimum": 0, "description": "Max characters to return (optional)"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_search",
            description=_BASE_DESCRIPTIONS["memory_search"] + file_roles,
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword(s)"},
                    "scopes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Search scope directories, e.g. ['memory-bank'], ['.ai-context'].",
                    },
                    "top_k": {"type": "integer", "minimum": 1, "description": "Number of results to return (default 10)"},
                    "include_paths": {"type": "array", "items": {"type": "string"}, "description": "Include path globs"},
                    "exclude_paths": {"type": "array", "items": {"type": "string"}, "description": "Exclude path globs"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_guard_check",
            description=_BASE_DESCRIPTIONS["memory_guard_check"],
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_backup",
            description=_BASE_DESCRIPTIONS["memory_backup"],
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": f"File paths to backup. Common targets: {path_hint}.",
                    },
                    "reason": {"type": "string", "description": "Backup reason (optional)"},
                    "tag": {"type": "string", "description": "Tag (optional)"},
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_compact",
            description=_BASE_DESCRIPTIONS["memory_compact"],
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Target file path for compaction.",
                    },
                    "policy": {
                        "type": "string",
                        "enum": ["hot_task", "error_summary", "warm_context"],
                        "description": "Compaction policy: hot_task, error_summary, or warm_context",
                    },
                    "dry_run": {"type": "boolean", "default": True, "description": "Preview only; do not write files"},
                    "backup": {"type": "boolean", "default": True, "description": "Create backup before apply mode"},
                    "archive_original": {"type": "boolean", "default": True, "description": "Archive original content"},
                    "compress_to_tokens": {"type": "integer", "minimum": 1, "description": "Target token cap (optional)"},
                },
                "required": ["path", "policy"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_write",
            description=_BASE_DESCRIPTIONS["memory_write"] + file_roles,
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Target file path. Must be within allowed_roots. Targets: {path_hint}.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to write. For overwrite mode, this replaces the entire file.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "default": "overwrite",
                        "description": "Write mode: 'overwrite' replaces the file, 'append' adds to the end.",
                    },
                    "backup": {
                        "type": "boolean",
                        "default": True,
                        "description": "Auto-backup existing file before writing (default true).",
                    },
                    "create_if_missing": {
                        "type": "boolean",
                        "default": True,
                        "description": "Create the file if it does not exist (default true).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for the write (logged in audit event, optional).",
                    },
                    "inject_user_tag": {
                        "type": "boolean",
                        "description": (
                            "Whether to inject an HTML comment with the writing user and timestamp. "
                            "Omit (default) to auto-detect: Markdown files get the tag, every other "
                            "extension is left untouched so JSON/YAML/TOML/source files are not corrupted. "
                            "Set to false to force-disable, true to force-enable."
                        ),
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_write_record",
            description=_BASE_DESCRIPTIONS["memory_write_record"],
            inputSchema={
                "type": "object",
                "properties": {
                    "content_markdown": {
                        "type": "string",
                        "description": "Markdown body for the memory record.",
                    },
                    "record_kind": {
                        "type": "string",
                        "enum": [
                            "note",
                            "event",
                            "claim_candidate",
                            "rule_candidate",
                            "handoff",
                            "skill_candidate",
                            "validation_result",
                            "system_rule",
                            "archive_record",
                            "observation",
                            "artifact_ref",
                            "incident",
                            "decision",
                            "procedure",
                            "snapshot_daily",
                            "snapshot_weekly",
                            "snapshot_monthly",
                        ],
                        "default": "note",
                    },
                    "scope": {
                        "type": "string",
                        "enum": [
                            "personal",
                            "shared",
                            "local",
                            "archive",
                            "session",
                            "user_private",
                            "task_or_branch",
                            "project_shared",
                            "org_shared",
                        ],
                        "default": "personal",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["raw", "candidate", "validated", "published", "degraded", "archived"],
                    },
                    "author": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "validated_by": {"type": "string"},
                    "classifier_model": {"type": "string"},
                    "classifier_prompt_version": {"type": "string"},
                    "tag_schema_version": {"type": "string", "default": "v1"},
                    "schema_version": {"type": "string", "enum": ["1.0", "2.0"]},
                    "occurred_at": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                    "memory_tier": {"type": "string", "enum": ["hot", "warm", "cold", "fossil"]},
                    "cognitive_level": {"type": "string", "enum": ["dao", "fa", "shu"]},
                    "derived_from_record_ids": {"type": "array", "items": {"type": "string"}},
                    "derived_from_snapshot_ids": {"type": "array", "items": {"type": "string"}},
                    "derived_from_revision_ids": {"type": "array", "items": {"type": "string"}},
                    "supersedes": {"type": "array", "items": {"type": "string"}},
                    "conflicts_with": {"type": "array", "items": {"type": "string"}},
                    "related_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "importance_score": {"type": "number", "minimum": 0, "maximum": 1},
                    "asset_paths": {"type": "array", "items": {"type": "string"}},
                    "map_names": {"type": "array", "items": {"type": "string"}},
                    "plugin_names": {"type": "array", "items": {"type": "string"}},
                    "module_names": {"type": "array", "items": {"type": "string"}},
                    "class_names": {"type": "array", "items": {"type": "string"}},
                    "blueprint_paths": {"type": "array", "items": {"type": "string"}},
                    "system_area": {"type": "string"},
                },
                "required": ["content_markdown"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_rebuild_index",
            description=_BASE_DESCRIPTIONS["memory_rebuild_index"],
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_search_records",
            description=_BASE_DESCRIPTIONS["memory_search_records"],
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Record search query."},
                    "top_k": {"type": "integer", "minimum": 1, "description": "Number of results to return."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_compile",
            description=_BASE_DESCRIPTIONS["memory_compile"],
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": [
                            "runtime_digest",
                            "task_handoff",
                            "system_digest",
                            "publish_queue",
                            "daily_snapshot",
                            "weekly_snapshot",
                            "monthly_snapshot",
                            "rollback_context",
                            "review_queue",
                            "dao_digest",
                            "fa_digest",
                            "shu_digest",
                        ],
                        "description": "Compile target.",
                    },
                    "user": {"type": "string"},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "include_scopes": {"type": "array", "items": {"type": "string"}},
                    "include_statuses": {"type": "array", "items": {"type": "string"}},
                    "preferred_tags": {"type": "array", "items": {"type": "string"}},
                    "as_of": {"type": "string"},
                    "body_mode": {
                        "type": "string",
                        "enum": ["compact", "full"],
                        "default": "compact",
                        "description": (
                            "Compiled body rendering mode. compact keeps only key extracted content "
                            "plus source references; full preserves previous verbose record rendering."
                        ),
                    },
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_get_runtime_digest",
            description=_BASE_DESCRIPTIONS["memory_get_runtime_digest"],
            inputSchema={
                "type": "object",
                "properties": {
                    "user": {"type": "string"},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_validate_candidate",
            description=_BASE_DESCRIPTIONS["memory_validate_candidate"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "validated_by": {"type": "string"},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_publish_candidate",
            description=_BASE_DESCRIPTIONS["memory_publish_candidate"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "published_by": {"type": "string"},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_archive_record",
            description=_BASE_DESCRIPTIONS["memory_archive_record"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_update_index",
            description=_BASE_DESCRIPTIONS["memory_update_index"],
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_health_check",
            description=_BASE_DESCRIPTIONS["memory_health_check"],
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_migrate_records",
            description=_BASE_DESCRIPTIONS["memory_migrate_records"],
            inputSchema={
                "type": "object",
                "properties": {
                    "target_schema_version": {"type": "string", "default": "1.0"},
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_delete_record",
            description=_BASE_DESCRIPTIONS["memory_delete_record"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_record_observation",
            description=_BASE_DESCRIPTIONS["memory_record_observation"],
            inputSchema={
                "type": "object",
                "properties": {
                    "content_markdown": {"type": "string", "description": "Observation body as Markdown."},
                    "author": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "source_refs": {"type": "array", "items": {"type": "string"}},
                    "task_id": {"type": "string"},
                    "branch": {"type": "string"},
                    "occurred_at": {"type": "string"},
                    "memory_tier": {"type": "string", "enum": ["hot", "warm", "cold", "fossil"], "default": "hot"},
                    "cognitive_level": {"type": "string", "enum": ["dao", "fa", "shu"], "default": "shu"},
                    "related_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_paths": {"type": "array", "items": {"type": "string"}},
                    "map_names": {"type": "array", "items": {"type": "string"}},
                    "plugin_names": {"type": "array", "items": {"type": "string"}},
                    "module_names": {"type": "array", "items": {"type": "string"}},
                    "class_names": {"type": "array", "items": {"type": "string"}},
                    "blueprint_paths": {"type": "array", "items": {"type": "string"}},
                    "system_area": {"type": "string"},
                },
                "required": ["content_markdown"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_link_artifact",
            description=_BASE_DESCRIPTIONS["memory_link_artifact"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "related_artifact_ids": {"type": "array", "items": {"type": "string"}},
                    "asset_paths": {"type": "array", "items": {"type": "string"}},
                    "map_names": {"type": "array", "items": {"type": "string"}},
                    "plugin_names": {"type": "array", "items": {"type": "string"}},
                    "module_names": {"type": "array", "items": {"type": "string"}},
                    "class_names": {"type": "array", "items": {"type": "string"}},
                    "blueprint_paths": {"type": "array", "items": {"type": "string"}},
                    "system_area": {"type": "string"},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="memory_trace_lineage",
            description=_BASE_DESCRIPTIONS["memory_trace_lineage"],
            inputSchema={
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "max_depth": {"type": "integer", "minimum": 0},
                },
                "required": ["record_id"],
                "additionalProperties": False,
            },
        ),
    ]


def _build_tools(config: MemoryConfig) -> list[Tool]:
    """Build tool definitions with dynamic descriptions from config.

    The MCP surface exposes the general read/write facades plus dedicated
    Project Board read/write tools. `_build_facade_tools` no longer constructs schemas
    for legacy `memory_context` / `memory_enhance`; admin/legacy ops live
    on the CLI. We still defensively filter by name in case a downstream
    caller monkey-patches `_build_facade_tools` to extend the surface.
    """
    file_roles = _build_file_roles(config)

    target_paths = [t.path for t in config.guard_targets]
    path_hint = ", ".join(target_paths) if target_paths else "memory-bank/*.md, .ai-context/*.md"
    facade_tools = _build_facade_tools(file_roles, path_hint, config.tag_allowed_tags)
    task_sync_tool = Tool(
        name="memory_task_sync",
        description=_BASE_DESCRIPTIONS["memory_task_sync"],
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "sync",
                        "history",
                        "create",
                        "assign",
                        "claim",
                        "decline",
                        "report",
                        "block",
                        "resume",
                        "submit",
                        "review",
                        "reassign",
                        "cancel",
                    ],
                    "default": "sync",
                },
                "command_id": {"type": "string", "description": "Idempotency key for each mutation."},
                "expected_version": {"type": "integer", "minimum": 0},
                "expected_assignment_epoch": {"type": "integer", "minimum": 0},
                "task_id": {"type": "string"},
                "actor_id": {"type": "string", "description": "Optional local actor override; context_token supplies this when omitted."},
                "agent_id": {"type": "string", "description": "Agent filter for reads; also accepted as the local actor alias."},
                "context_token": {"type": "string", "description": "Optional task-context token used to derive the local actor identity."},
                "cursor": {"type": "integer", "minimum": 0},
                "max_items": {"type": "integer", "minimum": 1, "maximum": 200},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "acceptance": {"type": "string"},
                "priority": {"type": "string"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "parent_task_id": {"type": "string"},
                "produced_memory": {"type": "array", "items": {"type": "string"}},
                "assignee": {"type": "string"},
                "attempt_id": {"type": "string"},
                "summary": {"type": "string"},
                "reason": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "submission_id": {"type": "string"},
                "review_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["approved", "changes_requested"]},
            },
            "additionalProperties": False,
        },
    )
    allowed_tools = {"memory_read", "memory_write", "memory_board_read", "memory_board_write"}
    return [*([tool for tool in facade_tools if tool.name in allowed_tools]), task_sync_tool]


__all__ = ["_build_file_roles", "_build_facade_tools", "_build_legacy_tools", "_build_tools"]
