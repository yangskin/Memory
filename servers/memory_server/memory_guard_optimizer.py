from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_backup import backup_files
from .memory_compactor import _compact_error_summary, _compact_hot_task, _compact_warm_context
from .memory_config import GuardTarget, MemoryConfig
from .memory_events import append_event
from .memory_locks import LockTimeoutError, file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import DiskFullError, _atomic_write_text
from .memory_result import error_result
from .token_estimator import estimate_tokens


@dataclass(frozen=True)
class GuardBudget:
    target: GuardTarget
    max_chars: int | None
    max_tokens: int | None
    policy: str | None


def _norm(path: str | Path) -> str:
    return str(path).replace("\\", "/").strip("/")


def _active_context_user_file_matches(target_path: str, rel_path: str) -> bool:
    if _norm(target_path) != "memory-bank/activeContext.md":
        return False
    rel = _norm(rel_path)
    return rel.startswith("memory-bank/activeContext/") and rel.endswith(".md")


def guard_budget_for_path(config: MemoryConfig, rel_path: str) -> GuardBudget | None:
    """Return the effective guard budget for a concrete repository file.

    User-scoped activeContext files inherit the canonical
    ``memory-bank/activeContext.md`` target because guard reports them as
    ``memory-bank/activeContext/<user>.md``.
    """

    rel = _norm(rel_path)
    for target in config.guard_targets:
        target_path = _norm(target.path)
        if rel != target_path and not rel.endswith(target_path) and not _active_context_user_file_matches(target_path, rel):
            continue
        max_chars = target.max_chars if target.max_chars is not None else config.guard_default_max_chars
        max_tokens = target.max_tokens if target.max_tokens is not None else config.guard_default_max_tokens
        return GuardBudget(
            target=target,
            max_chars=max_chars,
            max_tokens=max_tokens,
            policy=target.policy,
        )
    return None


def is_over_guard_budget(
    text: str,
    *,
    max_chars: int | None,
    max_tokens: int | None,
) -> bool:
    if max_chars is not None and len(text) > max_chars:
        return True
    if max_tokens is not None and estimate_tokens(text) > max_tokens:
        return True
    return False


def _within_budget(text: str, budget: GuardBudget) -> bool:
    return not is_over_guard_budget(
        text,
        max_chars=budget.max_chars,
        max_tokens=budget.max_tokens,
    )


def _generated_header(text: str) -> str | None:
    stripped = text.lstrip()
    if not stripped:
        return None
    first = stripped.splitlines()[0].strip()
    if first.startswith("<!--") and "generated_by=memory-mcp" in first:
        return first
    return None


_GENERATED_RENDERER_RE = re.compile(r"\brenderer=([A-Za-z0-9_-]+)")


def _stable_generated_header(header: str) -> str:
    """Keep only the stable generated-document identity fields.

    Legacy generated headers carried record IDs, timestamps, configuration
    hashes, and guard method markers. They are volatile provenance rather than
    document content, so retaining them creates needless merge conflicts.
    """
    match = _GENERATED_RENDERER_RE.search(header)
    renderer = match.group(1) if match else ""
    suffix = f" renderer={renderer}" if renderer else ""
    return f"<!-- generated_by=memory-mcp{suffix} -->"


def _canonicalize_generated_header(text: str) -> str:
    """Replace a legacy generated header in-place with its stable form."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if not (stripped.startswith("<!--") and "generated_by=memory-mcp" in stripped):
            return text
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        leading = line[: len(line) - len(line.lstrip())]
        lines[index] = leading + _stable_generated_header(stripped) + newline
        return "".join(lines)
    return text


def _stamp_guard_optimized_header(original: str, candidate: str, method: str) -> str:
    header = _generated_header(original)
    if not header:
        return candidate
    stamped = _stable_generated_header(header)
    lines = candidate.lstrip().splitlines()
    if lines and lines[0].strip().startswith("<!--") and "generated_by=memory-mcp" in lines[0]:
        lines[0] = stamped
        return "\n".join(lines).rstrip() + "\n"
    return (stamped + "\n\n" + candidate.strip() + "\n").rstrip() + "\n"


def _hard_trim_to_budget(text: str, budget: GuardBudget) -> str:
    if _within_budget(text, budget):
        return text

    marker = "\n\n[Guard optimized: details folded; raw records and backups retain the original text.]\n"
    limit_candidates: list[int] = []
    if budget.max_chars is not None:
        limit_candidates.append(max(0, budget.max_chars))
    if budget.max_tokens is not None:
        # The local estimator is approximate and language-dependent. Use a
        # conservative char cap, then verify with the estimator in the loop.
        limit_candidates.append(max(0, int(budget.max_tokens * 3.2)))
    limit = min(limit_candidates) if limit_candidates else len(text)
    limit = min(limit, len(text))

    while limit >= 0:
        available = max(0, limit - len(marker))
        candidate = text[:available].rstrip() + marker
        if _within_budget(candidate, budget):
            return candidate
        limit = int(limit * 0.85) - 1

    return ""


def _compact_by_policy(text: str, policy: str | None) -> str:
    if policy == "hot_task":
        compacted, _notes = _compact_hot_task(text)
    elif policy == "error_summary":
        compacted, _notes = _compact_error_summary(text)
    else:
        compacted, _notes = _compact_warm_context(text)
    return compacted


def _split_blocks(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    prefix: list[str] = []
    body_start = 0
    for idx, line in enumerate(lines[:8]):
        if line.startswith("## "):
            body_start = idx
            break
        prefix.append(line)
        body_start = idx + 1
    blocks: list[str] = []
    current: list[str] = []
    for line in lines[body_start:]:
        if line.startswith("## ") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return prefix, [b for b in blocks if b]


def _pack_generated_document(text: str, budget: GuardBudget) -> str:
    prefix, blocks = _split_blocks(text)
    output = "\n".join(prefix).rstrip() + "\n\n"
    if _within_budget(output, budget):
        base = output
    else:
        return _hard_trim_to_budget(output, budget)

    for block in blocks:
        block = block.rstrip() + "\n\n"
        candidate = base + block
        if _within_budget(candidate, budget):
            base = candidate
            continue
        # Keep the record header and fold the verbose body.
        block_lines = block.splitlines()
        shortened = "\n".join(block_lines[:4]).rstrip()
        if shortened:
            candidate = base + shortened + "\n\n"
            if _within_budget(candidate, budget):
                base = candidate
        break

    if base.strip() == output.strip():
        base += "_No record blocks fit the configured guard budget._\n"
    return _hard_trim_to_budget(base.rstrip() + "\n", budget)


def deterministic_guard_compact(text: str, budget: GuardBudget) -> str:
    if _within_budget(text, budget):
        return text
    if _generated_header(text):
        packed = _pack_generated_document(text, budget)
    else:
        packed = _compact_by_policy(text, budget.policy)
    packed = _stamp_guard_optimized_header(text, packed, "deterministic")
    return _hard_trim_to_budget(packed, budget)


def _llm_guard_compact(
    config: MemoryConfig,
    *,
    rel_path: str,
    text: str,
    budget: GuardBudget,
) -> tuple[str | None, dict[str, Any] | None]:
    from .memory_llm_runner import STATUS_OK, run_llm_capability

    def _invoke(client: Any, profile: Any) -> str:
        system = (
            "You compress project memory Markdown. Preserve facts, decisions, "
            "owners, file paths, dates, risks, and open tasks. Do not invent facts. "
            "The original raw records remain stored elsewhere, so remove repetition "
            "and verbose history. Output Markdown only."
        )
        limit_bits: list[str] = []
        if budget.max_chars is not None:
            limit_bits.append(f"at most {budget.max_chars} characters")
        if budget.max_tokens is not None:
            limit_bits.append(f"about {budget.max_tokens} estimated tokens or less")
        limits = " and ".join(limit_bits) or "as concise as possible"
        prompt = (
            f"Compress this memory document for `{rel_path}` to {limits}.\n\n"
            "Keep high-value bullets grouped by topic. Prefer current state over old discussion.\n\n"
            "DOCUMENT:\n"
            f"{text}"
        )
        kwargs: dict[str, Any] = {"temperature": 0}
        if getattr(profile, "max_tokens", None):
            kwargs["max_tokens"] = int(profile.max_tokens)
        complete_text = getattr(client, "complete_text", None)
        if callable(complete_text):
            return str(complete_text(prompt, system=system, **kwargs)).strip()
        response = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            **kwargs,
        )
        from .memory_llm import extract_text

        return extract_text(response).strip()

    envelope = run_llm_capability(
        config,
        "guard_compaction",
        _invoke,
        force_enabled=True,
    )
    if not envelope.ok:
        return None, envelope.to_dict()
    candidate = str(envelope.value or "").strip()
    if not candidate:
        return None, envelope.to_dict()
    candidate = _stamp_guard_optimized_header(text, candidate, "llm")
    candidate = _hard_trim_to_budget(candidate, budget)
    meta = envelope.to_dict()
    meta["status"] = meta.get("status") or STATUS_OK
    return candidate, meta


def optimize_text_for_guard(
    config: MemoryConfig,
    *,
    rel_path: str,
    text: str,
    force: bool = False,
    prefer_llm: bool = True,
    override_max_chars: int | None = None,
    override_max_tokens: int | None = None,
) -> tuple[str, dict[str, Any]]:
    budget = guard_budget_for_path(config, rel_path)
    if budget is None:
        return text, {"optimized": False, "reason": "no_guard_target"}
    if override_max_chars is not None or override_max_tokens is not None:
        budget = GuardBudget(
            target=budget.target,
            max_chars=(
                min(budget.max_chars, override_max_chars)
                if budget.max_chars is not None and override_max_chars is not None
                else override_max_chars if override_max_chars is not None else budget.max_chars
            ),
            max_tokens=(
                min(budget.max_tokens, override_max_tokens)
                if budget.max_tokens is not None and override_max_tokens is not None
                else override_max_tokens if override_max_tokens is not None else budget.max_tokens
            ),
            policy=budget.policy,
        )
    before = {"chars": len(text), "tokens_est": estimate_tokens(text)}
    if not force and _within_budget(text, budget):
        return text, {
            "optimized": False,
            "reason": "within_budget",
            "method": "none",
            "before": before,
            "after": {"chars": len(text), "tokens_est": estimate_tokens(text)},
        }

    text = _canonicalize_generated_header(text)
    llm_meta: dict[str, Any] | None = None
    method = "deterministic"
    optimized: str | None = None
    if prefer_llm:
        optimized, llm_meta = _llm_guard_compact(
            config,
            rel_path=rel_path,
            text=text,
            budget=budget,
        )
        if optimized:
            method = "llm"

    if not optimized:
        optimized = deterministic_guard_compact(text, budget)
        method = "deterministic"

    optimized = _hard_trim_to_budget(optimized, budget)
    after = {"chars": len(optimized), "tokens_est": estimate_tokens(optimized)}
    return optimized, {
        "optimized": True,
        "method": method,
        "path": rel_path,
        "policy": budget.policy,
        "before": before,
        "after": after,
        "max_chars": budget.max_chars,
        "max_tokens": budget.max_tokens,
        "llm": llm_meta,
    }


def _archive_active_context_original(config: MemoryConfig, rel_path: str, text: str) -> str | None:
    rel = _norm(rel_path)
    if not rel.startswith("memory-bank/activeContext/") or not rel.endswith(".md"):
        return None
    user = Path(rel).stem
    archive_dir = config.repo_root / "memory-bank" / "archive" / "activeContext" / user
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = archive_dir / f"activeContext-{stamp}-{uuid.uuid4().hex[:8]}.md"
    notice = (
        f"# Archived activeContext for `{user}`\n\n"
        f"<!-- archived-by: memory-mcp guard optimizer; source={rel} -->\n\n"
    )
    archive_path.write_text(notice + text, encoding="utf-8")
    return archive_path.relative_to(config.repo_root).as_posix()


def optimize_guard_targets(config: MemoryConfig, *, prefer_llm: bool = True) -> dict[str, Any]:
    """Optimize every currently exceeded guard target in place.

    This is best-effort and safe for startup auto-maintenance: every target is
    backed up before overwrite, and user-scoped activeContext files also get a
    readable archive copy under ``memory-bank/archive/activeContext/<user>``.
    """

    from .memory_guard import memory_guard_check

    guard = memory_guard_check(config)
    if not guard.get("ok"):
        return guard

    actions: list[dict[str, Any]] = []
    guard_items = guard.get("items") or guard.get("targets") or []
    for item in guard_items:
        if item.get("status") != "exceeded":
            continue
        rel_path = str(item.get("path") or "")
        actions.append(
            _optimize_one_path(
                config,
                rel_path,
                prefer_llm=prefer_llm,
                reason="target_exceeded",
            )
        )

    actions.extend(_optimize_for_total_budget(config, prefer_llm=prefer_llm))

    append_event(
        config,
        event_type="guard_optimizer",
        payload={
            "actions": [
                {
                    "path": a.get("path"),
                    "ok": a.get("ok"),
                    "optimized": a.get("optimized"),
                    "method": a.get("method"),
                    "after": a.get("after"),
                }
                for a in actions
            ],
        },
        status="ok" if all(a.get("ok", True) for a in actions) else "error",
    )
    return {
        "ok": all(a.get("ok", True) for a in actions),
        "actions": actions,
        "count": len(actions),
    }


def _optimize_one_path(
    config: MemoryConfig,
    rel_path: str,
    *,
    prefer_llm: bool,
    reason: str,
    override_max_chars: int | None = None,
    override_max_tokens: int | None = None,
) -> dict[str, Any]:
    manager = PathManager(config)
    try:
        target = manager.resolve(rel_path, must_exist=True, must_be_file=True)
    except (PathSecurityError, FileNotFoundError, IsADirectoryError) as exc:
        return error_result("path_not_allowed", str(exc), path=rel_path)
    try:
        source_text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return error_result("read_failed", str(exc), path=rel_path)
    optimized, meta = optimize_text_for_guard(
        config,
        rel_path=rel_path,
        text=source_text,
        force=True,
        prefer_llm=prefer_llm,
        override_max_chars=override_max_chars,
        override_max_tokens=override_max_tokens,
    )
    if not meta.get("optimized") or optimized == source_text:
        return {
            "ok": True,
            "path": rel_path,
            "optimized": False,
            "reason": meta.get("reason") or reason,
        }
    try:
        with file_lock(config.repo_root, target):
            backup_files(
                config,
                [rel_path],
                reason=f"guard_optimizer.pre_overwrite:{reason}",
                tag="guard_optimizer",
                event_type="memory_backup",
                write_event=True,
            )
            archived_to = _archive_active_context_original(config, rel_path, source_text)
            _atomic_write_text(target, optimized, fsync_strict=config.mcp_fsync_strict)
    except LockTimeoutError as exc:
        return error_result("lock_timeout", str(exc), path=rel_path)
    except DiskFullError as exc:
        return error_result("disk_full", str(exc), path=rel_path)
    except OSError as exc:
        return error_result("write_failed", str(exc), path=rel_path)
    action = {"ok": True, "path": rel_path, "optimized": True, "reason": reason, **meta}
    if archived_to:
        action["archived_to"] = archived_to
    return action


def _optimize_for_total_budget(config: MemoryConfig, *, prefer_llm: bool) -> list[dict[str, Any]]:
    from .memory_guard import memory_guard_check

    actions: list[dict[str, Any]] = []
    for _round in range(5):
        guard = memory_guard_check(config)
        total = guard.get("total_budget") if isinstance(guard, dict) else None
        if not isinstance(total, dict) or total.get("status") != "exceeded":
            break
        items = guard.get("items") or guard.get("targets") or []
        candidates = [
            item
            for item in items
            if item.get("status") in {"ok", "warn", "exceeded"}
            and isinstance(item.get("chars"), int)
            and int(item.get("chars") or 0) > 0
            and guard_budget_for_path(config, str(item.get("path") or "")) is not None
        ]
        if not candidates:
            break
        candidates.sort(
            key=lambda item: (int(item.get("tokens_est") or 0), int(item.get("chars") or 0)),
            reverse=True,
        )
        item = candidates[0]
        rel_path = str(item.get("path") or "")
        chars = int(item.get("chars") or 0)
        tokens = int(item.get("tokens_est") or 0)
        max_chars = total.get("max_chars")
        max_tokens = total.get("max_tokens")
        total_chars = int(total.get("total_chars") or 0)
        total_tokens = int(total.get("total_tokens_est") or 0)
        excess_chars = max(0, total_chars - int(max_chars)) if isinstance(max_chars, int) else 0
        excess_tokens = max(0, total_tokens - int(max_tokens)) if isinstance(max_tokens, int) else 0
        override_chars = max(400, min(chars - 1, chars - max(256, excess_chars + 256))) if chars > 500 else None
        override_tokens = max(120, min(tokens - 1, tokens - max(64, excess_tokens + 64))) if tokens > 160 else None
        action = _optimize_one_path(
            config,
            rel_path,
            prefer_llm=prefer_llm,
            reason="total_budget_exceeded",
            override_max_chars=override_chars,
            override_max_tokens=override_tokens,
        )
        actions.append(action)
        if not action.get("optimized"):
            break
    return actions
