from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import nullcontext

from .memory_config import MemoryConfig
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import _atomic_write_text, safe_read_text
from .memory_locks import file_lock
from .memory_request_id import content_sha
from .memory_frontmatter import parse_record_pack_entries, canonical_record
from .memory_identity import canonical_identity
from .memory_result import error_result, ok_result
from .memory_task_context import get_task_ids_for_user

FACET_FIELDS = [
    "derived_from_record_ids",
    "derived_from_snapshot_ids",
    "derived_from_revision_ids",
    "supersedes",
    "conflicts_with",
    "related_artifact_ids",
    "asset_paths",
    "map_names",
    "plugin_names",
    "module_names",
    "class_names",
    "blueprint_paths",
]


def _db_path(config: MemoryConfig) -> Path:
    return (config.repo_root / ".ai-memory" / "search.db").resolve()


def _dirty_path(config: MemoryConfig) -> Path:
    return (config.repo_root / ".ai-memory" / "search-index-dirty.json").resolve()


def _is_record_source_path(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel.startswith("memory-bank/") or not rel.endswith(".md"):
        return False
    if rel in {
        "memory-bank/teamContext.md",
        "memory-bank/progress.md",
        "memory-bank/techContext.md",
        "memory-bank/systemPatterns.md",
        "memory-bank/projectbrief.md",
        "memory-bank/activeContext.md",
    }:
        return False
    if rel.startswith(("memory-bank/activeContext/", "memory-bank/compiled/", "memory-bank/archive/manual-edits/", "memory-bank/archive/activeContext/")):
        return False
    return True


def record_corpus_watermark(config: MemoryConfig) -> str:
    return _snapshot_watermark(_record_corpus_snapshot(config))


def _record_corpus_snapshot(config: MemoryConfig) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    manager = PathManager(config)
    if not (config.repo_root / "memory-bank").is_dir():
        return snapshot
    for abs_path, rel_path in manager.iter_files(scopes=["memory-bank"], include_paths=["memory-bank/**/*.md"]):
        if not _is_record_source_path(rel_path):
            continue
        raw = abs_path.read_bytes()
        # mtime+size 会漏掉保留时间戳且等长的外部改写。索引是可重建派生物，
        # 这里用原始字节摘要换取确定的新鲜度判断。
        snapshot[rel_path] = f"{len(raw)}:{hashlib.sha256(raw).hexdigest()}"
    return snapshot


def _snapshot_watermark(snapshot: dict[str, str]) -> str:
    return content_sha("\n".join(f"{path}:{signature}" for path, signature in sorted(snapshot.items())))


def mark_index_dirty(config: MemoryConfig, *, reason: str, paths: list[str] | None = None) -> None:
    target = _dirty_path(config)
    with file_lock(config.repo_root, target):
        existing: dict[str, Any] = {}
        try:
            if target.is_file():
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
        except (OSError, ValueError, json.JSONDecodeError):
            existing = {}
        merged_paths = sorted({str(item) for item in existing.get("paths", []) if str(item)} | {str(item) for item in (paths or []) if str(item)})
        payload = {
            "dirty": True,
            "reason": str(reason)[:500],
            "paths": merged_paths,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2), fsync_strict=config.mcp_fsync_strict)


def _clear_dirty_paths(config: MemoryConfig, paths: list[str] | None = None) -> None:
    target = _dirty_path(config)
    if not target.exists():
        return
    with file_lock(config.repo_root, target):
        if not target.exists():
            return
        if paths is None:
            target.unlink(missing_ok=True)
            return
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        remaining = sorted(set(str(item) for item in data.get("paths", []) if str(item)) - set(paths))
        if not remaining:
            target.unlink(missing_ok=True)
            return
        data["paths"] = remaining
        _atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2), fsync_strict=config.mcp_fsync_strict)


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


def _cjk_ngrams(text: str) -> list[str]:
    grams: list[str] = []
    length = len(text)
    for size in (2, 3):
        if length < size:
            continue
        grams.extend(text[index : index + size] for index in range(0, length - size + 1))
    if not grams and text:
        grams.append(text)
    return grams


def build_search_text(
    *,
    title: str,
    body: str,
    tags: list[str],
    metadata_values: list[str],
) -> str:
    """Build dependency-free FTS text with CJK n-grams and structured metadata."""
    parts = [title, body, " ".join(tags), " ".join(metadata_values)]
    tokens: list[str] = []

    for part in parts:
        for token in _TOKEN_RE.findall(part):
            if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]+", token):
                tokens.extend(_cjk_ngrams(token))
            else:
                tokens.append(token.lower())

    return " ".join(tokens)


def _connect(config: MemoryConfig) -> sqlite3.Connection:
    """Open the FTS database with WAL + busy-timeout for multi-process safety.

    WAL (Write-Ahead Logging) lets readers continue while a writer holds
    the write lock, which is exactly the pattern when multiple MCP
    server processes (one per VS Code window) hit the same workspace
    index. ``busy_timeout`` then gives concurrent writers a bounded
    grace period before SQLite raises ``database is locked``.

    Both PRAGMAs are idempotent and cheap; they do not require schema
    changes and are safe to apply on every connect.
    """
    path = _db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.DatabaseError:
        # Read-only filesystems / corrupted db: callers will detect via
        # _is_index_healthy and rebuild from scratch.
        pass
    return conn


def _is_index_healthy(config: MemoryConfig) -> bool:
    """Cheap integrity probe so callers can recover from a corrupted db file."""
    path = _db_path(config)
    if not path.exists():
        return True  # nothing to be unhealthy
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _reset_corrupted_index(config: MemoryConfig) -> None:
    """Remove a corrupted index file so the next ``_connect`` builds fresh.

    Windows keeps file handles alive briefly after a connection is GC'd; we
    nudge GC and retry once before giving up so callers don't see a spurious
    ``PermissionError``.
    """
    import gc

    path = _db_path(config)
    gc.collect()
    for attempt in range(2):
        try:
            path.unlink()
            break
        except FileNotFoundError:
            break
        except PermissionError:
            if attempt == 0:
                gc.collect()
                continue
            return
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            pass


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # 完整逻辑记录投影保留每个物理来源，重复来源删除后仍能选出剩余真源。
    conn.execute("""CREATE TABLE IF NOT EXISTS memory_record_payloads (
        path TEXT NOT NULL, id TEXT NOT NULL, metadata_json TEXT NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY(path, id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS payload_record_id ON memory_record_payloads(id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            schema_version TEXT,
            record_kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            author TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            task_id TEXT,
            branch TEXT,
            occurred_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            memory_tier TEXT,
            cognitive_level TEXT,
            importance_score REAL,
            system_area TEXT,
            facets_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_index_sources (
            path TEXT PRIMARY KEY,
            signature TEXT NOT NULL
        )
        """
    )
    _drop_unique_path_constraint_if_needed(conn)
    existing_record_columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_records)").fetchall()}
    column_specs = {
        "schema_version": "TEXT",
        "occurred_at": "TEXT",
        "valid_from": "TEXT",
        "valid_to": "TEXT",
        "memory_tier": "TEXT",
        "cognitive_level": "TEXT",
        "importance_score": "REAL",
        "system_area": "TEXT",
        "facets_json": "TEXT NOT NULL DEFAULT '[]'",
    }
    for column, spec in column_specs.items():
        if column not in existing_record_columns:
            conn.execute(f"ALTER TABLE memory_records ADD COLUMN {column} {spec}")
    existing_fts_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(memory_records_fts)").fetchall()
    }
    if existing_fts_columns and "search_text" not in existing_fts_columns:
        conn.execute("DROP TABLE memory_records_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts USING fts5(
            id UNINDEXED,
            path UNINDEXED,
            title,
            body,
            tags,
            search_text
        )
        """
    )


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO memory_index_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _index_source_snapshot(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(path): str(signature)
        for path, signature in conn.execute("SELECT path, signature FROM memory_index_sources")
    }


def _drop_unique_path_constraint_if_needed(conn: sqlite3.Connection) -> None:
    """Migrate old indexes where ``path`` was unique.

    Record packs intentionally store several logical records in one physical
    file, so ``path`` can no longer be unique. SQLite cannot drop an autoindex
    created by a table-level UNIQUE constraint; rebuild the table in place when
    that legacy constraint is detected.
    """
    try:
        indexes = conn.execute("PRAGMA index_list(memory_records)").fetchall()
    except sqlite3.DatabaseError:
        return
    has_unique_path = False
    for index in indexes:
        if not bool(index[2]):
            continue
        name = str(index[1])
        columns = [str(row[2]) for row in conn.execute(f"PRAGMA index_info({name})").fetchall()]
        if columns == ["path"]:
            has_unique_path = True
            break
    if not has_unique_path:
        return
    conn.execute("ALTER TABLE memory_records RENAME TO memory_records_old")
    conn.execute(
        """
        CREATE TABLE memory_records (
            id TEXT PRIMARY KEY,
            path TEXT NOT NULL,
            schema_version TEXT,
            record_kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            author TEXT NOT NULL,
            tags_json TEXT NOT NULL,
            task_id TEXT,
            branch TEXT,
            occurred_at TEXT,
            valid_from TEXT,
            valid_to TEXT,
            memory_tier TEXT,
            cognitive_level TEXT,
            importance_score REAL,
            system_area TEXT,
            facets_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT,
            updated_at TEXT,
            title TEXT NOT NULL,
            body TEXT NOT NULL
        )
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_records_old)").fetchall()}
    copy_columns = [
        "id",
        "path",
        "schema_version",
        "record_kind",
        "scope",
        "status",
        "author",
        "tags_json",
        "task_id",
        "branch",
        "occurred_at",
        "valid_from",
        "valid_to",
        "memory_tier",
        "cognitive_level",
        "importance_score",
        "system_area",
        "facets_json",
        "created_at",
        "updated_at",
        "title",
        "body",
    ]
    selected = [column for column in copy_columns if column in existing_columns]
    if selected:
        joined = ", ".join(selected)
        conn.execute(f"INSERT OR REPLACE INTO memory_records ({joined}) SELECT {joined} FROM memory_records_old")
    conn.execute("DROP TABLE memory_records_old")


def _metadata_list(metadata: dict[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _facet_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in FACET_FIELDS:
        values.extend(_metadata_list(metadata, key))
    return values


def _metadata_search_values(metadata: dict[str, Any]) -> list[str]:
    values = [
        str(metadata.get("schema_version", "") or ""),
        str(metadata.get("record_kind", "") or ""),
        str(metadata.get("scope", "") or ""),
        str(metadata.get("status", "") or ""),
        canonical_identity(metadata.get("author")),
        str(metadata.get("task_id", "") or ""),
        str(metadata.get("branch", "") or ""),
        str(metadata.get("memory_tier", "") or ""),
        str(metadata.get("cognitive_level", "") or ""),
        str(metadata.get("system_area", "") or ""),
    ]
    values.extend(_facet_values(metadata))
    return values


def _parse_index_source(text: str, rel_path: str) -> list[tuple[dict[str, Any], str]]:
    # 受管理的 pack 必须完整；普通 Markdown 文档可以没有记录 front matter。
    if "memory-record-pack" in text or "/packs/" in rel_path or "/record-packs/" in rel_path:
        from .memory_git_merge import read_pack
        return [(item.metadata, item.body) for item in read_pack(text).values()]
    try:
        return parse_record_pack_entries(text)
    except ValueError:
        if text.startswith("---\n"):
            raise ValueError(f"invalid record source: {rel_path}")
        return []


def _iter_record_files(config: MemoryConfig) -> tuple[list[tuple[str, dict[str, Any], str]], dict[str, int]]:
    manager = PathManager(config)
    records: list[tuple[str, dict[str, Any], str]] = []
    stats = {
        "scanned_files": 0,
        "skipped_non_records": 0,
        "skipped_read_errors": 0,
    }

    for abs_path, rel_path in manager.iter_files(scopes=["memory-bank"], include_paths=["memory-bank/**/*.md"]):
        stats["scanned_files"] += 1
        if not _is_record_source_path(rel_path):
            stats["skipped_non_records"] += 1
            continue
        try:
            text = safe_read_text(abs_path, errors="strict")
            parsed_entries = _parse_index_source(text, rel_path)
        except (OSError, UnicodeError):
            raise
        added = 0
        for metadata, body in parsed_entries:
            if not metadata.get("id") or not metadata.get("record_kind"):
                continue
            records.append((rel_path, metadata, body))
            added += 1
        if added == 0:
            stats["skipped_non_records"] += 1

    return records, stats


def _deduplicate_record_ids(
    records: list[tuple[str, dict[str, Any], str]],
) -> tuple[
    list[tuple[str, dict[str, Any], str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Resolve duplicate logical IDs before the rebuild touches SQLite.

    Parsed-equivalent logical records may legitimately coexist while archive
    ownership is being migrated. They are indexed once using a deterministic
    path and reported to the caller. Different records sharing an ID are a
    corpus integrity error and must fail closed before the valid index is
    replaced.
    """
    grouped: dict[str, list[tuple[str, dict[str, Any], str]]] = {}
    for row in records:
        grouped.setdefault(str(row[1].get("id")), []).append(row)

    unique_records: list[tuple[str, dict[str, Any], str]] = []
    exact_duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for record_id in sorted(grouped):
        rows = sorted(grouped[record_id], key=lambda row: row[0])
        canonical = rows[0]
        if len(rows) == 1:
            unique_records.append(canonical)
            continue

        paths = [row[0] for row in rows]
        equivalent = all(
            canonical_record(row[1], row[2]) == canonical_record(canonical[1], canonical[2])
            for row in rows[1:]
        )
        detail = {
            "id": record_id,
            "occurrences": len(rows),
            "paths": paths,
        }
        if equivalent:
            detail["canonical_path"] = canonical[0]
            exact_duplicates.append(detail)
            unique_records.append(canonical)
        else:
            conflicts.append(detail)

    return unique_records, exact_duplicates, conflicts


def memory_rebuild_index(config: MemoryConfig, *, _attempt: int = 0) -> dict[str, Any]:
    with file_lock(config.repo_root, _db_path(config)):
        return _rebuild_index_locked(config, _attempt=_attempt)


def _rebuild_index_locked(config: MemoryConfig, *, _attempt: int = 0) -> dict[str, Any]:
    """Rebuild the SQLite FTS index from record Markdown files."""
    try:
        source_snapshot = _record_corpus_snapshot(config)
        records, stats = _iter_record_files(config)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    except FileNotFoundError as exc:
        return error_result("not_found", str(exc))
    except (OSError, ValueError) as exc:
        return error_result("invalid_record_source", str(exc))

    source_records = records
    records, exact_duplicates, conflicts = _deduplicate_record_ids(records)
    stats["duplicate_record_ids"] = len(exact_duplicates)
    stats["deduplicated_records"] = sum(
        int(item["occurrences"]) - 1 for item in exact_duplicates
    )
    if conflicts:
        return error_result(
            "duplicate_record_id",
            "record corpus contains conflicting records with the same logical ID",
            conflicts=conflicts,
            stats=stats,
        )

    # If a previous crash / disk error left a corrupted db file, drop it so
    # the rebuild can start from a clean schema instead of failing on every
    # subsequent call.
    if not _is_index_healthy(config):
        _reset_corrupted_index(config)

    try:
        with _connect(config) as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM memory_records")
            conn.execute("DELETE FROM memory_records_fts")
            conn.execute("DELETE FROM memory_index_sources")
            conn.execute("DELETE FROM memory_record_payloads")
            conn.executemany(
                "INSERT OR REPLACE INTO memory_record_payloads VALUES (?, ?, ?, ?)",
                [(path, str(meta["id"]), json.dumps(meta, ensure_ascii=False), body)
                 for path, meta, body in source_records],
            )
            for rel_path, metadata, body in records:
                tags = [str(tag) for tag in metadata.get("tags", []) if str(tag)]
                title = _first_heading(body)
                facets = _facet_values(metadata)
                metadata_values = _metadata_search_values(metadata)
                search_text = build_search_text(
                    title=title,
                    body=body,
                    tags=tags,
                    metadata_values=metadata_values,
                )
                conn.execute(
                    """
                    INSERT INTO memory_records (
                        id, path, schema_version, record_kind, scope, status, author, tags_json,
                        task_id, branch, occurred_at, valid_from, valid_to, memory_tier,
                        cognitive_level, importance_score, system_area, facets_json,
                        created_at, updated_at, title, body
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(metadata.get("id")),
                        rel_path,
                        metadata.get("schema_version"),
                        str(metadata.get("record_kind", "")),
                        str(metadata.get("scope", "")),
                        str(metadata.get("status", "")),
                        canonical_identity(metadata.get("author")),
                        json.dumps(tags, ensure_ascii=False),
                        metadata.get("task_id"),
                        metadata.get("branch"),
                        metadata.get("occurred_at"),
                        metadata.get("valid_from"),
                        metadata.get("valid_to"),
                        metadata.get("memory_tier"),
                        metadata.get("cognitive_level"),
                        metadata.get("importance_score"),
                        metadata.get("system_area"),
                        json.dumps(facets, ensure_ascii=False),
                        metadata.get("created_at"),
                        metadata.get("updated_at"),
                        title,
                        body,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_records_fts (id, path, title, body, tags, search_text)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(metadata.get("id")),
                        rel_path,
                        title,
                        body,
                        " ".join(tags),
                        search_text,
                    ),
                )
            conn.executemany(
                "INSERT INTO memory_index_sources (path, signature) VALUES (?, ?)",
                sorted(source_snapshot.items()),
            )
            watermark = _snapshot_watermark(source_snapshot)
            _set_meta(conn, "corpus_watermark", watermark)
            _set_meta(conn, "projection_version", "1")
            _set_meta(conn, "projection_watermark", watermark)
            _set_meta(conn, "built_at", datetime.now(timezone.utc).isoformat())
            _set_meta(conn, "config_hash", config.config_hash)
            conn.execute("COMMIT")
    except sqlite3.Error as exc:
        return error_result("index_failed", f"failed to rebuild index: {exc}")

    try:
        end_snapshot = _record_corpus_snapshot(config)
    except (OSError, PathSecurityError) as exc:
        mark_index_dirty(config, reason=f"post-rebuild corpus scan failed: {exc}")
        return error_result("index_raced", "record corpus could not be verified after rebuild")
    if end_snapshot != source_snapshot:
        mark_index_dirty(config, reason="record corpus changed during index rebuild")
        if _attempt < 2:
            return memory_rebuild_index(config, _attempt=_attempt + 1)
        return error_result("index_raced", "record corpus changed during three consecutive rebuild attempts")

    _clear_dirty_paths(config)

    return ok_result(
        "index rebuilt",
        indexed_records=len(records),
        db_path=_db_path(config).relative_to(config.repo_root).as_posix(),
        stats=stats,
        duplicate_records=exact_duplicates,
        corpus_watermark=_snapshot_watermark(source_snapshot),
        rebuild_attempts=_attempt + 1,
    )


def _index_record_rows(config: MemoryConfig, rows: list[tuple[str, dict[str, Any], str]], *, connection=None) -> None:
    with (nullcontext(connection) if connection is not None else _connect(config)) as conn:
        _ensure_schema(conn)
        for rel_path, metadata, body in rows:
            tags = [str(tag) for tag in metadata.get("tags", []) if str(tag)]
            title = _first_heading(body)
            facets = _facet_values(metadata)
            metadata_values = _metadata_search_values(metadata)
            search_text = build_search_text(title=title, body=body, tags=tags, metadata_values=metadata_values)
            conn.execute("DELETE FROM memory_records WHERE id = ?", (str(metadata.get("id")),))
            conn.execute("DELETE FROM memory_records_fts WHERE id = ?", (str(metadata.get("id")),))
            conn.execute(
                """
                INSERT INTO memory_records (
                    id, path, schema_version, record_kind, scope, status, author, tags_json,
                    task_id, branch, occurred_at, valid_from, valid_to, memory_tier,
                    cognitive_level, importance_score, system_area, facets_json,
                    created_at, updated_at, title, body
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(metadata.get("id")),
                    rel_path,
                    metadata.get("schema_version"),
                    str(metadata.get("record_kind", "")),
                    str(metadata.get("scope", "")),
                    str(metadata.get("status", "")),
                    canonical_identity(metadata.get("author")),
                    json.dumps(tags, ensure_ascii=False),
                    metadata.get("task_id"),
                    metadata.get("branch"),
                    metadata.get("occurred_at"),
                    metadata.get("valid_from"),
                    metadata.get("valid_to"),
                    metadata.get("memory_tier"),
                    metadata.get("cognitive_level"),
                    metadata.get("importance_score"),
                    metadata.get("system_area"),
                    json.dumps(facets, ensure_ascii=False),
                    metadata.get("created_at"),
                    metadata.get("updated_at"),
                    title,
                    body,
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_records_fts (id, path, title, body, tags, search_text)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(metadata.get("id")), rel_path, title, body, " ".join(tags), search_text),
            )


def memory_update_index(config: MemoryConfig, *, paths: list[str]) -> dict[str, Any]:
    from .memory_index_incremental import refresh_paths
    return refresh_paths(config, paths)


def ensure_index_fresh(config: MemoryConfig) -> dict[str, Any]:
    from .memory_index_incremental import ensure_fresh
    return ensure_fresh(config)


def _escape_fts5_token(token: str) -> str:
    """Wrap a token in double quotes for FTS5 MATCH, escaping internal quotes.

    FTS5 treats wide ranges of characters as syntax (`-`, `:`, `*`, `(`, `)`, etc.)
    plus the bareword keywords AND/OR/NOT/NEAR. Quoting every token as a phrase
    eliminates that surface entirely and is safe for both ASCII and CJK content.
    """
    return '"' + token.replace('"', '""') + '"'


def build_fts5_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from a free-form user query.

    The query is normalized through the same tokenizer used at index time
    (Latin/digit tokens lower-cased, CJK runs expanded into bigrams/trigrams)
    and every token is wrapped as a phrase before being joined by spaces (AND).
    Empty queries return an empty string so callers can short-circuit.
    """
    search_text = build_search_text(title=query, body="", tags=[], metadata_values=[])
    tokens = [token for token in search_text.split() if token]
    if not tokens:
        return ""
    return " ".join(_escape_fts5_token(token) for token in tokens)


def _private_visibility_sql(user: str | None, params: list[Any], *, config: MemoryConfig) -> str:
    if not user:
        return ""
    user = canonical_identity(user)
    task_ids = sorted(get_task_ids_for_user(config, user))
    clauses = ["r.author = ?"]
    params.append(user)
    if task_ids:
        clauses.append(f"r.task_id IN ({_sql_placeholders(task_ids)})")
        params.extend(task_ids)
    return "AND (r.scope NOT IN ('personal', 'session', 'user_private') OR " + " OR ".join(clauses) + ")"


def _query_index(conn: sqlite3.Connection, config: MemoryConfig, query: str, top_k: int, *, user: str | None = None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    match_expr = build_fts5_match_query(query)
    if not match_expr:
        return []
    params: list[Any] = [match_expr]
    visibility = _private_visibility_sql(user, params, config=config)
    params.append(top_k)
    return conn.execute(
        f"""
        SELECT
            r.id,
            r.path,
            r.schema_version,
            r.record_kind,
            r.scope,
            r.status,
            r.author,
            r.tags_json,
            r.task_id,
            r.branch,
            r.occurred_at,
            r.valid_from,
            r.valid_to,
            r.memory_tier,
            r.cognitive_level,
            r.importance_score,
            r.system_area,
            r.facets_json,
            r.title,
            snippet(memory_records_fts, 3, '[', ']', ' ... ', 12) AS snippet,
            bm25(memory_records_fts) AS rank
        FROM memory_records_fts
        JOIN memory_records AS r ON r.id = memory_records_fts.id
        WHERE memory_records_fts MATCH ?
        {visibility}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()


def _sql_placeholders(values: list[str]) -> str:
    return ", ".join("?" for _ in values)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _facet_like_pattern(value: str) -> str:
    # Facets are stored as a JSON array of scalar strings. Matching the
    # JSON-quoted value avoids most substring false positives; retrieval still
    # verifies field-level facets against Markdown truth after prefiltering.
    return "%" + _escape_like(json.dumps(value, ensure_ascii=False)) + "%"


def prefilter_record_paths(
    config: MemoryConfig,
    *,
    include_scopes: list[str],
    include_statuses: list[str],
    user: str | None = None,
    task_id: str | None = None,
    branch: str | None = None,
    system_area: str | None = None,
    facet_filters: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Return candidate record paths from the derived SQLite metadata index.

    源字节水位已验证；返回事务一致的完整投影。损坏源或同 ID 冲突必须
    报错，不能把不完整或歧义内容作为可信上下文。
    """
    db_file = _db_path(config)
    freshness = ensure_index_fresh(config)
    if not freshness.get("ok"):
        return freshness

    scopes = [str(item) for item in include_scopes if str(item)]
    statuses = [str(item) for item in include_statuses if str(item)]
    if not scopes or not statuses:
        return ok_result("record paths prefiltered", paths=[], stats={"prefiltered_records": 0})

    where = [
        f"scope IN ({_sql_placeholders(scopes)})",
        f"status IN ({_sql_placeholders(statuses)})",
    ]
    params: list[Any] = [*scopes, *statuses]

    if user:
        user = canonical_identity(user)
        task_ids = sorted(get_task_ids_for_user(config, user))
        clauses = ["author = ?"]
        params.append(user)
        if task_ids:
            clauses.append(f"task_id IN ({_sql_placeholders(task_ids)})")
            params.extend(task_ids)
        where.append("(scope NOT IN ('personal', 'session', 'user_private') OR " + " OR ".join(clauses) + ")")
    if task_id:
        where.append("(task_id IS NULL OR task_id = ?)")
        params.append(task_id)
    if branch:
        where.append("(branch IS NULL OR branch = ?)")
        params.append(branch)
    if system_area:
        where.append("system_area = ?")
        params.append(system_area)

    for _field, expected_values in (facet_filters or {}).items():
        values = [str(item).strip() for item in expected_values if str(item).strip()]
        if not values:
            continue
        clauses = []
        for value in values:
            clauses.append("facets_json LIKE ? ESCAPE '\\'")
            params.append(_facet_like_pattern(value))
        where.append("(" + " OR ".join(clauses) + ")")

    sql = ("SELECT r.path, r.id, p.metadata_json, p.body FROM (SELECT * FROM memory_records WHERE "
           + " AND ".join(where) + ") AS r LEFT JOIN memory_record_payloads AS p "
           "ON p.path=r.path AND p.id=r.id ORDER BY r.path, r.id")
    try:
        with _connect(config) as conn:
            _ensure_schema(conn)
            # 数据与水位来自同一读事务，不能把新水位贴到旧检索结果上。
            conn.execute("BEGIN")
            rows = conn.execute(sql, params).fetchall()
            watermark = conn.execute("SELECT value FROM memory_index_meta WHERE key='corpus_watermark'").fetchone()
            projection = conn.execute("SELECT value FROM memory_index_meta WHERE key='projection_watermark'").fetchone()
            # 旧端不持有新端文件锁；严格检查同一 SQLite 快照，避免旧正文配上新水位。
            if watermark is None or projection is None or watermark[0] != projection[0]:
                return error_result("index_projection_incomplete", "legacy writer changed the index after freshness verification; retry the read")
            if any(row[2] is None for row in rows):
                return error_result("index_projection_incomplete", "record payloads do not match index rows")
            payloads = [{"path": str(path), "metadata": json.loads(metadata), "body": body}
                        for path, record_id, metadata, body in rows]
    except (sqlite3.Error, ValueError) as exc:
        return error_result("index_failed", f"failed to prefilter record paths: {exc}")

    paths = [str(row[0]) for row in rows]
    return ok_result(
        "record paths prefiltered",
        paths=paths,
        records=payloads,
        corpus_watermark=watermark[0] if watermark else None,
        stats={
            "prefiltered_records": len(paths),
            "changed_sources": freshness.get("changed_sources", 0),
            "db_path": db_file.relative_to(config.repo_root).as_posix(),
        },
    )


def record_paths_for_exact_task(
    config: MemoryConfig,
    *,
    task_id: str,
    include_scopes: list[str],
) -> dict[str, Any]:
    """Return indexed record paths whose task and scope match exactly."""
    task = str(task_id or "").strip()
    scopes = [str(item).strip() for item in include_scopes if str(item).strip()]
    if not task or not scopes:
        return ok_result("task record paths selected", paths=[])
    db_file = _db_path(config)
    if not db_file.exists():
        return error_result("index_missing", "record index does not exist")
    freshness = ensure_index_fresh(config)
    if not freshness.get("ok"):
        return freshness
    sql = (
        "SELECT path FROM memory_records WHERE task_id = ? "
        f"AND scope IN ({_sql_placeholders(scopes)}) ORDER BY path"
    )
    try:
        with _connect(config) as conn:
            _ensure_schema(conn)
            rows = conn.execute(sql, [task, *scopes]).fetchall()
    except sqlite3.Error as exc:
        return error_result("index_failed", f"failed to select task record paths: {exc}")
    return ok_result("task record paths selected", paths=[str(row[0]) for row in rows])


def memory_search_records(config: MemoryConfig, query: str, *, user: str | None = None, top_k: int | None = None) -> dict[str, Any]:
    query_normalized = query.strip()
    if not query_normalized:
        return error_result("invalid_input", "query must not be empty")
    if top_k is None:
        top_k = 10
    if top_k <= 0:
        return error_result("invalid_input", "top_k must be > 0")

    db_file = _db_path(config)
    freshness = ensure_index_fresh(config)
    if not freshness.get("ok"):
        return freshness

    try:
        with _connect(config) as conn:
            _ensure_schema(conn)
            rows = _query_index(conn, config, query_normalized, top_k, user=user)
    except sqlite3.Error as exc:
        return error_result("search_failed", f"failed to search record index: {exc}")

    results: list[dict[str, Any]] = []
    for row in rows:
        tags = json.loads(row["tags_json"]) if row["tags_json"] else []
        facets = json.loads(row["facets_json"]) if row["facets_json"] else []
        results.append(
            {
                "id": row["id"],
                "path": row["path"],
                "schema_version": row["schema_version"],
                "record_kind": row["record_kind"],
                "scope": row["scope"],
                "status": row["status"],
                "author": row["author"],
                "tags": tags,
                "task_id": row["task_id"],
                "branch": row["branch"],
                "occurred_at": row["occurred_at"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
                "memory_tier": row["memory_tier"],
                "cognitive_level": row["cognitive_level"],
                "importance_score": row["importance_score"],
                "system_area": row["system_area"],
                "facets": facets,
                "title": row["title"],
                "snippet": row["snippet"],
                "score": float(-row["rank"]),
            }
        )

    return ok_result(
        "record search completed",
        query=query,
        results=results,
        stats={
            "total_hits": len(results),
            "returned_hits": len(results),
            "db_path": db_file.relative_to(config.repo_root).as_posix(),
        },
    )
