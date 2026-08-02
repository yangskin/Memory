from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .memory_backup import backup_files
from .memory_config import MemoryConfig
from .memory_events import append_event
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import _atomic_write_text, safe_read_text
from .memory_request_id import content_sha, new_request_id
from .memory_result import error_result, ok_result
from .token_estimator import estimate_tokens

_VALID_POLICIES = {"hot_task", "error_summary", "warm_context"}
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+")


def _transaction_dir(config: MemoryConfig) -> Path:
    return (config.repo_root / ".ai-memory" / "transactions").resolve()


def _write_transaction(config: MemoryConfig, path: Path, payload: dict) -> None:
    _atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        fsync_strict=config.mcp_fsync_strict,
    )


def recover_compaction_transactions(config: MemoryConfig) -> dict:
    """Finish prepared compactions after an unclean MCP process shutdown."""

    directory = _transaction_dir(config)
    if not directory.is_dir():
        return ok_result("no compaction transactions require recovery", recovered=0, conflicts=0, errors=[])
    manager = PathManager(config)
    recovered = 0
    conflicts = 0
    errors: list[dict[str, str]] = []
    for journal in sorted(directory.glob("compact-*.json")):
        try:
            transaction = json.loads(safe_read_text(journal, errors="strict"))
            if not isinstance(transaction, dict):
                raise ValueError("transaction root must be an object")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            errors.append({"journal": journal.name, "error": f"invalid transaction journal: {exc}"})
            continue
        if transaction.get("state") != "prepared":
            continue
        try:
            target = manager.resolve(str(transaction.get("path") or ""), must_exist=True, must_be_file=True)
            candidate = str(transaction.get("candidate_content") or "")
            source_sha = str(transaction.get("source_sha") or "")
            candidate_sha = str(transaction.get("candidate_sha") or "")
            if not source_sha or content_sha(candidate) != candidate_sha:
                raise ValueError("transaction hashes do not match the stored candidate")
            with file_lock(config.repo_root, target):
                current = safe_read_text(target, errors="strict")
                current_sha = content_sha(current)
                if current_sha == source_sha:
                    _atomic_write_text(target, candidate, fsync_strict=config.mcp_fsync_strict)
                    outcome = "replayed"
                elif current_sha == candidate_sha:
                    outcome = "already_applied"
                else:
                    transaction["state"] = "conflict"
                    transaction["finished_at"] = datetime.now(timezone.utc).isoformat()
                    transaction["recovery_error"] = "target changed independently after transaction preparation"
                    _write_transaction(config, journal, transaction)
                    conflicts += 1
                    continue
                transaction["state"] = "committed"
                transaction["finished_at"] = datetime.now(timezone.utc).isoformat()
                transaction["recovery_outcome"] = outcome
                transaction.pop("candidate_content", None)
                _write_transaction(config, journal, transaction)
                recovered += 1
        except FileNotFoundError as exc:
            # 目标在 prepared 后被明确删除时不能永远重试；将事务封存为冲突，
            # 保留 journal 供人工审计且不在每次 worker tick 重复报错。
            transaction["state"] = "conflict"
            transaction["finished_at"] = datetime.now(timezone.utc).isoformat()
            transaction["recovery_error"] = f"target disappeared after transaction preparation: {exc}"
            transaction.pop("candidate_content", None)
            try:
                _write_transaction(config, journal, transaction)
                conflicts += 1
            except OSError as write_exc:
                errors.append({"journal": journal.name, "error": f"failed to persist missing-target conflict: {write_exc}"})
        except (OSError, UnicodeError, ValueError, PathSecurityError) as exc:
            errors.append({"journal": journal.name, "error": str(exc)})
    if errors:
        return error_result(
            "recovery_incomplete",
            "one or more compaction transactions could not be recovered",
            recovered=recovered,
            conflicts=conflicts,
            errors=errors,
        )
    return ok_result("compaction transaction recovery completed", recovered=recovered, conflicts=conflicts, errors=[])


def _parse_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_title = "__preamble__"
    current_lines: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            sections.append((current_title, current_lines))
            current_title = match.group(1).strip().lower()
            current_lines = []
            continue
        current_lines.append(line)
    sections.append((current_title, current_lines))
    return sections


def _collect_by_heading(sections: list[tuple[str, list[str]]], keywords: list[str], fallback_text: str) -> list[str]:
    keywords_lower = [keyword.lower() for keyword in keywords]
    collected: list[str] = []
    for title, lines in sections:
        if title == "__preamble__":
            continue
        if any(keyword in title for keyword in keywords_lower):
            for line in lines:
                if line.strip():
                    collected.append(line.rstrip())
    if not collected and fallback_text:
        for line in fallback_text.splitlines():
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in keywords_lower):
                collected.append(line.rstrip())
    return collected


def _clean_dedup(lines: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = line.strip()
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(line.rstrip())
    return result


def _render_block(title: str, lines: list[str], fallback: str) -> list[str]:
    rendered = [f"## {title}"]
    if lines:
        rendered.extend(lines)
    else:
        rendered.append(f"- {fallback}")
    rendered.append("")
    return rendered


def _latest_attempts(lines: list[str], keep_count: int = 3) -> list[str]:
    bullets = [line for line in lines if _BULLET_RE.match(line)]
    source = bullets if bullets else lines
    if len(source) <= keep_count:
        return source
    return source[-keep_count:]


def _compact_hot_task(text: str) -> tuple[str, list[str]]:
    sections = _parse_sections(text)
    output: list[str] = ["# Hot Task Compact", ""]

    task_lines = _clean_dedup(_collect_by_heading(sections, ["task", "任务"], text))
    goal_lines = _clean_dedup(_collect_by_heading(sections, ["goal", "done definition", "目标", "完成定义"], text))
    status_lines = _clean_dedup(
        _collect_by_heading(sections, ["current status", "status", "当前状态", "当前工作焦点"], text)
    )
    files_lines = _clean_dedup(_collect_by_heading(sections, ["relevant files", "assets", "文件", "资产"], text))
    constraints_lines = _clean_dedup(_collect_by_heading(sections, ["constraints", "constraint", "约束", "限制"], text))
    attempts_lines = _clean_dedup(_collect_by_heading(sections, ["latest attempts", "尝试", "attempt"], text))
    request_lines = _clean_dedup(
        _collect_by_heading(
            sections,
            ["what i want from ai right now", "ai request", "我希望ai", "需要ai", "我想让ai"],
            text,
        )
    )

    output.extend(_render_block("Task", task_lines[:12], "not found; add a short task statement"))
    output.extend(_render_block("Goal / Done Definition", goal_lines[:12], "not found"))
    output.extend(_render_block("Current status", status_lines[:12], "not found"))
    output.extend(_render_block("Relevant files / assets", files_lines[:16], "not found"))
    output.extend(_render_block("Constraints", constraints_lines[:12], "not found"))
    output.extend(_render_block("Latest attempts (latest 3)", _latest_attempts(attempts_lines, 3), "no attempts recorded"))
    output.extend(_render_block("What I want from AI right now", request_lines[:12], "not found"))

    notes = [
        "Older attempts and long discussion content were folded.",
        "Completed long details should be moved to progress/archive files.",
    ]
    return "\n".join(output).strip() + "\n", notes


def _compact_error_summary(text: str) -> tuple[str, list[str]]:
    sections = _parse_sections(text)
    output: list[str] = ["# Latest Error Summary", ""]

    symptom = _clean_dedup(_collect_by_heading(sections, ["symptom", "症状", "报错现象"], text))
    when_happens = _clean_dedup(_collect_by_heading(sections, ["when", "happens", "触发", "何时"], text))
    first_error = _clean_dedup(
        _collect_by_heading(sections, ["first meaningful error", "first error", "首个", "第一条错误"], text)
    )
    changed = _clean_dedup(_collect_by_heading(sections, ["what changed", "changed", "改动", "变更"], text))
    ai_request = _clean_dedup(_collect_by_heading(sections, ["ai request", "what i want from ai", "希望ai"], text))

    output.extend(_render_block("Symptom", symptom[:10], "not found"))
    output.extend(_render_block("When it happens", when_happens[:10], "not found"))
    output.extend(_render_block("First meaningful error", first_error[:10], "not found"))
    output.extend(_render_block("What changed before this happened", changed[:10], "not found"))
    output.extend(_render_block("AI request", ai_request[:10], "not found"))

    notes = ["History append was removed. This file now keeps only the latest effective summary."]
    return "\n".join(output).strip() + "\n", notes


def _compact_warm_context(text: str) -> tuple[str, list[str]]:
    sections = _parse_sections(text)
    output: list[str] = ["# Warm Context Compact", ""]

    sprint_focus = _clean_dedup(
        _collect_by_heading(sections, ["current sprint", "focus", "冲刺", "焦点", "目标"], text)
    )
    blockers = _clean_dedup(_collect_by_heading(sections, ["blocker", "阻塞", "待解决", "问题"], text))
    recent_decisions = _clean_dedup(_collect_by_heading(sections, ["decision", "决策"], text))
    week_priority = _clean_dedup(_collect_by_heading(sections, ["this week", "priority", "本周", "优先"], text))

    output.extend(_render_block("Current sprint / focus", sprint_focus[:14], "not found"))
    output.extend(_render_block("Blockers", blockers[:12], "not found"))
    output.extend(_render_block("Recent decisions summary", recent_decisions[:12], "not found"))
    output.extend(_render_block("This week priorities", week_priority[:12], "not found"))

    notes = ["Historical sprint details should be archived outside active context."]
    return "\n".join(output).strip() + "\n", notes


def _apply_token_budget(text: str, token_limit: int | None) -> tuple[str, bool]:
    if token_limit is None:
        return text, False
    if token_limit <= 0:
        return "", True
    char_budget = token_limit * 4
    if len(text) <= char_budget:
        return text, False
    marker = "\n\n[Compacted to token budget in Phase 1]\n"
    available = max(0, char_budget - len(marker))
    return text[:available].rstrip() + marker, True


def compact_memory(
    config: MemoryConfig,
    *,
    path: str,
    policy: str,
    dry_run: bool = True,
    backup: bool = True,
    archive_original: bool = True,
    compress_to_tokens: int | None = None,
) -> dict:
    if policy not in _VALID_POLICIES:
        return error_result("invalid_policy", f"policy must be one of: {sorted(_VALID_POLICIES)}")

    manager = PathManager(config)
    try:
        target = manager.resolve(path, must_exist=True, must_be_file=True)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))
    except IsADirectoryError as exc:
        return error_result("invalid_path", str(exc))

    try:
        source_text = safe_read_text(target, errors="strict")
    except UnicodeError as exc:
        return error_result("invalid_encoding", f"source is not valid UTF-8: {exc}")
    original_chars = len(source_text)
    original_tokens = estimate_tokens(source_text)

    if policy == "hot_task":
        compacted_text, notes = _compact_hot_task(source_text)
    elif policy == "error_summary":
        compacted_text, notes = _compact_error_summary(source_text)
    else:
        compacted_text, notes = _compact_warm_context(source_text)

    compacted_text, budget_trimmed = _apply_token_budget(compacted_text, compress_to_tokens)
    if budget_trimmed:
        notes.append("Output was truncated to compress_to_tokens budget.")

    compacted_chars = len(compacted_text)
    compacted_tokens = estimate_tokens(compacted_text)
    reduction_ratio = 0.0 if original_chars == 0 else round((1.0 - (compacted_chars / original_chars)) * 100.0, 2)

    response_payload = {
        "path": manager.to_repo_relative(target),
        "policy": policy,
        "dry_run": dry_run,
        "backup": backup,
        "archive_original": archive_original,
        "before": {"chars": original_chars, "tokens_est": original_tokens},
        "after": {"chars": compacted_chars, "tokens_est": compacted_tokens},
        "reduction_percent": reduction_ratio,
        "candidate_content": compacted_text,
        "notes": notes,
    }

    if dry_run:
        return ok_result("dry run completed", **response_payload)

    backup_result: dict | None = None
    if backup or archive_original:
        backup_result = backup_files(
            config,
            [manager.to_repo_relative(target)],
            reason="memory_compact_apply",
            tag=policy,
            event_type="memory_backup",
            write_event=True,
        )
        if not backup_result.get("ok"):
            return backup_result

    transaction_id = "compact-" + new_request_id().replace("-", "")
    journal = _transaction_dir(config) / f"{transaction_id}.json"
    transaction = {
        "transaction_id": transaction_id,
        "operation": "memory_compact",
        "state": "prepared",
        "path": manager.to_repo_relative(target),
        "policy": policy,
        "source_sha": content_sha(source_text),
        "candidate_sha": content_sha(compacted_text),
        "candidate_content": compacted_text,
        "backup_batch_id": backup_result.get("batch_id") if backup_result else None,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "config_hash": config.config_hash,
    }
    try:
        _write_transaction(config, journal, transaction)
        with file_lock(config.repo_root, target):
            current_text = safe_read_text(target, errors="strict")
            if content_sha(current_text) != transaction["source_sha"]:
                transaction["state"] = "conflict"
                transaction["finished_at"] = datetime.now(timezone.utc).isoformat()
                transaction["recovery_error"] = "target changed after compaction candidate was created"
                transaction.pop("candidate_content", None)
                _write_transaction(config, journal, transaction)
                return error_result(
                    "source_changed",
                    "source changed before compact commit; no compacted content was written",
                    transaction_id=transaction_id,
                )
            _atomic_write_text(target, compacted_text, fsync_strict=config.mcp_fsync_strict)
            transaction["state"] = "committed"
            transaction["finished_at"] = datetime.now(timezone.utc).isoformat()
            transaction.pop("candidate_content", None)
            _write_transaction(config, journal, transaction)
    except (OSError, UnicodeError, ValueError) as exc:
        return error_result(
            "write_failed",
            f"failed to commit compact transaction: {exc}",
            transaction_id=transaction_id,
        )

    event_warning: dict[str, str] | None = None
    try:
        append_event(
            config,
            event_type="memory_compact",
            payload={
            "path": manager.to_repo_relative(target),
            "policy": policy,
            "backup": backup,
            "archive_original": archive_original,
            "dry_run": False,
            "before": {"chars": original_chars, "tokens_est": original_tokens},
            "after": {"chars": compacted_chars, "tokens_est": compacted_tokens},
            "batch_id": backup_result.get("batch_id") if backup_result else None,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:  # noqa: BLE001 - the compact transaction is already committed
        event_warning = {
            "code": "event_log_deferred",
            "message": f"compact committed but audit event failed: {type(exc).__name__}: {exc}",
        }

    result = ok_result(
        "compact apply completed",
        **response_payload,
        applied=True,
        batch_id=backup_result.get("batch_id") if backup_result else None,
        backup_result=backup_result,
        transaction_id=transaction_id,
    )
    if event_warning:
        result["warnings"] = [event_warning]
    return result


__all__ = ["compact_memory", "recover_compaction_transactions"]
