from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_config import MemoryConfig
from .memory_events import append_event, get_current_user
from .memory_frontmatter import PACK_HEADER, parse_record_pack_entries, render_record_pack_entry
from .memory_identity import canonical_identity
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import _atomic_write_text, iter_record_files, refresh_index_if_exists
from .memory_request_id import content_sha
from .memory_records import parse_record_markdown, render_record_markdown
from .memory_result import error_result, ok_result

_DATE_PACK_RE = re.compile(r"(?P<date>\d{8})-\d{3}\.md$")
_MEM_RECORD_RE = re.compile(r"^mem_(?P<date>\d{8})_")


@dataclass(frozen=True)
class PackEntry:
    record_id: str
    metadata: dict[str, Any]
    body: str

    @property
    def rendered(self) -> str:
        return render_record_markdown(self.metadata, self.body)

    @property
    def packed(self) -> str:
        return render_record_pack_entry(self.record_id, self.rendered)


@dataclass
class _PackState:
    ids: set[str]
    char_count: int
    ends_with_newline: bool
    size_bytes: int


def _is_pack_rel_path(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/")
    return "/packs/" in rel or rel.startswith("memory-bank/archive/record-packs/")


def _is_archive_pack_rel_path(rel_path: str) -> bool:
    return rel_path.replace("\\", "/").startswith("memory-bank/archive/record-packs/")


def _record_date(metadata: dict[str, Any], record_id: str, fallback_ts: float) -> datetime:
    created = str(metadata.get("created_at") or metadata.get("occurred_at") or "").strip()
    if created:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            pass
    match = _MEM_RECORD_RE.match(record_id)
    if match:
        try:
            return datetime.strptime(match.group("date"), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_ts, timezone.utc)


def _pack_rel_path_for_entry(source_rel_path: str, entry: PackEntry, fallback_ts: float, index: int) -> str:
    source = Path(source_rel_path.replace("\\", "/"))
    date_label = _record_date(entry.metadata, entry.record_id, fallback_ts).strftime("%Y%m%d")
    return (source.parent / "packs" / f"{date_label}-{index:03d}.md").as_posix()


def _archive_rel_path(user: str, bucket: str, source_sha: str, fragment_index: int) -> str:
    """Return an immutable archive shard path for one source pack fragment.

    The source content digest makes simultaneous archive runs on different
    devices produce different paths when their source packs differ. Reusing
    the same digest is safe because the expected shard content is identical.
    """

    return f"memory-bank/archive/record-packs/{user}/{bucket}-{source_sha[:16]}-{fragment_index:03d}.md"


def _render_archive_shard(entries: list[PackEntry]) -> str:
    """Render a complete archive shard without mutating an existing pack."""

    content = f"{PACK_HEADER}\n\n"
    for entry in entries:
        if content != f"{PACK_HEADER}\n\n":
            if not content.endswith("\n"):
                content += "\n"
            content += "\n"
        content += entry.packed
    return content


def _partition_archive_entries(entries: list[PackEntry], max_chars: int) -> list[list[PackEntry]] | dict[str, Any]:
    """Split one source pack into bounded immutable archive fragments."""

    fragments: list[list[PackEntry]] = []
    current: list[PackEntry] = []
    for entry in entries:
        candidate = [*current, entry]
        if len(_render_archive_shard(candidate)) <= max_chars:
            current = candidate
            continue
        if not current or len(_render_archive_shard([entry])) > max_chars:
            return error_result("record_too_large", f"record {entry.record_id} is larger than target pack size")
        fragments.append(current)
        current = [entry]
    if current:
        fragments.append(current)
    return fragments


def _write_immutable_archive_shard(
    config: MemoryConfig,
    *,
    rel_path: str,
    content: str,
    dry_run: bool,
) -> str | dict[str, Any]:
    """Create one archive shard, accepting only an identical prior write."""

    manager = PathManager(config)
    try:
        abs_path = manager.resolve(rel_path, must_exist=False, must_be_file=False)
    except PathSecurityError as exc:
        return error_result("path_not_allowed", str(exc))
    try:
        if dry_run:
            if abs_path.exists() and abs_path.read_text(encoding="utf-8") != content:
                return error_result("archive_shard_conflict", "immutable archive shard already has different content", path=rel_path)
            return rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(config.repo_root, abs_path):
            if abs_path.exists():
                if abs_path.read_text(encoding="utf-8") == content:
                    return rel_path
                return error_result("archive_shard_conflict", "immutable archive shard already has different content", path=rel_path)
            _atomic_write_text(abs_path, content, fsync_strict=config.mcp_fsync_strict)
            return rel_path
    except (OSError, UnicodeError) as exc:
        return error_result("archive_shard_write_failed", str(exc), path=rel_path)


def _archive_user_for_source(
    config: MemoryConfig,
    source_rel_path: str,
    entries: list[PackEntry],
) -> str | None:
    """解析归档分区用户，避免不同成员继续写同一个月度 pack。

    新格式的个人/共享 pack 已把用户 ID 编码在路径中，路径归属优先；
    旧格式 pack 没有用户目录时，读取本机 ``user_config.local.json`` 的
    企业微信 ID。仅在两者都不可用时，才使用记录里唯一且稳定的作者。
    """

    parts = Path(source_rel_path.replace("\\", "/")).parts
    path_user = ""
    if len(parts) >= 3 and parts[:2] == ("memory-bank", "people"):
        path_user = canonical_identity(parts[2])
    elif len(parts) >= 4 and parts[:3] == ("memory-bank", "shared", "packs"):
        path_user = canonical_identity(parts[3])
    if path_user and path_user != "unknown":
        return path_user

    configured_user = canonical_identity(get_current_user(config.repo_root))
    if configured_user and configured_user != "unknown":
        return configured_user

    authors = {
        canonical_identity(entry.metadata.get("author"))
        for entry in entries
        if canonical_identity(entry.metadata.get("author")) not in {"", "unknown"}
    }
    if len(authors) == 1:
        return next(iter(authors))
    return None


def _entry_ids_from_text(text: str) -> set[str]:
    ids: set[str] = set()
    try:
        for metadata, _body in parse_record_pack_entries(text):
            record_id = str(metadata.get("id") or "").strip()
            if record_id:
                ids.add(record_id)
    except ValueError:
        return set()
    return ids


def _append_entry_to_pack(
    config: MemoryConfig,
    *,
    rel_path_factory: Any,
    entry: PackEntry,
    max_chars: int,
    dry_run: bool,
    pack_states: dict[str, _PackState] | None = None,
) -> str | dict[str, Any]:
    manager = PathManager(config)
    payload = entry.packed
    states = pack_states if pack_states is not None else {}
    if len(payload) + len(PACK_HEADER) + 2 > max_chars:
        return error_result("record_too_large", f"record {entry.record_id} is larger than target pack size")

    for pack_index in range(1, 10000):
        rel_path = rel_path_factory(pack_index)
        try:
            abs_path = manager.resolve(rel_path, must_exist=False, must_be_file=False)
        except PathSecurityError as exc:
            return error_result("path_not_allowed", str(exc))
        if dry_run:
            state = states.get(rel_path)
            if state is None:
                try:
                    existing = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
                    state = _PackState(
                        ids=_entry_ids_from_text(existing),
                        char_count=len(existing),
                        ends_with_newline=existing.endswith("\n"),
                        size_bytes=abs_path.stat().st_size if abs_path.exists() else 0,
                    )
                    states[rel_path] = state
                except OSError as exc:
                    return error_result("pack_read_failed", str(exc), path=rel_path)
            if entry.record_id in state.ids:
                return rel_path
            separator_chars = len(PACK_HEADER) + 2 if state.char_count == 0 else (1 if state.ends_with_newline else 2)
            projected_chars = state.char_count + separator_chars + len(payload)
            if projected_chars > max_chars:
                continue
            state.ids.add(entry.record_id)
            state.char_count = projected_chars
            state.ends_with_newline = payload.endswith("\n")
            return rel_path
        try:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(config.repo_root, abs_path):
                on_disk_size = abs_path.stat().st_size if abs_path.exists() else 0
                state = states.get(rel_path)
                if state is None or state.size_bytes != on_disk_size:
                    existing = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
                    state = _PackState(
                        ids=_entry_ids_from_text(existing),
                        char_count=len(existing),
                        ends_with_newline=existing.endswith("\n"),
                        size_bytes=on_disk_size,
                    )
                    states[rel_path] = state
                if entry.record_id in state.ids:
                    return rel_path
                separator_chars = len(PACK_HEADER) + 2 if state.char_count == 0 else (1 if state.ends_with_newline else 2)
                projected_chars = state.char_count + separator_chars + len(payload)
                if projected_chars > max_chars:
                    continue
                if state.char_count:
                    with abs_path.open("a", encoding="utf-8", newline="\n") as handle:
                        if not state.ends_with_newline:
                            handle.write("\n")
                        handle.write("\n")
                        handle.write(payload)
                else:
                    abs_path.write_text(f"{PACK_HEADER}\n\n{payload}", encoding="utf-8", newline="\n")
                state.ids.add(entry.record_id)
                state.char_count = projected_chars
                state.ends_with_newline = payload.endswith("\n")
                state.size_bytes = abs_path.stat().st_size
                return rel_path
        except OSError as exc:
            return error_result("pack_write_failed", str(exc), path=rel_path)
    return error_result("pack_full", "no available pack slot")


def pack_existing_records(
    config: MemoryConfig,
    *,
    max_files: int | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Coalesce existing single-record Markdown files into date packs.

    This is a one-shot migration helper for historical ``mem_*.md`` files.
    It moves records that fit inside ``record_packing.max_pack_chars`` and
    skips pack files, compiled views, and non-record Markdown.
    """
    limit = max_files if max_files is not None else 500
    moved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    pack_states: dict[str, _PackState] = {}
    index_targets: set[str] = set()
    try:
        files = iter_record_files(config)
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))

    for abs_path, rel_path in files:
        if len(moved) >= limit:
            break
        if _is_pack_rel_path(rel_path) or rel_path.startswith("memory-bank/compiled/"):
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
            metadata, body = parse_record_markdown(text)
        except (OSError, ValueError):
            continue
        record_id = str(metadata.get("id") or "").strip()
        if not record_id or not metadata.get("record_kind"):
            continue
        entry = PackEntry(record_id=record_id, metadata=metadata, body=body)
        fallback_ts = abs_path.stat().st_mtime
        result = _append_entry_to_pack(
            config,
            rel_path_factory=lambda idx, source=rel_path, ent=entry, ts=fallback_ts: _pack_rel_path_for_entry(
                source, ent, ts, idx
            ),
            entry=entry,
            max_chars=config.record_packing_max_pack_chars,
            dry_run=dry_run,
            pack_states=pack_states,
        )
        if isinstance(result, dict):
            skipped.append({"path": rel_path, "id": record_id, "reason": result.get("error"), "message": result.get("message")})
            continue
        if not dry_run:
            try:
                with file_lock(config.repo_root, abs_path):
                    abs_path.unlink()
                index_targets.add(result)
            except OSError as exc:
                skipped.append({"path": rel_path, "id": record_id, "reason": "remove_source_failed", "message": str(exc)})
                continue
        moved.append({"id": record_id, "from": rel_path, "to": result})

    if not dry_run:
        # 目标包按记录 ID 更新索引即可替换旧路径；集中刷新避免反复解析持续增长的包。
        for target in sorted(index_targets):
            refresh_index_if_exists(config, target)
        append_event(config, "record_pack_migration", {"moved": moved, "skipped": skipped})
    return ok_result(
        "record pack migration completed" if not dry_run else "record pack migration planned",
        dry_run=dry_run,
        moved=len(moved),
        skipped=len(skipped),
        actions=moved,
        skipped_items=skipped,
    )


def _pack_date_from_path(path: Path) -> datetime | None:
    match = _DATE_PACK_RE.search(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("date"), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compact_old_record_packs(
    config: MemoryConfig,
    *,
    older_than_days: int | None = None,
    max_pack_chars: int | None = None,
    max_files: int | None = None,
    dry_run: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
    """Move old date packs into immutable per-user archive shards.

    The data remains available to normal record iteration because archive packs
    are still Markdown files under ``memory-bank``. Files are split at
    ``record_packing.archive_pack_max_chars`` (1 MiB by default). Each source
    pack is rendered into content-addressed shards, so no archive run appends
    to a monthly file and same-user devices cannot create a Git write conflict.
    """
    cutoff_days = older_than_days if older_than_days is not None else config.record_packing_archive_after_days
    archive_max = max_pack_chars if max_pack_chars is not None else config.record_packing_archive_pack_max_chars
    limit = max_files if max_files is not None else 200
    current_ts = now if now is not None else time.time()
    cutoff_ts = current_ts - (cutoff_days * 24 * 60 * 60)

    candidates: list[tuple[Path, str, datetime]] = []
    try:
        for abs_path, rel_path in iter_record_files(config):
            rel = rel_path.replace("\\", "/")
            if "/packs/" not in rel or _is_archive_pack_rel_path(rel):
                continue
            pack_date = _pack_date_from_path(abs_path)
            reference_ts = pack_date.timestamp() if pack_date is not None else abs_path.stat().st_mtime
            if reference_ts > cutoff_ts:
                continue
            bucket_date = pack_date or datetime.fromtimestamp(abs_path.stat().st_mtime, timezone.utc)
            candidates.append((abs_path, rel_path, bucket_date))
    except (PathSecurityError, FileNotFoundError) as exc:
        return error_result("path_error", str(exc))

    candidates.sort(key=lambda item: item[1])
    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    index_targets: set[str] = set()
    for abs_path, rel_path, bucket_date in candidates[:limit]:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
            entries = [
                PackEntry(record_id=str(metadata.get("id")), metadata=metadata, body=body)
                for metadata, body in parse_record_pack_entries(text)
                if metadata.get("id") and metadata.get("record_kind")
            ]
        except (OSError, ValueError) as exc:
            skipped.append({"path": rel_path, "reason": "parse_failed", "message": str(exc)})
            continue
        archive_user = _archive_user_for_source(config, rel_path, entries)
        if not archive_user:
            skipped.append(
                {
                    "path": rel_path,
                    "reason": "archive_user_unknown",
                    "message": (
                        "archive pack requires a stable user id from its source path, "
                        "MCP/Memory/user_config.local.json, or one unique record author"
                    ),
                }
            )
            continue
        bucket = bucket_date.strftime("%Y%m")
        source_sha = content_sha(text)
        fragments = _partition_archive_entries(entries, archive_max)
        if isinstance(fragments, dict):
            skipped.append({"path": rel_path, "reason": fragments.get("error"), "message": fragments.get("message")})
            continue
        written_to: set[str] = set()
        failed: dict[str, Any] | None = None
        for fragment_index, fragment in enumerate(fragments, start=1):
            result = _write_immutable_archive_shard(
                config,
                rel_path=_archive_rel_path(archive_user, bucket, source_sha, fragment_index),
                content=_render_archive_shard(fragment),
                dry_run=dry_run,
            )
            if isinstance(result, dict):
                failed = result
                break
            written_to.add(result)
        if failed is not None:
            skipped.append({"path": rel_path, "reason": failed.get("error"), "message": failed.get("message")})
            continue
        if not dry_run:
            try:
                with file_lock(config.repo_root, abs_path):
                    abs_path.unlink()
                index_targets.update(written_to)
            except OSError as exc:
                skipped.append({"path": rel_path, "reason": "remove_source_failed", "message": str(exc)})
                continue
        actions.append(
            {
                "from": rel_path,
                "to": sorted(written_to),
                "records": len(entries),
                "archive_user": archive_user,
            }
        )

    if not dry_run:
        # 每个归档目标只刷新一次，避免随归档包增长形成二次方级索引开销。
        for target in sorted(index_targets):
            refresh_index_if_exists(config, target)
        append_event(config, "record_pack_compaction", {"actions": actions, "skipped": skipped})
    return ok_result(
        "record pack compaction completed" if not dry_run else "record pack compaction planned",
        dry_run=dry_run,
        compacted=len(actions),
        skipped=len(skipped),
        actions=actions,
        skipped_items=skipped,
    )


def record_packing_stats(config: MemoryConfig) -> dict[str, Any]:
    active_pack_files = 0
    archive_pack_files = 0
    single_record_files = 0
    total_pack_bytes = 0
    try:
        files = iter_record_files(config)
    except (PathSecurityError, FileNotFoundError):
        files = []
    for abs_path, rel_path in files:
        rel = rel_path.replace("\\", "/")
        if _is_archive_pack_rel_path(rel):
            archive_pack_files += 1
            try:
                total_pack_bytes += abs_path.stat().st_size
            except OSError:
                pass
        elif "/packs/" in rel:
            active_pack_files += 1
            try:
                total_pack_bytes += abs_path.stat().st_size
            except OSError:
                pass
        else:
            try:
                metadata, _body = parse_record_markdown(abs_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                continue
            if metadata.get("id") and metadata.get("record_kind"):
                single_record_files += 1
    return {
        "active_pack_files": active_pack_files,
        "archive_pack_files": archive_pack_files,
        "single_record_files": single_record_files,
        "total_pack_bytes": total_pack_bytes,
    }
