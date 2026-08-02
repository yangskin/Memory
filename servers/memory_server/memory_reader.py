from __future__ import annotations

from datetime import datetime, timezone

from .memory_config import MemoryConfig
from .memory_events import get_current_user
from .memory_paths import PathManager, PathSecurityError, resolve_user_path
from .memory_record_io import safe_read_text
from .memory_result import error_result, ok_result


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def memory_get(
    config: MemoryConfig,
    path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_chars: int | None = None,
) -> dict:
    manager = PathManager(config)

    # user_scoped 路径重定向：多人安全策略始终开启。
    effective_path = path
    if config.multi_user and config.multi_user.user_scoped_paths:
        normalized = path.replace("\\", "/").strip("/")
        for scoped in config.multi_user.user_scoped_paths:
            scoped_norm = scoped.replace("\\", "/").strip("/")
            if scoped_norm == normalized or normalized.endswith(scoped_norm):
                current_user = get_current_user(config.repo_root)
                if current_user and current_user != "unknown":
                    effective_path = resolve_user_path(config, path, current_user)
                break

    try:
        resolved = manager.resolve(effective_path, must_exist=True, must_be_file=True)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))
    except IsADirectoryError as exc:
        return error_result("invalid_path", str(exc))

    text = safe_read_text(resolved, errors="replace")
    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    if total_lines == 0:
        return ok_result(
            "file is empty",
            path=manager.to_repo_relative(resolved),
            content="",
            start_line=1,
            end_line=0,
            truncated=False,
            meta={
                "size": resolved.stat().st_size,
                "mtime": datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc).isoformat(),
            },
        )

    actual_start = start_line if start_line is not None else 1
    actual_end = end_line if end_line is not None else total_lines

    if actual_start < 1:
        return error_result("invalid_input", "start_line must be >= 1")
    if actual_end < actual_start:
        return error_result("invalid_input", "end_line must be >= start_line")

    actual_start = min(actual_start, total_lines)
    actual_end = min(actual_end, total_lines)
    selected = "".join(lines[actual_start - 1 : actual_end])

    truncated = False
    if max_chars is not None:
        if max_chars < 0:
            return error_result("invalid_input", "max_chars must be >= 0")
        if len(selected) > max_chars:
            selected = selected[:max_chars]
            truncated = True
            included_lines = _count_lines(selected)
            actual_end = actual_start + max(0, included_lines - 1)

    stat = resolved.stat()
    return ok_result(
        "read completed",
        path=manager.to_repo_relative(resolved),
        content=selected,
        start_line=actual_start,
        end_line=actual_end,
        truncated=truncated,
        meta={
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        },
    )
