"""
memory_write — controlled write tool for memory files.

Safety features:
    - Path whitelist (allowed_roots) enforced by PathManager
    - Auto-backup before overwrite (configurable)
    - Guard check after write to warn on capacity overflow
    - Audit event logged for every write
    - Atomic write via temp file + os.replace
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_backup import backup_files
from .memory_config import MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_guard import check_total_budget
from .memory_locks import LockTimeoutError, file_lock
from .memory_compactor import _compact_warm_context
from .memory_paths import PathManager, PathSecurityError, resolve_user_path
from .memory_record_io import DiskFullError, _atomic_write_text
from .memory_request_id import content_sha, new_request_id
from .memory_result import error_result, ok_result
from .memory_users import validate_effective_user
from .token_estimator import estimate_tokens


def _lookup_write_policy(config: MemoryConfig, path: str) -> str | None:
    """查找目标路径对应的 write_policy。

    优先匹配 guard_targets 中的 write_policy，
    其次匹配 multi_user.user_scoped_paths / shared_paths_policy。
    返回 None 表示无特殊策略。
    """
    # 1. guard targets 中的 write_policy 优先
    normalized = path.replace("\\", "/").strip("/")
    for target in config.guard_targets:
        tp = target.path.replace("\\", "/").strip("/")
        if tp == normalized or normalized.endswith(tp):
            if target.write_policy:
                return target.write_policy
    # 2. multi_user.user_scoped_paths 兜底：兼容旧 config 覆盖了
    # guard.targets、但没有给 activeContext 写 write_policy 的情况。
    if config.multi_user:
        for scoped in config.multi_user.user_scoped_paths or []:
            scoped_norm = scoped.replace("\\", "/").strip("/")
            if scoped_norm == normalized or normalized.endswith(scoped_norm):
                return "user_scoped"

    # 3. multi_user.shared_paths_policy 兜底
    if config.multi_user and config.multi_user.shared_paths_policy:
        for sp, policy in config.multi_user.shared_paths_policy.items():
            sp_norm = sp.replace("\\", "/").strip("/")
            if sp_norm == normalized or normalized.endswith(sp_norm):
                return policy
    return None


def _active_context_guard_limits(config: MemoryConfig, rel_path: str) -> tuple[int | None, int | None]:
    normalized = rel_path.replace("\\", "/").strip("/")
    for target in config.guard_targets:
        target_norm = target.path.replace("\\", "/").strip("/")
        if normalized == target_norm or normalized.endswith(target_norm):
            max_chars = target.max_chars if target.max_chars is not None else config.guard_default_max_chars
            max_tokens = target.max_tokens if target.max_tokens is not None else config.guard_default_max_tokens
            return max_chars, max_tokens
        if target_norm == "memory-bank/activeContext.md" and normalized.startswith("memory-bank/activeContext/"):
            max_chars = target.max_chars if target.max_chars is not None else config.guard_default_max_chars
            max_tokens = target.max_tokens if target.max_tokens is not None else config.guard_default_max_tokens
            return max_chars, max_tokens
    return None, None


def _is_user_scoped_active_context(config: MemoryConfig, original_path: str, effective_path: str, policy: str | None) -> bool:
    if policy != "user_scoped":
        return False
    normalized_original = original_path.replace("\\", "/").strip("/")
    normalized_effective = effective_path.replace("\\", "/").strip("/")
    if normalized_effective.startswith("memory-bank/activeContext/") and normalized_effective.endswith(".md"):
        return True
    if not config.multi_user:
        return False
    for scoped in config.multi_user.user_scoped_paths or []:
        scoped_norm = scoped.replace("\\", "/").strip("/")
        if scoped_norm == "memory-bank/activeContext.md" and (
            normalized_original == scoped_norm or normalized_original.endswith(scoped_norm)
        ):
            return True
    return False


def _active_context_archive_user(rel_path: str) -> str:
    name = Path(rel_path).stem.strip()
    return name or "unknown"


def _maybe_auto_archive_active_context(
    config: MemoryConfig,
    *,
    resolved: Path,
    rel_path: str,
    final_content: str,
    after_chars: int,
    after_tokens: int,
    max_chars: int | None,
    max_tokens: int | None,
    fsync_strict: bool,
) -> dict[str, Any] | None:
    settings = getattr(config, "key_documents_active_context_auto_archive", None) or {}
    if settings.get("enabled") is False:
        return None
    over_chars = max_chars is not None and after_chars > max_chars
    over_tokens = max_tokens is not None and after_tokens > max_tokens
    if not over_chars and not over_tokens:
        return None

    user = _active_context_archive_user(rel_path)
    archive_root = str(settings.get("archive_dir") or "memory-bank/archive/activeContext").replace("\\", "/").strip("/")
    archive_dir = (config.repo_root / archive_root / user).resolve()
    try:
        archive_dir.relative_to(config.repo_root.resolve())
    except ValueError:
        return {
            "ok": False,
            "error": "archive_path_not_allowed",
            "message": f"archive_dir escapes repo root: {archive_root}",
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive_name = f"activeContext-{stamp}-{secrets.token_hex(4)}.md"
    archive_path = archive_dir / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Archived activeContext for `{user}`\n\n"
        "<!-- archived-by: memory-mcp active-context auto-archive; "
        f"source={rel_path}; chars={after_chars}; tokens={after_tokens}; "
        f"max_chars={max_chars}; max_tokens={max_tokens}; ts={datetime.now(timezone.utc).isoformat()} -->\n\n"
    )
    try:
        _atomic_write_text(archive_path, header + final_content, fsync_strict=fsync_strict)
    except DiskFullError as exc:
        return {"ok": False, "error": "disk_full", "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": "archive_failed", "message": str(exc)}

    compacted_text, notes = _compact_warm_context(final_content)
    if max_chars is not None and len(compacted_text) > max_chars:
        marker = "\n\n[Compacted by activeContext auto-archive]\n"
        compacted_text = compacted_text[: max(0, max_chars - len(marker))].rstrip() + marker
        notes.append("Compacted activeContext was trimmed to max_chars.")
    if compacted_text and not compacted_text.endswith("\n"):
        compacted_text += "\n"

    try:
        _atomic_write_text(resolved, compacted_text, fsync_strict=fsync_strict)
    except DiskFullError as exc:
        return {"ok": False, "error": "disk_full", "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": "compact_write_failed", "message": str(exc)}

    try:
        archive_rel = archive_path.relative_to(config.repo_root).as_posix()
    except ValueError:
        archive_rel = str(archive_path)
    after_compact_chars = len(compacted_text)
    after_compact_tokens = estimate_tokens(compacted_text)
    append_event(
        config,
        event_type="active_context_auto_archive",
        payload={
            "path": rel_path,
            "archive_path": archive_rel,
            "before": {"chars": after_chars, "tokens_est": after_tokens},
            "after": {"chars": after_compact_chars, "tokens_est": after_compact_tokens},
            "max_chars": max_chars,
            "max_tokens": max_tokens,
            "notes": notes,
        },
    )
    return {
        "ok": True,
        "action": "archived_and_compacted",
        "archive_path": archive_rel,
        "before": {"chars": after_chars, "tokens_est": after_tokens},
        "after": {"chars": after_compact_chars, "tokens_est": after_compact_tokens},
        "notes": notes,
    }


def memory_write(
    config: MemoryConfig,
    path: str,
    content: str,
    *,
    mode: str = "overwrite",
    backup: bool = True,
    create_if_missing: bool = True,
    reason: str | None = None,
    inject_user_tag: bool | None = None,
    if_match: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Write content to a memory file with safety controls.

    Args:
        config: MemoryConfig instance.
        path: Target file path (must be within allowed_roots).
        content: The content to write.
        mode: "overwrite" (replace entire file) or "append" (add to end).
        backup: Whether to auto-backup before writing (default True).
        create_if_missing: Create the file if it doesn't exist (default True).
        reason: Optional reason for the write (logged in audit event).
        inject_user_tag: Whether to inject an HTML comment with the writing
            user and timestamp into the file content. When None (default), the
            writer auto-detects: Markdown files (`.md`/`.markdown`) get the
            tag for traceability, every other extension is left untouched so
            JSON / YAML / TOML / source files cannot be silently corrupted.
            Pass False to force-disable injection even for Markdown.
        if_match: Optional SHA-256 (lowercase hex) of the *expected* on-disk
            file contents. If supplied and the current file's sha differs,
            the write is rejected with ``error="conflict"`` and a fresh
            ``current_sha`` so the caller can re-read, re-merge, and retry.
            Use this when multiple agents may write the same file
            concurrently and you want to surface lost-update conflicts
            instead of accepting last-write-wins. The empty string matches
            "file does not yet exist".
        request_id: Optional client-supplied request id. When omitted, a
            fresh UUID7 is generated. Returned in the result and recorded
            in the audit event so multi-process retries are traceable.

    Returns:
        Result dict with ok/error status and metadata. On success the
        result includes ``request_id`` and ``new_sha`` (the SHA-256 of
        the file contents *after* the write, suitable for use as the
        next call's ``if_match``).
    """
    rid = request_id or new_request_id()
    # P0-1: refuse to write when effective user id is a placeholder.
    # This prevents silent collisions on shared / unconfigured machines.
    user_err = validate_effective_user(config)
    if user_err is not None and user_err.get("ok") is False:
        return {**user_err, "request_id": rid}
    # Validate mode
    if mode not in ("overwrite", "append"):
        return error_result("invalid_input", "mode must be 'overwrite' or 'append'")

    if not content and mode == "overwrite":
        return error_result("invalid_input", "content must not be empty for overwrite mode")

    # write_policy 检查：append_only 强制降级 / user_scoped 路径重定向
    policy_override: str | None = None
    effective_mode = mode
    effective_path = path
    _write_policy = _lookup_write_policy(config, path)
    if _write_policy == "append_only" and mode == "overwrite":
        # P0-2 (v0.6.0 OOTB): default behavior is strict reject so the
        # AI client cannot silently turn an "overwrite" intent into an
        # append (which would duplicate / corrupt shared knowledge).
        # Legacy silent downgrade is opt-in via
        # mcp.shared_overwrite_policy="downgrade".
        if getattr(config, "mcp_shared_overwrite_policy", "reject") == "reject":
            return {
                "ok": False,
                "error": "shared_overwrite_forbidden",
                "message": (
                    "Target is a shared append-only file; overwrite would "
                    "destroy other users' contributions. Use mode='append' "
                    "or memory_write(operation='record', ...) instead."
                ),
                "path": path,
                "policy": "append_only",
                "suggested_operation": "record",
                "suggested_mode": "append",
                "request_id": rid,
            }
        effective_mode = "append"
        policy_override = "append_only"
    elif _write_policy == "user_scoped":
        current_user_for_path = get_current_user(config.repo_root)
        if not current_user_for_path or current_user_for_path == "unknown":
            return error_result(
                "user_required",
                "user_scoped write requires a valid user identity. "
                "Set MEMORY_MCP_USER or MCP/Memory/user_config.local.json.",
            )
        effective_path = resolve_user_path(config, path, current_user_for_path)

    manager = PathManager(config)

    # Resolve and validate path security（使用 effective_path，可能已被 user_scoped 重定向）
    try:
        resolved = manager.resolve(
            effective_path,
            must_exist=not create_if_missing,
            must_be_file=False,
        )
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))

    # ── Critical section: cross-process serialised by per-target file lock.
    #
    # Holding the lock around read → if_match check → backup → atomic write
    # → audit event guarantees that two MCP server processes (one per
    # VS Code window / Codex session) cannot interleave their writes on
    # the same file. The lock is sidecar (`.ai-memory/locks/<sha>.lock`),
    # so the target file itself is never opened with an exclusive handle.
    try:
        with file_lock(config.repo_root, resolved):
            # If it exists, ensure it's a file (not a directory)
            if resolved.exists() and not resolved.is_file():
                return error_result("invalid_path", f"target is not a file: {effective_path}")

            file_exists = resolved.exists()
            rel_path = manager.to_repo_relative(resolved)

            # Read original content for diff metadata + if_match check
            original_content = ""
            if file_exists:
                try:
                    original_content = resolved.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    return error_result("read_failed", f"failed to read existing file: {exc}")

            # Optimistic-locking: caller asserted the on-disk sha they
            # observed before composing `content`. If another writer has
            # mutated the file in the meantime, surface a structured
            # conflict instead of silently overwriting their changes.
            if if_match is not None:
                expected = if_match.strip().lower()
                actual = content_sha(original_content) if file_exists else ""
                if expected != actual:
                    return error_result(
                        "conflict",
                        "if_match precondition failed: file changed since read",
                        path=rel_path,
                        expected_sha=expected,
                        current_sha=actual,
                        request_id=rid,
                    )

            # Pre-write global budget check
            net_new_chars = (
                len(content) - len(original_content)
                if effective_mode == "overwrite"
                else len(content)
            )
            if net_new_chars > 0:
                budget_err = check_total_budget(config, extra_chars=net_new_chars)
                if budget_err is not None:
                    return budget_err

            # Auto-backup before writing (only if file exists)
            backup_result: dict[str, Any] | None = None
            if backup and file_exists:
                backup_result = backup_files(
                    config,
                    [rel_path],
                    reason=reason or "memory_write auto-backup",
                    tag="pre_write",
                    event_type="memory_backup",
                    write_event=True,
                )
                if not backup_result.get("ok"):
                    return error_result(
                        "backup_failed",
                        f"auto-backup failed before write: {backup_result.get('message', 'unknown')}",
                    )

            # Build final content
            current_user = get_current_user(config.repo_root)
            suffix = resolved.suffix.lower()
            if inject_user_tag is None:
                tag_enabled = suffix in {".md", ".markdown"}
            else:
                tag_enabled = bool(inject_user_tag)
            if effective_mode == "append":
                if tag_enabled:
                    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    user_header = f"\n<!-- written by {current_user} at {timestamp} -->\n"
                    separator = "" if original_content.endswith("\n") or not original_content else "\n"
                    final_content = original_content + separator + user_header + content
                else:
                    separator = "" if original_content.endswith("\n") or not original_content else "\n"
                    final_content = original_content + separator + content
            else:
                if tag_enabled:
                    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    footer = f"\n<!-- last overwritten by {current_user} at {timestamp} -->\n"
                    final_content = content.rstrip("\n") + "\n" + footer
                else:
                    final_content = content

            # Ensure final content ends with newline
            if final_content and not final_content.endswith("\n"):
                final_content += "\n"

            # Atomic write via shared helper: same-dir tmp + O_EXCL +
            # fsync (data + parent dir) + os.replace. Honors
            # mcp.fsync_strict from config.
            resolved.parent.mkdir(parents=True, exist_ok=True)
            try:
                _atomic_write_text(
                    resolved,
                    final_content,
                    fsync_strict=config.mcp_fsync_strict,
                )
            except DiskFullError as exc:
                # Surface a structured ``disk_full`` code so operators
                # / agents can branch on disk pressure specifically
                # (e.g. trigger compaction or fail over) instead of
                # treating it as a generic write failure.
                return error_result(
                    "disk_full",
                    f"out of disk space writing {rel_path}: {exc}",
                    errno=exc.errno,
                )
            except OSError as exc:
                return error_result("write_failed", f"failed to write file: {exc}")

            # Compute metadata
            after_chars = len(final_content)
            after_tokens = estimate_tokens(final_content)
            before_chars = len(original_content)
            before_tokens = estimate_tokens(original_content)
            new_sha = content_sha(final_content)

            # Log audit event（自动包含 user 字段，由 append_event 注入）
            append_event(
                config,
                event_type="memory_write",
                payload={
                    "path": rel_path,
                    "mode": effective_mode,
                    "original_mode": mode if policy_override else None,
                    "policy_override": policy_override,
                    "reason": reason,
                    "backup": backup,
                    "created": not file_exists,
                    "before": {"chars": before_chars, "tokens_est": before_tokens},
                    "after": {"chars": after_chars, "tokens_est": after_tokens},
                    "batch_id": backup_result.get("batch_id") if backup_result else None,
                    "written_at": datetime.now(timezone.utc).isoformat(),
                    "request_id": rid,
                    "new_sha": new_sha,
                    "if_match": if_match,
                },
            )

            # Check guard threshold for the written file
            guard_warning: str | None = None
            guard_max_chars: int | None = None
            guard_max_tokens: int | None = None
            for target in config.guard_targets:
                if (
                    target.path == rel_path
                    or rel_path.endswith(target.path)
                    or (
                        target.path.replace("\\", "/").strip("/") == "memory-bank/activeContext.md"
                        and rel_path.startswith("memory-bank/activeContext/")
                    )
                ):
                    guard_max_chars = target.max_chars if target.max_chars is not None else config.guard_default_max_chars
                    guard_max_tokens = target.max_tokens if target.max_tokens is not None else config.guard_default_max_tokens
                    if guard_max_chars is not None and after_chars > guard_max_chars:
                        guard_warning = f"exceeds max_chars ({after_chars}/{guard_max_chars})"
                    elif guard_max_tokens is not None and after_tokens > guard_max_tokens:
                        guard_warning = f"exceeds max_tokens ({after_tokens}/{guard_max_tokens})"
                    elif guard_max_chars is not None and after_chars >= int(guard_max_chars * 0.9):
                        guard_warning = f"near max_chars threshold ({after_chars}/{guard_max_chars})"
                    elif guard_max_tokens is not None and after_tokens >= int(guard_max_tokens * 0.9):
                        guard_warning = f"near max_tokens threshold ({after_tokens}/{guard_max_tokens})"
                    break

            active_context_auto_compaction = None
            final_after_chars = after_chars
            final_after_tokens = after_tokens
            final_new_sha = new_sha
            if _is_user_scoped_active_context(config, path, rel_path, _write_policy):
                if guard_max_chars is None and guard_max_tokens is None:
                    guard_max_chars, guard_max_tokens = _active_context_guard_limits(config, rel_path)
                active_context_auto_compaction = _maybe_auto_archive_active_context(
                    config,
                    resolved=resolved,
                    rel_path=rel_path,
                    final_content=final_content,
                    after_chars=after_chars,
                    after_tokens=after_tokens,
                    max_chars=guard_max_chars,
                    max_tokens=guard_max_tokens,
                    fsync_strict=config.mcp_fsync_strict,
                )
                if active_context_auto_compaction and active_context_auto_compaction.get("ok"):
                    try:
                        compacted_content = resolved.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        compacted_content = ""
                    final_after_chars = len(compacted_content)
                    final_after_tokens = estimate_tokens(compacted_content)
                    final_new_sha = content_sha(compacted_content)

            result = ok_result(
                "write completed",
                path=rel_path,
                mode=effective_mode,
                created=not file_exists,
                before={"chars": before_chars, "tokens_est": before_tokens},
                after={"chars": final_after_chars, "tokens_est": final_after_tokens},
                backup_batch_id=backup_result.get("batch_id") if backup_result else None,
                guard_warning=guard_warning,
                reason=reason,
                request_id=rid,
                new_sha=final_new_sha,
            )
            if active_context_auto_compaction is not None:
                result["active_context_auto_compaction"] = active_context_auto_compaction
            if policy_override:
                result["original_mode"] = mode
                result["policy_override"] = policy_override
            return result
    except LockTimeoutError as exc:
        return error_result(
            "lock_timeout",
            f"could not acquire write lock for {effective_path}: {exc}",
            path=effective_path,
            request_id=rid,
        )
