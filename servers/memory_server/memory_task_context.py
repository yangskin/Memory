"""Deterministic task context resolver for shared MCP server instances.

The memory server must not keep a single process-global "current task".
Multiple agents can call the same MCP server instance interleaved, so task
identity is bound to an explicit context token returned by begin_task.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_identity import canonical_identity
from .memory_locks import file_lock
from .memory_record_io import _atomic_write_text
from .memory_result import error_result, ok_result
from .memory_users import is_placeholder_user

_STORE_VERSION = 1
_MAX_GOAL_CHARS = 2000
_MAX_ACTIVE_FILES = 64
_TASK_MATCH_GOAL_THRESHOLD = 0.82
_TASK_MATCH_GOAL_WITH_FILES_THRESHOLD = 0.65
_RECOVERY_REBIND_THRESHOLD = 0.85
_RECOVERY_CANDIDATE_THRESHOLD = 0.35


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path(config: MemoryConfig) -> Path:
    return config.repo_root / ".ai-memory" / "task-contexts.json"


def _current_task_rel_path(*, user: str, context_token: str) -> str:
    safe_user = _slug(user, max_len=64)
    return f".ai-context/current-task/{safe_user}/{context_token}.md"


def _empty_store() -> dict[str, Any]:
    return {
        "version": _STORE_VERSION,
        "tasks": {},
        "contexts": {},
        "session_bindings": {},
    }


def _read_store(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return _empty_store()
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return _empty_store()
    if not isinstance(data, dict):
        return _empty_store()
    data.setdefault("version", _STORE_VERSION)
    data.setdefault("tasks", {})
    data.setdefault("contexts", {})
    data.setdefault("session_bindings", {})
    if not isinstance(data["tasks"], dict):
        data["tasks"] = {}
    if not isinstance(data["contexts"], dict):
        data["contexts"] = {}
    if not isinstance(data["session_bindings"], dict):
        data["session_bindings"] = {}
    return data


def _write_store(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _write_current_task_file(config: MemoryConfig, ctx: dict[str, Any], task: dict[str, Any]) -> str:
    rel_path = _current_task_rel_path(
        user=str(ctx.get("user") or "unknown"),
        context_token=str(ctx.get("context_token") or "ctx_missing"),
    )
    path = config.repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    active_files = task.get("active_files") if isinstance(task.get("active_files"), list) else []
    lines = [
        "# Current Task",
        "",
        f"- task_id: `{ctx.get('task_id')}`",
        f"- task_run_id: `{ctx.get('task_run_id')}`",
        f"- context_token: `{ctx.get('context_token')}`",
        f"- user: `{ctx.get('user')}`",
        f"- agent_id: `{ctx.get('agent_id')}`",
        f"- workspace_id: `{ctx.get('workspace_id')}`",
    ]
    if ctx.get("branch"):
        lines.append(f"- branch: `{ctx.get('branch')}`")
    if task.get("user_goal"):
        lines.extend(["", "## Goal", "", str(task.get("user_goal"))])
    if active_files:
        lines.extend(["", "## Active Files", ""])
        lines.extend(f"- `{item}`" for item in active_files[:_MAX_ACTIVE_FILES])
    lines.append("")
    _atomic_write_text(path, "\n".join(lines), fsync_strict=config.mcp_fsync_strict)
    return rel_path


def _slug(value: str, *, max_len: int = 48) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value.lower()).strip("-")
    text = re.sub(r"-+", "-", text)
    return (text[:max_len].strip("-") or "task")


def _slug_id(value: str, *, prefix: str) -> str:
    text = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{3,96}", text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{_slug(text, max_len=40)}_{digest}"


def _hash_text(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _normalize_text(value: object, *, max_chars: int | None = None) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value.strip())
    if max_chars is not None:
        text = text[:max_chars]
    return text


def _normalize_list(value: object, *, limit: int = 128) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = _normalize_text(item)
        if not text:
            continue
        normalized = text.replace("\\", "/")
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(normalized)
        if len(out) >= limit:
            break
    return out


def _word_set(value: str) -> set[str]:
    return {part for part in re.split(r"[^A-Za-z0-9\u4e00-\u9fff]+", value.lower()) if len(part) >= 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a.intersection(b)) / max(1, len(a.union(b)))


def _list_overlap(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    aa = {item.lower() for item in a}
    bb = {item.lower() for item in b}
    return len(aa.intersection(bb)) / max(1, min(len(aa), len(bb)))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recovery_text(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("content_markdown", "content", "system_area", "task_id", "branch"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    for key in ("source_refs", "asset_paths", "map_names", "plugin_names", "module_names", "class_names", "blueprint_paths"):
        value = args.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
    return "\n".join(parts)


def _path_signal_score(active_files: list[str], text: str) -> tuple[float, list[str]]:
    if not active_files or not text.strip():
        return 0.0, []
    normalized_text = text.lower().replace("\\", "/")
    hits: list[str] = []
    for item in active_files:
        normalized = str(item).lower().replace("\\", "/")
        if not normalized:
            continue
        path = Path(normalized)
        stem = path.stem
        parent = path.parent.as_posix().strip(".")
        if normalized in normalized_text or (stem and stem in normalized_text) or (parent and parent in normalized_text):
            hits.append(str(item))
    if not hits:
        return 0.0, []
    return min(1.0, len(hits) / max(1, min(len(active_files), 3))), hits[:5]


def _recency_score(ctx: dict[str, Any]) -> float:
    stamp = _parse_time(ctx.get("last_used_at") or ctx.get("created_at"))
    if stamp is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - stamp).total_seconds())
    if age_seconds <= 24 * 60 * 60:
        return 1.0
    if age_seconds <= 7 * 24 * 60 * 60:
        return 0.55
    if age_seconds <= 30 * 24 * 60 * 60:
        return 0.25
    return 0.0


def _context_recovery_candidates(config: MemoryConfig, args: dict[str, Any]) -> list[dict[str, Any]]:
    user = canonical_identity(_normalize_text(args.get("user")) or get_current_user(config.repo_root))
    agent_id = _normalize_text(args.get("agent_id") or args.get("client_name"))
    workspace_id = _workspace_id(config, args)
    branch = _normalize_text(args.get("branch")) or None
    text = _recovery_text(args)
    words = _word_set(text)
    store_file = _store_path(config)
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        tasks = dict(store.get("tasks") or {})
        contexts = dict(store.get("contexts") or {})

    candidates: list[dict[str, Any]] = []
    for token, ctx in contexts.items():
        if not isinstance(ctx, dict):
            continue
        if user and canonical_identity(ctx.get("user")) != user:
            continue
        task_id = str(ctx.get("task_id") or "")
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            continue
        ctx_workspace = str(ctx.get("workspace_id") or task.get("workspace_id") or "")
        workspace_match = bool(ctx_workspace and ctx_workspace.lower() == workspace_id.lower())
        if workspace_id and ctx_workspace and not workspace_match:
            continue
        ctx_branch = str(ctx.get("branch") or task.get("branch") or "")
        if branch and ctx_branch and ctx_branch != branch:
            continue

        goal_words = set(str(item) for item in task.get("goal_words", []) if str(item))
        goal_overlap = _jaccard(words, goal_words)
        file_score, file_hits = _path_signal_score(list(task.get("active_files") or []), text)
        task_words = _word_set(" ".join([task_id, str(task.get("user_goal") or "")]))
        task_word_hits = sorted(words.intersection(task_words))[:8]
        agent_match = bool(agent_id and str(ctx.get("agent_id") or "").lower() == agent_id.lower())

        score = 0.0
        reasons: list[str] = []
        if workspace_match:
            score += 0.22
            reasons.append("workspace")
        score += 0.18
        reasons.append("user")
        if agent_match:
            score += 0.06
            reasons.append("agent")
        if goal_overlap:
            score += min(0.24, goal_overlap * 0.60)
            reasons.append("goal_words")
        if file_score:
            score += min(0.28, file_score * 0.28)
            reasons.append("active_files")
        if task_word_hits:
            score += min(0.12, len(task_word_hits) * 0.03)
            reasons.append("task_terms")
        recency = _recency_score(ctx)
        if recency:
            score += min(0.10, recency * 0.10)
            reasons.append("recency")
        score = min(0.99, score)
        if score < _RECOVERY_CANDIDATE_THRESHOLD:
            continue
        candidates.append(
            {
                "context_token": str(token),
                "task_id": task_id,
                "task_run_id": ctx.get("task_run_id"),
                "user": ctx.get("user"),
                "agent_id": ctx.get("agent_id"),
                "workspace_id": ctx_workspace,
                "current_task_path": ctx.get("current_task_path"),
                "confidence": round(score, 3),
                "matched_by": reasons,
                "file_hits": file_hits,
            }
        )
    candidates.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
    return candidates[:5]


def recover_task_context_for_write(
    config: MemoryConfig,
    args: dict[str, Any],
    context_error: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Salvage valuable record/observation writes that carry a stale token.

    Context inference is intentionally conservative: a high-confidence match
    may rebind the write to an existing task, while uncertain matches keep the
    record as recovered/orphaned with the original token preserved in metadata.
    """

    if context_error.get("error") != "invalid_context_token":
        return args, None
    operation = str(args.get("operation") or "record")
    if operation not in {"record", "observation"}:
        return args, None
    body = args.get("content_markdown") if args.get("content_markdown") is not None else args.get("content")
    if not isinstance(body, str) or not body.strip():
        return args, None

    invalid_token = _normalize_text(args.get("context_token"))
    candidates = _context_recovery_candidates(config, args)
    best = candidates[0] if candidates else None
    recovered = dict(args)
    recovered.pop("context_token", None)
    recovery = {
        "enabled": True,
        "recovered": True,
        "original_error": "invalid_context_token",
        "invalid_context_token": invalid_token,
        "candidates": candidates,
    }
    if best and float(best.get("confidence") or 0.0) >= _RECOVERY_REBIND_THRESHOLD:
        loaded = get_task_context(config, str(best.get("context_token") or ""))
        if loaded.get("ok"):
            recovered["context_token"] = loaded.get("context_token")
            recovery.update(
                {
                    "mode": "rebound",
                    "context_rebound": True,
                    "confidence": best.get("confidence"),
                    "matched_by": best.get("matched_by"),
                    "rebound_context_token": loaded.get("context_token"),
                    "task_id": loaded.get("task_id"),
                }
            )
            rebound_args, rebound_error = apply_task_context(config, recovered)
            if rebound_error is None:
                return rebound_args, recovery
            recovered.pop("context_token", None)

    user = canonical_identity(_normalize_text(args.get("user")) or get_current_user(config.repo_root))
    recovered.setdefault("user", user)
    recovered.setdefault("author", user)
    recovered.setdefault("task_id", "recovered_invalid_context")
    recovered["status"] = "raw"
    if operation == "record":
        recovered.setdefault("record_kind", "note")
        recovered.setdefault("scope", "personal")
    allowed = set(getattr(config, "tag_allowed_tags", None) or [])
    tags = [str(item) for item in recovered.get("tags") or [] if str(item) and (not allowed or str(item) in allowed)]
    if "needs_validation" in allowed and "needs_validation" not in tags:
        tags.append("needs_validation")
    if tags:
        recovered["tags"] = tags
    refs = [str(item) for item in recovered.get("source_refs") or [] if str(item)]
    if invalid_token:
        refs.append(f"invalid_context_token:{invalid_token}")
    recovered["source_refs"] = refs
    recovery.update(
        {
            "mode": "orphan",
            "context_rebound": False,
            "confidence": float(best.get("confidence") or 0.0) if best else 0.0,
            "task_id": recovered.get("task_id"),
        }
    )
    return recovered, recovery


def _workspace_id(config: MemoryConfig, args: dict[str, Any]) -> str:
    explicit = _normalize_text(args.get("workspace_id") or args.get("workspace"))
    if explicit:
        return explicit
    roots = _normalize_list(args.get("roots"), limit=8)
    if roots:
        return roots[0]
    return config.repo_root.as_posix()


def _fingerprint(
    *,
    workspace_id: str,
    branch: str | None,
    external_ref: str | None,
    user_goal: str,
    active_files: list[str],
) -> str:
    payload = {
        "workspace": workspace_id.lower(),
        "branch": (branch or "").lower(),
        "external_ref": (external_ref or "").lower(),
        "goal": user_goal.lower(),
        "active_files": sorted(item.lower() for item in active_files),
    }
    return _hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), length=16)


def _session_binding_key(*, user: str, agent_id: str, client_session_id: str, workspace_id: str) -> str:
    raw = json.dumps(
        {
            "user": user,
            "agent_id": agent_id,
            "client_session_id": client_session_id,
            "workspace_id": workspace_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _hash_text(raw, length=32)


def _public_context(ctx: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "context_token",
        "task_id",
        "task_run_id",
        "user",
        "agent_id",
        "client_session_id",
        "workspace_id",
        "branch",
        "current_task_path",
        "created_at",
        "last_used_at",
    ]
    return {key: ctx.get(key) for key in keys if ctx.get(key) not in (None, "", [])}


def _find_matching_task(
    tasks: dict[str, Any],
    *,
    workspace_id: str,
    branch: str | None,
    external_ref: str | None,
    fingerprint: str,
    user_goal: str,
    active_files: list[str],
) -> tuple[str | None, float, list[str]]:
    if external_ref:
        for task_id, task in tasks.items():
            refs = [str(item).lower() for item in task.get("external_refs", []) if str(item)]
            if external_ref.lower() in refs:
                return str(task_id), 0.98, ["external_ref"]

    for task_id, task in tasks.items():
        if task.get("fingerprint") == fingerprint:
            return str(task_id), 0.94, ["fingerprint"]

    goal_words = _word_set(user_goal)
    best: tuple[str | None, float, list[str]] = (None, 0.0, [])
    for task_id, task in tasks.items():
        if str(task.get("workspace_id", "")).lower() != workspace_id.lower():
            continue
        task_branch = str(task.get("branch") or "")
        if branch and task_branch and task_branch != branch:
            continue
        goal_score = _jaccard(goal_words, set(task.get("goal_words", [])))
        file_score = _list_overlap(active_files, list(task.get("active_files", [])))
        task_files = list(task.get("active_files", []))
        files_conflict = bool(active_files and task_files and file_score <= 0.0)
        if goal_score >= _TASK_MATCH_GOAL_THRESHOLD and not files_conflict:
            score = min(0.90, 0.70 + goal_score * 0.20 + file_score * 0.10)
            reasons = ["goal_similarity"]
            if file_score:
                reasons.append("active_files")
        elif goal_score >= _TASK_MATCH_GOAL_WITH_FILES_THRESHOLD and file_score >= 0.50:
            score = min(0.86, 0.55 + goal_score * 0.20 + file_score * 0.15)
            reasons = ["goal_similarity", "active_files"]
        else:
            continue
        if score > best[1]:
            best = (str(task_id), score, reasons)
    return best


def _new_task_id(user_goal: str, external_ref: str | None) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    if external_ref:
        base = _slug(external_ref, max_len=44)
    else:
        base = _slug(user_goal, max_len=44)
    digest = _hash_text(f"{external_ref or ''}\n{user_goal}\n{secrets.token_hex(8)}", length=8)
    return f"task_{day}_{base}_{digest}"


def _new_run_id(agent_id: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"run_{day}_{_slug(agent_id, max_len=24)}_{secrets.token_hex(4)}"


def begin_or_resolve_task(config: MemoryConfig, **kwargs: Any) -> dict[str, Any]:
    """Create or resolve task identity and bind it to a context token."""
    user = canonical_identity(_normalize_text(kwargs.get("user")) or get_current_user(config.repo_root))
    if is_placeholder_user(user) and not getattr(config, "mcp_allow_unknown_user", False):
        return error_result(
            "user_not_configured",
            "begin_task requires a stable user id; pass user or configure MCP/Memory/user_config.local.json.",
        )

    agent_id = _normalize_text(kwargs.get("agent_id") or kwargs.get("client_name")) or "unknown-agent"
    client_session_id = _normalize_text(kwargs.get("client_session_id"))
    client_name = _normalize_text(kwargs.get("client_name"))
    client_version = _normalize_text(kwargs.get("client_version"))
    branch = _normalize_text(kwargs.get("branch")) or None
    external_ref = _normalize_text(kwargs.get("external_ref")) or None
    user_goal = _normalize_text(kwargs.get("user_goal") or kwargs.get("goal"), max_chars=_MAX_GOAL_CHARS)
    active_files = _normalize_list(kwargs.get("active_files"), limit=_MAX_ACTIVE_FILES)
    explicit_task_id = _normalize_text(kwargs.get("task_id"))
    workspace_id = _workspace_id(config, kwargs)
    now = _now_text()

    if not explicit_task_id and not user_goal and not external_ref and not client_session_id:
        return error_result(
            "insufficient_task_signal",
            "begin_task requires task_id, external_ref, user_goal, or client_session_id.",
        )

    store_file = _store_path(config)
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        tasks: dict[str, Any] = store["tasks"]
        contexts: dict[str, Any] = store["contexts"]
        session_bindings: dict[str, Any] = store["session_bindings"]

        binding_key = ""
        if client_session_id:
            binding_key = _session_binding_key(
                user=user,
                agent_id=agent_id,
                client_session_id=client_session_id,
                workspace_id=workspace_id,
            )
            existing_token = session_bindings.get(binding_key)
            if isinstance(existing_token, str) and existing_token in contexts and not explicit_task_id:
                ctx = contexts[existing_token]
                ctx["last_used_at"] = now
                task_for_ctx = tasks.get(str(ctx.get("task_id"))) if isinstance(tasks, dict) else None
                if isinstance(task_for_ctx, dict):
                    ctx["current_task_path"] = _write_current_task_file(config, ctx, task_for_ctx)
                _write_store(store_file, store)
                return ok_result(
                    "task context resolved from client session binding",
                    status="matched_session",
                    confidence=0.99,
                    matched_by=["client_session_id"],
                    **_public_context(ctx),
                )

        active_fingerprint = _fingerprint(
            workspace_id=workspace_id,
            branch=branch,
            external_ref=external_ref,
            user_goal=user_goal,
            active_files=active_files,
        )
        matched_by: list[str] = []
        confidence = 1.0
        if explicit_task_id:
            task_id = _slug_id(explicit_task_id, prefix="task")
            status = "explicit"
            matched_by = ["task_id"]
        else:
            task_id, confidence, matched_by = _find_matching_task(
                tasks,
                workspace_id=workspace_id,
                branch=branch,
                external_ref=external_ref,
                fingerprint=active_fingerprint,
                user_goal=user_goal,
                active_files=active_files,
            )
            if task_id:
                status = "matched_existing"
            else:
                task_id = _new_task_id(user_goal or external_ref or client_session_id, external_ref)
                confidence = 0.55 if user_goal or external_ref else 0.35
                status = "created_provisional" if confidence < 0.70 else "created"
                matched_by = ["created"]

        task = tasks.get(task_id)
        if not isinstance(task, dict):
            task = {
                "task_id": task_id,
                "created_at": now,
                "workspace_id": workspace_id,
                "branch": branch,
                "external_refs": [],
                "active_files": [],
                "goal_words": [],
                "agents": [],
            }
            tasks[task_id] = task
        task["updated_at"] = now
        task["workspace_id"] = workspace_id
        if branch:
            task["branch"] = branch
        if user_goal:
            task["user_goal"] = user_goal
            task["goal_words"] = sorted(_word_set(user_goal))
        if active_files:
            task["active_files"] = active_files
        if external_ref:
            refs = [str(item) for item in task.get("external_refs", []) if str(item)]
            if external_ref not in refs:
                refs.append(external_ref)
            task["external_refs"] = refs
        task["fingerprint"] = active_fingerprint
        agents = [str(item) for item in task.get("agents", []) if str(item)]
        if agent_id not in agents:
            agents.append(agent_id)
        task["agents"] = agents

        context_token = "ctx_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
        ctx = {
            "context_token": context_token,
            "task_id": task_id,
            "task_run_id": _new_run_id(agent_id),
            "user": user,
            "agent_id": agent_id,
            "client_session_id": client_session_id,
            "client_name": client_name,
            "client_version": client_version,
            "workspace_id": workspace_id,
            "branch": branch,
            "created_at": now,
            "last_used_at": now,
        }
        ctx["current_task_path"] = _write_current_task_file(config, ctx, task)
        contexts[context_token] = ctx
        if binding_key:
            session_bindings[binding_key] = context_token
        _write_store(store_file, store)

    try:
        append_event(
            config,
            "task_context_resolved",
            {
                "task_id": task_id,
                "task_run_id": ctx["task_run_id"],
                "agent_id": agent_id,
                "status": status,
                "matched_by": matched_by,
                "confidence": confidence,
            },
        )
    except Exception:
        pass

    return ok_result(
        "task context resolved",
        status=status,
        confidence=confidence,
        matched_by=matched_by,
        **_public_context(ctx),
    )


def get_task_context(config: MemoryConfig, context_token: str) -> dict[str, Any]:
    token = _normalize_text(context_token)
    if not token:
        return error_result("context_token_required", "context_token is required")
    store_file = _store_path(config)
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        ctx = store.get("contexts", {}).get(token)
        if not isinstance(ctx, dict):
            return error_result("invalid_context_token", "context_token was not found")
        ctx["last_used_at"] = _now_text()
        _write_store(store_file, store)
    return ok_result("task context loaded", **_public_context(ctx))


def mark_task_checkpoint(
    config: MemoryConfig,
    context_token: str,
    phase: str,
) -> dict[str, Any]:
    """把 checkpoint 持久化回任务索引，供后续任务可靠定位上一次完成任务。

    旧版本只返回 checkpoint 响应，没有在 task-context store 中留下完成态；
    因此新实现保留有限 phase 历史，并在 ``task_done`` 时写入
    ``completed_at``。调用方必须把这里视为派生增强：失败不得破坏主写入。
    """

    token = _normalize_text(context_token)
    normalized_phase = _normalize_text(phase)
    if not token:
        return error_result("context_token_required", "context_token is required")
    if not normalized_phase:
        return error_result("invalid_input", "checkpoint phase is required")

    store_file = _store_path(config)
    now = _now_text()
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        ctx = store.get("contexts", {}).get(token)
        if not isinstance(ctx, dict):
            return error_result("invalid_context_token", "context_token was not found")
        task_id = _normalize_text(ctx.get("task_id"))
        task = store.get("tasks", {}).get(task_id)
        if not isinstance(task, dict):
            return error_result("task_not_found", f"task was not found: {task_id}")

        history = [item for item in task.get("checkpoint_history", []) if isinstance(item, dict)]
        history.append({"phase": normalized_phase, "at": now, "task_run_id": ctx.get("task_run_id")})
        task["checkpoint_history"] = history[-16:]
        task["last_phase"] = normalized_phase
        task["last_checkpoint_at"] = now
        task["updated_at"] = now
        if normalized_phase == "task_done":
            task["status"] = "completed"
            task["completed_at"] = now
        elif task.get("status") != "completed":
            task["status"] = "active"
        _write_store(store_file, store)

    return ok_result(
        "task checkpoint persisted",
        task_id=task_id,
        phase=normalized_phase,
        completed_at=task.get("completed_at"),
    )


def get_task_history(
    config: MemoryConfig,
    *,
    user: str | None,
    workspace_id: str | None = None,
    branch: str | None = None,
    exclude_task_id: str | None = None,
) -> list[dict[str, Any]]:
    """返回当前用户可归属的任务历史，完成任务优先且按时间倒序。"""

    normalized_user = canonical_identity(_normalize_text(user))
    normalized_workspace = _normalize_text(workspace_id)
    normalized_branch = _normalize_text(branch)
    store_file = _store_path(config)
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        contexts = dict(store.get("contexts") or {})
        tasks = dict(store.get("tasks") or {})

    visible_task_ids: set[str] = set()
    for ctx in contexts.values():
        if not isinstance(ctx, dict):
            continue
        if normalized_user and canonical_identity(ctx.get("user")) != normalized_user:
            continue
        task_id = _normalize_text(ctx.get("task_id"))
        if task_id:
            visible_task_ids.add(task_id)

    result: list[dict[str, Any]] = []
    for task_id in visible_task_ids:
        if exclude_task_id and task_id == exclude_task_id:
            continue
        task = tasks.get(task_id)
        if not isinstance(task, dict):
            continue
        task_workspace = _normalize_text(task.get("workspace_id"))
        task_branch = _normalize_text(task.get("branch"))
        if normalized_workspace and task_workspace and task_workspace != normalized_workspace:
            continue
        if normalized_branch and task_branch and task_branch != normalized_branch:
            continue
        result.append(
            {
                "task_id": task_id,
                "status": task.get("status"),
                "last_phase": task.get("last_phase"),
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
                "completed_at": task.get("completed_at"),
                "last_checkpoint_at": task.get("last_checkpoint_at"),
                "workspace_id": task.get("workspace_id"),
                "branch": task.get("branch"),
                "user_goal": task.get("user_goal"),
                "active_files": list(task.get("active_files") or [])[:_MAX_ACTIVE_FILES],
            }
        )

    result.sort(
        key=lambda item: str(
            item.get("completed_at")
            or item.get("last_checkpoint_at")
            or item.get("updated_at")
            or item.get("created_at")
            or ""
        ),
        reverse=True,
    )
    return result


def get_task_ids_for_user(config: MemoryConfig, user: str | None) -> set[str]:
    """Return task ids that are known to belong to ``user``.

    This is intentionally derived from context records rather than record
    authors, because some callers identify the writing agent as ``author``
    while the task context still carries the human/account user.
    """
    normalized_user = _normalize_text(user)
    if not normalized_user:
        return set()
    store_file = _store_path(config)
    with file_lock(config.repo_root, store_file):
        store = _read_store(store_file)
        contexts = dict(store.get("contexts") or {})

    task_ids: set[str] = set()
    for ctx in contexts.values():
        if not isinstance(ctx, dict):
            continue
        if canonical_identity(ctx.get("user")) != canonical_identity(normalized_user):
            continue
        task_id = _normalize_text(ctx.get("task_id"))
        if task_id:
            task_ids.add(task_id)
    return task_ids


def apply_task_context(config: MemoryConfig, args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return args with user/task defaults injected from context_token."""
    token = _normalize_text(args.get("context_token"))
    if not token:
        return args, None
    loaded = get_task_context(config, token)
    if not loaded.get("ok"):
        return args, loaded
    enriched = dict(args)
    if not enriched.get("user"):
        enriched["user"] = loaded.get("user")
    if not enriched.get("author"):
        enriched["author"] = loaded.get("user")
    if not enriched.get("task_id"):
        enriched["task_id"] = loaded.get("task_id")
    if not enriched.get("branch") and loaded.get("branch"):
        enriched["branch"] = loaded.get("branch")
    enriched["_task_context"] = {
        "context_token": loaded.get("context_token"),
        "task_id": loaded.get("task_id"),
        "task_run_id": loaded.get("task_run_id"),
        "user": loaded.get("user"),
        "agent_id": loaded.get("agent_id"),
        "workspace_id": loaded.get("workspace_id"),
        "branch": loaded.get("branch"),
        "current_task_path": loaded.get("current_task_path"),
    }
    return enriched, None


def attach_task_context(result: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    ctx = args.get("_task_context")
    if isinstance(ctx, dict) and result.get("ok"):
        result["task_context"] = dict(ctx)
        result.setdefault("task_id", ctx.get("task_id"))
        result.setdefault("task_run_id", ctx.get("task_run_id"))
        result.setdefault("user", ctx.get("user"))
        result.setdefault("author", ctx.get("user"))
        result.setdefault("agent_id", ctx.get("agent_id"))
    return result
