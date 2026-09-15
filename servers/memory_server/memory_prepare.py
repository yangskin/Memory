"""部署时准备本地索引、周日志身份与 Git 记录合并。"""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .memory_config import MemoryConfig
from .memory_journal import get_replica_id
from .memory_locks import file_lock
from .memory_record_io import _atomic_write_text
from .memory_result import error_result, ok_result

ATTRIBUTE_RULES = (
    '/memory-bank/**/packs/**/*.md text merge=memory-records',
    '/memory-bank/archive/record-packs/**/*.md text merge=memory-records',
)


def install_merge_driver(repo_root: Path) -> dict:
    script = Path(__file__).resolve().parents[2] / 'scripts/merge_memory_records.py'
    try:
        git_root = subprocess.run(['git', '-C', str(repo_root), 'rev-parse', '--show-toplevel'],
                                  capture_output=True, text=True, encoding='utf-8', timeout=10)
        if git_root.returncode != 0:
            if (repo_root / '.git').exists():
                return error_result('git_config_failed', 'Git checkout is present but unreadable')
            return ok_result('standalone deployment without Git', installed=False)
        check = subprocess.run([sys.executable, str(script), '--self-test'], capture_output=True, text=True, timeout=10)
        if check.returncode:
            return error_result('git_merge_selftest_failed', 'configured Python could not run the Memory merge driver')
        top = Path(git_root.stdout.strip()).resolve()
        # 只为实际项目根的 memory-bank 注册规则，不误匹配另一个子项目。
        prefix = repo_root.resolve().relative_to(top).as_posix()
        prefix = '' if prefix == '.' else '/' + prefix
        rules = [prefix + rule for rule in ATTRIBUTE_RULES]
        command = shlex.join([Path(sys.executable).as_posix(), script.as_posix()]) + ' "%O" "%A" "%B"'
        for key, value in [('merge.memory-records.name', 'Memory record merge'),
                           ('merge.memory-records.driver', command),
                           ('merge.memory-records.recursive', 'binary')]:
            result = subprocess.run(['git', '-C', str(repo_root), 'config', '--local', key, value],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode:
                return error_result('git_config_failed', f'could not set {key}')
        attributes = top / '.gitattributes'
        with file_lock(repo_root, attributes):
            previous = attributes.read_text(encoding='utf-8') if attributes.exists() else ''
            missing = [rule for rule in rules if rule not in previous.splitlines()]
            if missing:
                _atomic_write_text(attributes, previous.rstrip() + '\n\n# Memory record-aware merge\n' + '\n'.join(missing) + '\n', fsync_strict=True)
        return ok_result('Git Memory merge driver registered', installed=True, attributes=str(attributes))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return error_result('git_config_failed', str(exc))


def prepare_memory(config: MemoryConfig, *, git_integration=True) -> dict:
    from .memory_record_index import ensure_index_fresh, _connect, _is_index_healthy
    try:
        get_replica_id(config.repo_root)
    except (OSError, ValueError, RuntimeError) as exc:
        return error_result('replica_identity_failed', str(exc))
    merge = install_merge_driver(config.repo_root) if git_integration else ok_result('Git integration not requested', installed=False)
    if not merge.get('ok'):
        return merge
    indexed = ensure_index_fresh(config)
    if not indexed.get('ok'):
        return indexed
    if not _is_index_healthy(config):
        return error_result('index_integrity_failed', 'prepared SQLite failed integrity check')
    with _connect(config) as conn:
        records = conn.execute('SELECT count(*) FROM memory_records').fetchone()[0]
        fts = conn.execute('SELECT count(*) FROM memory_records_fts').fetchone()[0]
        if records != fts:
            return error_result('index_projection_incomplete', 'FTS and record counts differ')
        conn.execute("SELECT id FROM memory_records_fts WHERE memory_records_fts MATCH ? LIMIT 1", ('"memory"',)).fetchall()
    return ok_result('Memory local storage ready', ready=True, indexed_records=records,
                     index=indexed, git_merge=merge)
