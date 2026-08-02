from __future__ import annotations

import re
from pathlib import Path

from .memory_config import MemoryConfig
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import safe_read_text
from .memory_result import error_result, ok_result

_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".uasset",
    ".umap",
    ".wav",
    ".mp3",
    ".mp4",
    ".dll",
    ".exe",
    ".zip",
    ".7z",
    ".gz",
    ".bin",
    ".pdf",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
}


def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _terms(query: str) -> list[str]:
    """Extract search tokens from query, supporting both ASCII and CJK characters."""
    return [token for token in re.findall(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff\u3400-\u4dbf]+", query.lower()) if token]


def memory_search(
    config: MemoryConfig,
    query: str,
    *,
    scopes: list[str] | None = None,
    top_k: int | None = None,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> dict:
    query_normalized = query.strip().lower()
    if not query_normalized:
        return error_result("invalid_input", "query must not be empty")

    if top_k is None:
        top_k = 10
    if top_k <= 0:
        return error_result("invalid_input", "top_k must be > 0")

    manager = PathManager(config)
    terms = _terms(query_normalized)
    all_matches: list[dict] = []
    matched_files: set[str] = set()
    stats = {
        "scanned_files": 0,
        "skipped_large_files": 0,
        "skipped_binary_files": 0,
        "skipped_read_errors": 0,
    }

    try:
        file_iter = manager.iter_files(scopes=scopes, include_paths=include_paths, exclude_paths=exclude_paths)
        for abs_path, rel_path in file_iter:
            stats["scanned_files"] += 1
            try:
                size = abs_path.stat().st_size
            except OSError:
                stats["skipped_read_errors"] += 1
                continue

            if size > config.max_file_size_bytes:
                stats["skipped_large_files"] += 1
                continue
            if config.skip_binary_files and _is_binary(abs_path):
                stats["skipped_binary_files"] += 1
                continue

            try:
                text = safe_read_text(abs_path, errors="replace")
            except OSError:
                stats["skipped_read_errors"] += 1
                continue

            all_lines = text.splitlines()
            total_lines = len(all_lines)
            file_bonus = 2 if query_normalized in rel_path.lower() else 0
            file_hits: list[dict] = []
            for line_no, line in enumerate(all_lines, start=1):
                line_lower = line.lower()
                score = file_bonus
                if query_normalized in line_lower:
                    score += 5
                term_hits = sum(1 for term in terms if term in line_lower)
                score += term_hits
                if line.lstrip().startswith("#"):
                    score += 2 * term_hits
                if score <= 0:
                    continue
                # Build context window: ±2 lines around the match
                ctx_start = max(1, line_no - 2)
                ctx_end = min(total_lines, line_no + 2)
                snippet_lines = all_lines[ctx_start - 1 : ctx_end]
                snippet = "\n".join(l.rstrip() for l in snippet_lines)[:480]
                file_hits.append(
                    {
                        "path": rel_path,
                        "start_line": ctx_start,
                        "end_line": ctx_end,
                        "snippet": snippet,
                        "score": float(score),
                    }
                )
            # Merge overlapping context windows within the same file
            file_hits.sort(key=lambda h: h["start_line"])
            merged_hits: list[dict] = []
            for hit in file_hits:
                if merged_hits and hit["start_line"] <= merged_hits[-1]["end_line"] + 1:
                    prev = merged_hits[-1]
                    new_end = max(prev["end_line"], hit["end_line"])
                    merged_snippet_lines = all_lines[prev["start_line"] - 1 : new_end]
                    prev["end_line"] = new_end
                    prev["snippet"] = "\n".join(l.rstrip() for l in merged_snippet_lines)[:480]
                    prev["score"] = max(prev["score"], hit["score"])
                else:
                    merged_hits.append(dict(hit))
            for hit in merged_hits:
                matched_files.add(rel_path)
                all_matches.append(hit)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))

    all_matches.sort(key=lambda item: (-item["score"], item["path"], item["start_line"]))
    trimmed = all_matches[:top_k]

    stats["total_hits"] = len(all_matches)
    stats["matched_files"] = len(matched_files)
    stats["returned_hits"] = len(trimmed)

    return ok_result(
        "search completed",
        query=query,
        results=trimmed,
        stats=stats,
    )
