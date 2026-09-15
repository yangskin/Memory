"""显式冷归档归并；保留本地原件、版本化路径映射及可恢复计划。"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .memory_config import MemoryConfig
from .memory_frontmatter import canonical_record, PACK_HEADER
from .memory_git_merge import read_pack, render_pack
from .memory_locks import file_lock
from .memory_paths import PathManager, PathSecurityError
from .memory_record_io import _atomic_write_text
from .memory_result import error_result, ok_result

MANIFEST_ROOT = 'memory-bank/archive/pack-migrations'


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _logical(records) -> str:
    return _sha(json.dumps({key: canonical_record(item.metadata, item.body)
                           for key, item in sorted(records.items())}, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def _plan_path(config, plan_id):
    if not re.fullmatch(r'[0-9a-f]{32}', plan_id):
        raise ValueError('invalid migration plan ID')
    return config.repo_root / '.ai-memory/pack-migrations' / plan_id / 'plan.json'


def plan_coalescence(config: MemoryConfig, *, older_than_days=7, max_bytes=524288) -> dict:
    if older_than_days < 1 or max_bytes < 1024 or max_bytes > config.max_file_size_bytes:
        return error_result('invalid_input', 'invalid age or pack byte limit')
    manager = PathManager(config)
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    sources, all_records, groups = {}, {}, defaultdict(dict)
    try:
        for absolute, relative in manager.iter_files(scopes=['memory-bank'], include_paths=['memory-bank/archive/record-packs/**/*.md']):
            if '/coalesced/' in relative or '/journal/' in relative:
                continue
            raw = absolute.read_bytes()
            records = read_pack(raw.decode('utf-8'))
            dates = {key: datetime.fromisoformat(str(item.metadata['created_at']).replace('Z', '+00:00'))
                     for key, item in records.items()}
            if any(value.tzinfo is None for value in dates.values()):
                raise ValueError(f'record timestamp lacks timezone: {relative}')
            if any(value > cutoff for value in dates.values()):
                continue
            sources[relative] = {'sha256': _sha(raw), 'ids': list(records), 'bytes': len(raw)}
            for key, item in records.items():
                if key in all_records and canonical_record(item.metadata, item.body) != canonical_record(all_records[key].metadata, all_records[key].body):
                    raise ValueError(f'conflicting duplicate record ID: {key}')
                all_records[key] = item
                author, scope = str(item.metadata.get('author') or ''), str(item.metadata.get('scope') or '')
                if not author or not re.fullmatch(r'[a-z_]+', scope):
                    raise ValueError(f'record author/scope is missing: {key}')
                groups[(author, scope, dates[key].astimezone(timezone.utc).strftime('%Y%m'))][key] = item
        targets = {}
        for (author, scope, month), records in sorted(groups.items()):
            current = {}
            header_bytes = len((PACK_HEADER + "\n").encode("utf-8"))
            current_bytes = header_bytes
            def flush():
                if not current:
                    return
                content = render_pack(current, compact=True)
                parsed = read_pack(content)
                if _logical(parsed) != _logical(current):
                    raise ValueError('sparse serialization changed record semantics')
                digest = _sha(content.encode('utf-8'))
                path = f'memory-bank/archive/record-packs/coalesced/{_sha(author.encode())[:16]}/{scope}/{month}/{digest}.md'
                targets[path] = {'content': content, 'sha256': digest, 'ids': sorted(current)}
            for key, item in sorted(records.items()):
                entry_bytes = len(render_pack({key: item}, compact=True).encode('utf-8')) - header_bytes
                if header_bytes + entry_bytes > max_bytes:
                    raise ValueError(f'record exceeds migration pack limit: {key}')
                if current_bytes + entry_bytes > max_bytes:
                    flush()
                    current = {}
                    current_bytes = header_bytes
                current[key] = item
                current_bytes += entry_bytes
            flush()
        digest = _sha(json.dumps({'sources': sources, 'max_bytes': max_bytes}, sort_keys=True).encode())[:32]
        plan = {'version': 1, 'plan_id': digest, 'sources': sources, 'targets': targets,
                'logical_sha256': _logical(all_records), 'record_count': len(all_records)}
        target = _plan_path(config, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(config.repo_root, target):
            if target.exists() and json.loads(target.read_text(encoding='utf-8')) != plan:
                raise ValueError('existing plan does not match source snapshot')
            _atomic_write_text(target, json.dumps(plan, ensure_ascii=False), fsync_strict=True)
        return ok_result('archive coalescence planned', plan_id=digest, dry_run=True,
                         source_files=len(sources), target_files=len(targets), records=len(all_records),
                         before_bytes=sum(item['bytes'] for item in sources.values()),
                         after_bytes=sum(len(item['content'].encode('utf-8')) for item in targets.values()))
    except (OSError, ValueError, KeyError, PathSecurityError) as exc:
        return error_result('migration_plan_failed', str(exc))


def apply_coalescence(config: MemoryConfig, plan_id: str, *, rollback=False, retire_legacy_paths=False) -> dict:
    # 旧客户端可以展开新 pack，但无法理解旧路径映射。不能由本机版本推断所有读端已升级。
    if not rollback and not retire_legacy_paths:
        return error_result('legacy_readers_not_confirmed',
            'original paths retained: retiring legacy paths requires explicit confirmation that all readers support migration manifests',
            plan_id=plan_id, required_option='--retire-legacy-paths')
    manager = PathManager(config)
    try:
        plan_path = _plan_path(config, plan_id)
        with file_lock(config.repo_root, plan_path):
            plan = json.loads(plan_path.read_text(encoding='utf-8'))
            if plan.get('version') != 1 or plan.get('plan_id') != plan_id:
                raise ValueError('invalid migration plan version or identity')
            sources, targets = plan['sources'], plan['targets']
            for relative in sources:
                if not relative.startswith('memory-bank/archive/record-packs/') or '/coalesced/' in relative or '/journal/' in relative:
                    raise ValueError('migration source is outside legacy archive packs')
                checked = manager.resolve(relative, must_exist=False, must_be_file=False)
                if manager.to_repo_relative(checked) != relative:
                    raise ValueError('migration source path must be canonical')
            merged = {}
            for relative, item in targets.items():
                if not relative.startswith('memory-bank/archive/record-packs/coalesced/'):
                    raise ValueError('migration target is outside coalesced archive packs')
                checked = manager.resolve(relative, must_exist=False, must_be_file=False)
                if manager.to_repo_relative(checked) != relative:
                    raise ValueError('migration target path must be canonical')
                if _sha(item['content'].encode('utf-8')) != item['sha256']:
                    raise ValueError('migration target checksum mismatch')
                parsed = read_pack(item['content'])
                if set(parsed) != set(item['ids']):
                    raise ValueError('migration target IDs do not match plan')
                for key, record in parsed.items():
                    if key in merged:
                        raise ValueError('record duplicated across migration targets')
                    merged[key] = record
            if (_logical(merged) != plan['logical_sha256'] or len(merged) != plan['record_count'] or
                set(merged) != {key for source in sources.values() for key in source['ids']}):
                raise ValueError('migration logical record verification failed')
            # 内容寻址备份避免叠加完整原路径，支持深目录的 Windows 部署。
            backup_root = config.repo_root / '.ai-memory/pack-migration-originals'
            # 所有源先预检；修改后的源不得被计划覆盖或删除。
            for relative, source in sources.items():
                target = config.repo_root / relative
                backup = backup_root / (source["sha256"] + ".bak.md")
                if target.exists() and _sha(target.read_bytes()) != source['sha256']:
                    raise ValueError(f'source changed since migration plan: {relative}')
                if not target.exists() and (not backup.exists() or _sha(backup.read_bytes()) != source['sha256']):
                    raise ValueError(f'source and verified backup are missing: {relative}')
                original = read_pack((target if target.exists() else backup).read_text(encoding='utf-8'))
                if set(original) != set(source['ids']) or any(
                    key not in merged or canonical_record(item.metadata, item.body) != canonical_record(merged[key].metadata, merged[key].body)
                    for key, item in original.items()
                ):
                    raise ValueError(f'plan does not preserve source records: {relative}')
            if rollback:
                for relative, item in targets.items():
                    target = config.repo_root / relative
                    if target.exists() and _sha(target.read_bytes()) != item['sha256']:
                        raise ValueError(f'target changed after migration: {relative}')
                for relative, source in sources.items():
                    target = config.repo_root / relative
                    backup = backup_root / (source["sha256"] + ".bak.md")
                    with file_lock(config.repo_root, target):
                        if target.exists() and _sha(target.read_bytes()) != source['sha256']:
                            raise ValueError(f'source changed during rollback: {relative}')
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            from .memory_encoding import _atomic_write_bytes
                            raw = backup.read_bytes()
                            if _sha(raw) != source['sha256']:
                                raise ValueError('rollback backup checksum mismatch')
                            _atomic_write_bytes(target, raw, fsync_strict=True)
                for relative, item in targets.items():
                    target = config.repo_root / relative
                    with file_lock(config.repo_root, target):
                        if target.exists():
                            if _sha(target.read_bytes()) != item['sha256']:
                                raise ValueError(f'target changed during rollback: {relative}')
                            target.unlink()
                (config.repo_root / MANIFEST_ROOT / f'{plan_id}.json').unlink(missing_ok=True)
            else:
                for relative, item in targets.items():
                    target = config.repo_root / relative
                    with file_lock(config.repo_root, target):
                        if target.exists() and _sha(target.read_bytes()) != item['sha256']:
                            raise ValueError(f'migration target already differs: {relative}')
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            _atomic_write_text(target, item['content'], fsync_strict=True)
                        if _sha(target.read_bytes()) != item['sha256']:
                            raise ValueError('target readback failed')
                # 映射随项目 Git 同步，不包含重复正文；先发布映射再逐源移除，支持断点恢复。
                manifest = {**plan, 'targets': {path: {k: v for k, v in item.items() if k != 'content'} for path, item in targets.items()}}
                manifest_path = config.repo_root / MANIFEST_ROOT / f'{plan_id}.json'
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False), fsync_strict=True)
                for relative, source in sources.items():
                    target = config.repo_root / relative
                    backup = backup_root / (source["sha256"] + ".bak.md")
                    with file_lock(config.repo_root, target):
                        if not target.exists():
                            continue
                        raw = target.read_bytes()
                        if _sha(raw) != source['sha256']:
                            raise ValueError(f'source changed during migration: {relative}')
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        from .memory_encoding import _atomic_write_bytes
                        _atomic_write_bytes(backup, raw, fsync_strict=True)
                        if _sha(backup.read_bytes()) != source['sha256']:
                            raise ValueError('source backup verification failed')
                        target.unlink()
            from .memory_record_index import mark_index_dirty
            mark_index_dirty(config, reason='archive pack migration', paths=list(sources) + list(targets))
            return ok_result('archive migration rolled back' if rollback else 'archive migration applied',
                             plan_id=plan_id, records=plan['record_count'],
                             source_files=len(sources), target_files=len(targets), rollback=rollback)
    except (OSError, ValueError, KeyError, PathSecurityError) as exc:
        return error_result('migration_failed', str(exc), plan_id=plan_id)


def read_migrated_path(config: MemoryConfig, relative: str) -> tuple[str, list[str]] | None:
    """旧文件引用按 ID 解析到新包；数据库删除或新 clone 后也可重建。"""
    if not relative.startswith('memory-bank/archive/record-packs/'):
        return None
    manager = PathManager(config)
    root = config.repo_root / MANIFEST_ROOT
    if not root.exists():
        return None
    for path in sorted(root.glob('*.json')):
        manifest_path = manager.resolve(path.relative_to(config.repo_root).as_posix(), must_exist=True, must_be_file=True)
        data = json.loads(manifest_path.read_text(encoding='utf-8'))
        if data.get('version') != 1:
            raise ValueError('unsupported migration manifest version')
        source = data.get('sources', {}).get(relative)
        if not source:
            continue
        wanted = set(source['ids'])
        found, targets = {}, []
        for target, item in data['targets'].items():
            if not wanted.intersection(item['ids']):
                continue
            absolute = manager.resolve(target, must_exist=True, must_be_file=True)
            parsed = read_pack(absolute.read_text(encoding='utf-8'))
            found.update({key: record for key, record in parsed.items() if key in wanted})
            targets.append(target)
        if set(found) != wanted:
            raise ValueError('migrated source IDs are missing from targets')
        return render_pack(found), targets
    return None
