"""P1-3: shared-append auto-compact (v0.6.0 OOTB hardening).

When a shared append-only file (e.g. ``memory-bank/progress.md``) grows
past a configurable threshold, fold older entries into a weekly
archive ``memory-bank/archive/<basename>-YYYYWW.md`` and replace the
live file with a short banner + the most recent N lines.

Pure helper: callers (auto-maintenance, manual CLI) decide *when* to
run; this module owns *how*.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig

_DEFAULT_KEEP_LINES: int = 200
_DEFAULT_THRESHOLD_LINES: int = 2000
_BANNER_PREFIX = "<!-- archived-by: memory-mcp shared-append auto-compact"


def _iso_week_tag(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}W{iso.week:02d}"


def _looks_like_archive_banner(text: str) -> bool:
    return text.lstrip().startswith(_BANNER_PREFIX)


def needs_compaction(path: Path, threshold_lines: int = _DEFAULT_THRESHOLD_LINES) -> bool:
    if not path.is_file():
        return False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            count = sum(1 for _ in fh)
    except OSError:
        return False
    return count > threshold_lines


def auto_compact_shared_file(
    config: MemoryConfig,
    path: Path,
    *,
    threshold_lines: int = _DEFAULT_THRESHOLD_LINES,
    keep_lines: int = _DEFAULT_KEEP_LINES,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Idempotent fold of long shared append-only files.

    - Returns ``{ok: True, action: "skipped", reason: ...}`` when not needed.
    - Returns ``{ok: True, action: "archived", archive_path: ..., kept: N}``
      after a successful fold.
    """
    if not path.is_file():
        return {"ok": False, "error": "not_found", "path": str(path)}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": "read_failed", "message": str(exc)}

    lines = text.splitlines(keepends=True)
    if len(lines) <= threshold_lines:
        return {"ok": True, "action": "skipped", "reason": "below_threshold", "lines": len(lines)}

    moment = now or datetime.now(timezone.utc)
    week_tag = _iso_week_tag(moment)
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{path.stem}-{week_tag}.md"

    keep = lines[-keep_lines:] if keep_lines > 0 else []
    archived_lines = lines[: len(lines) - len(keep)]

    # Append (not overwrite) so multiple compactions in the same week
    # accumulate rather than clobber.
    with archive_path.open("a", encoding="utf-8") as fh:
        if archive_path.stat().st_size == 0:
            fh.write(
                f"# Archive of `{path.name}` — {week_tag}\n\n"
                f"<!-- generated-by: memory-mcp shared-append auto-compact -->\n\n"
            )
        fh.writelines(archived_lines)

    banner = (
        f"{_BANNER_PREFIX}; archived={len(archived_lines)} lines; "
        f"kept={len(keep)} lines; archive={archive_path.name}; ts={moment.isoformat()} -->\n\n"
    )
    new_content = banner + "".join(keep)

    # Atomic replace via temp file in the same directory.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    tmp_path.replace(path)

    return {
        "ok": True,
        "action": "archived",
        "archive_path": str(archive_path),
        "lines_archived": len(archived_lines),
        "lines_kept": len(keep),
    }
