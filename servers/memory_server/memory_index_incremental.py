"""同一 SQLite 事务更新 FTS、完整记录投影与源水位，不修改 Markdown。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .memory_config import MemoryConfig
from .memory_frontmatter import parse_record_pack_entries
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_result import error_result, ok_result


def _chunks(values, size=300):
    items = sorted(values)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def refresh_paths(config: MemoryConfig, paths: list[str], *, allow_missing=False,
                  expected: dict[str, str] | None = None) -> dict[str, Any]:
    from . import memory_record_index as idx
    if not isinstance(paths, list) or not paths or not all(isinstance(path, str) for path in paths):
        return error_result("invalid_input", "paths must be a non-empty list of strings")
    try:
        with file_lock(config.repo_root, idx._db_path(config)):
            return _refresh_locked(config, paths, allow_missing=allow_missing, expected=expected)
    except (OSError, ValueError, sqlite3.Error, PathSecurityError) as exc:
        return error_result("index_failed", f"incremental index update failed: {exc}")


def _refresh_locked(config, paths, *, allow_missing, expected):
    from . import memory_record_index as idx
    manager = PathManager(config)
    source_rows = []
    signatures = {}
    with idx._connect(config) as conn:
        idx._ensure_schema(conn)
        meta = dict(conn.execute("SELECT key, value FROM memory_index_meta"))
        # 旧端可更新传统表与水位，却不知道完整正文投影；新端写入不能覆盖这个失同步信号。
        if (meta.get("projection_watermark") != meta.get("corpus_watermark") or
            (meta.get("projection_version") != "1" and conn.execute("SELECT count(*) FROM memory_records").fetchone()[0])):
            return idx.memory_rebuild_index(config)

    for path in sorted(set(paths)):
        absolute = manager.resolve(path, must_exist=not allow_missing, must_be_file=not allow_missing)
        relative = manager.to_repo_relative(absolute)
        if not idx._is_record_source_path(relative):
            continue
        if not absolute.exists():
            if expected is not None and relative in expected:
                return error_result("index_raced", "source disappeared after freshness snapshot", path=relative)
            signatures[relative] = None
            continue
        raw = absolute.read_bytes()
        signature = f"{len(raw)}:{hashlib.sha256(raw).hexdigest()}"
        if expected is not None and expected.get(relative) != signature:
            return error_result("index_raced", "source changed after freshness snapshot", path=relative)
        signatures[relative] = signature
        text = raw.decode("utf-8").replace("\r\n", "\n")
        parsed = idx._parse_index_source(text, relative)
        for metadata, body in parsed:
            if metadata.get("id") and metadata.get("record_kind"):
                source_rows.append((relative, metadata, body))

    unique, duplicates, conflicts = idx._deduplicate_record_ids(source_rows)
    if conflicts:
        return error_result("duplicate_record_id", "conflicting records in changed sources", conflicts=conflicts)

    with idx._connect(config) as conn:
        idx._ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        transaction_meta = dict(conn.execute("SELECT key, value FROM memory_index_meta"))
        # 解析文件期间旧端也可能提交；取得 SQLite 写锁后再核对一次。
        if transaction_meta.get("projection_watermark") != transaction_meta.get("corpus_watermark"):
            conn.execute("ROLLBACK")
            return idx.memory_rebuild_index(config)
        affected = {str(row[1]["id"]) for row in source_rows}
        for path in signatures:
            affected.update(row[0] for row in conn.execute("SELECT id FROM memory_record_payloads WHERE path = ?", (path,)))
            conn.execute("DELETE FROM memory_record_payloads WHERE path = ?", (path,))
        conn.executemany("INSERT OR REPLACE INTO memory_record_payloads VALUES (?, ?, ?, ?)",
                         [(path, str(metadata['id']), json.dumps(metadata, ensure_ascii=False), body)
                          for path, metadata, body in source_rows])
        candidates = []
        for part in _chunks(affected):
            placeholders = ','.join('?' for _ in part)
            candidates.extend((path, json.loads(metadata), body) for path, metadata, body in conn.execute(
                f"SELECT path, metadata_json, body FROM memory_record_payloads WHERE id IN ({placeholders})", part))
        canonical, duplicates, conflicts = idx._deduplicate_record_ids(candidates)
        if conflicts:
            conn.execute("ROLLBACK")
            return error_result("duplicate_record_id", "conflicting records across sources", conflicts=conflicts)
        for part in _chunks(affected):
            placeholders = ','.join('?' for _ in part)
            conn.execute(f"DELETE FROM memory_records WHERE id IN ({placeholders})", part)
            conn.execute(f"DELETE FROM memory_records_fts WHERE id IN ({placeholders})", part)
        idx._index_record_rows(config, canonical, connection=conn)
        for path, signature in signatures.items():
            if signature is None:
                conn.execute("DELETE FROM memory_index_sources WHERE path = ?", (path,))
            else:
                conn.execute("INSERT OR REPLACE INTO memory_index_sources VALUES (?, ?)", (path, signature))
        # 本地写入仅更新已知源；查询仍会严格发现其他进程/Git 的变化。
        watermark = idx._snapshot_watermark(idx._index_source_snapshot(conn))
        idx._set_meta(conn, "corpus_watermark", watermark)
        idx._set_meta(conn, "projection_watermark", watermark)
        idx._set_meta(conn, "projection_version", "1")
        idx._set_meta(conn, "built_at", datetime.now(timezone.utc).isoformat())
        idx._set_meta(conn, "config_hash", config.config_hash)
        conn.execute("COMMIT")
    idx._clear_dirty_paths(config, list(signatures))
    return ok_result("index incrementally updated", indexed_records=len(unique),
                     changed_sources=len(signatures), skipped_records=0,
                     duplicate_records=duplicates, corpus_watermark=watermark,
                     db_path=idx._db_path(config).relative_to(config.repo_root).as_posix())


def ensure_fresh(config: MemoryConfig, *, _attempt: int = 0) -> dict[str, Any]:
    from . import memory_record_index as idx
    if not idx._db_path(config).exists() or not idx._is_index_healthy(config):
        return idx.memory_rebuild_index(config)
    try:
        # 发现阶段读原始字节摘要，保留对等长/保留 mtime 外部改写的保证。
        current = idx._record_corpus_snapshot(config)
        with file_lock(config.repo_root, idx._db_path(config)):
            with idx._connect(config) as conn:
                idx._ensure_schema(conn)
                indexed = idx._index_source_snapshot(conn)
                meta = dict(conn.execute("SELECT key, value FROM memory_index_meta"))
            if (meta.get("projection_version") != "1" or
                meta.get("projection_watermark") != meta.get("corpus_watermark")):
                return idx.memory_rebuild_index(config)
            changed = sorted(path for path in set(indexed) | set(current) if indexed.get(path) != current.get(path))
            if changed:
                result = refresh_paths(config, changed, allow_missing=True, expected=current)
                if not result.get("ok"):
                    if result.get("error") == "index_raced" and _attempt < 2:
                        return ensure_fresh(config, _attempt=_attempt + 1)
                    return result
            else:
                result = ok_result("record index is fresh", changed_sources=0,
                                   corpus_watermark=idx._snapshot_watermark(current))
            idx._clear_dirty_paths(config)
            result["indexed_sources"] = len(current)
            return result
    except (OSError, ValueError, PathSecurityError, sqlite3.Error) as exc:
        return error_result("index_check_failed", f"failed to verify index: {exc}")
