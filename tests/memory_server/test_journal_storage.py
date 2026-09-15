"""周日志汇总、旧格式互读与可选字段保持测试。"""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_frontmatter import canonical_record, parse_record_pack_entries, render_record_markdown, sparse_record_metadata
from servers.memory_server.memory_journal import get_replica_id, journal_path
from servers.memory_server.memory_record_packing import compact_old_record_packs
from servers.memory_server.memory_records import memory_write_record


def test_many_tasks_share_weekly_log_and_preserve_task_identity(repo):
    config = load_config(repo)
    results = [memory_write_record(config, content_markdown=f"事实 {i}", record_kind="note",
                                   author="alice", task_id=f"task-{i}") for i in range(12)]
    assert all(result['ok'] for result in results), results
    assert len({result['path'] for result in results}) == 1
    entries = parse_record_pack_entries((repo / results[0]['path']).read_text(encoding='utf-8'))
    assert {meta['task_id'] for meta, _ in entries} == {f'task-{i}' for i in range(12)}
    assert all(len(meta['id'].rsplit('_', 1)[-1]) == 32 for meta, _ in entries)
    assert all('classifier_model' not in meta for meta, _ in entries)
    assert all(meta['author'] == 'alice' and meta['scope'] == 'personal' for meta, _ in entries)


def test_scope_and_author_do_not_share_a_log(repo):
    config = load_config(repo)
    results = [memory_write_record(config, content_markdown='事实', record_kind='note', author=author, scope=scope)
               for author, scope in [('alice', 'personal'), ('bob', 'personal'), ('alice', 'project_shared')]]
    assert all(r['ok'] for r in results)
    assert len({r['path'] for r in results}) == 3


def test_sparse_representation_keeps_unknown_false_zero_and_relationships():
    metadata = {'id': 'a', 'record_kind': 'note', 'scope': 'personal', 'author': 'alice',
                'confidence': 0, 'immutable': False, 'source_refs': [], 'model': None,
                'supersedes': ['old'], 'future_field': None}
    sparse = sparse_record_metadata(metadata)
    assert sparse['confidence'] == 0 and sparse['immutable'] is False
    assert sparse['future_field'] is None and sparse['supersedes'] == ['old']
    assert 'model' not in sparse and 'source_refs' not in sparse
    # 原有 parser 不需要文件级默认信息，即可读取新写入。
    old_meta, old_body = parse_record_pack_entries(render_record_markdown(metadata, '正文'))[0]
    new_meta, new_body = parse_record_pack_entries(render_record_markdown(metadata, '正文', sparse=True))[0]
    assert canonical_record(old_meta, old_body) == canonical_record(new_meta, new_body)


def test_week_uses_iso_year_and_distinct_replica_paths():
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)
    path = journal_path('memory-bank/shared/x.md', author='alice', scope='project_shared',
                        now=now, replica_id='a'*32, index=1)
    assert '/2026-W53/' in path
    other = journal_path('memory-bank/shared/x.md', author='alice', scope='project_shared',
                         now=now, replica_id='b'*32, index=1)
    assert other != path


def test_replica_id_persists_and_corrupt_id_is_not_silently_replaced(repo):
    first = get_replica_id(repo)
    assert get_replica_id(repo) == first
    (repo / '.ai-memory/memory-replica-id').write_text('broken', encoding='ascii')
    with pytest.raises(ValueError, match='invalid'):
        get_replica_id(repo)


def test_journal_not_relocated_by_legacy_daily_maintenance(repo):
    config = load_config(repo)
    result = memory_write_record(config, content_markdown='长期事实', record_kind='note', author='alice')
    path = repo / result['path']
    future = datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp()
    compact = compact_old_record_packs(config, older_than_days=1, dry_run=False, now=future)
    assert compact['ok'] and path.exists()


def test_clone_and_linked_worktree_identity(tmp_path, monkeypatch):
    root = tmp_path / 'main'
    root.mkdir()
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    git('init', '-q')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
        '-c', 'core.hooksPath=', 'commit', '--allow-empty', '-qm', 'base')
    linked = tmp_path / 'linked'
    git('worktree', 'add', '-q', '-b', 'fixture-linked', str(linked))
    clone = tmp_path / 'clone'
    git('clone', '-q', str(root), str(clone))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('replica discovery must not spawn Git'))
    assert get_replica_id(root) == get_replica_id(linked)
    assert get_replica_id(root) != get_replica_id(clone)


def test_standalone_replica_does_not_spawn_git(repo, monkeypatch):
    monkeypatch.delenv('GIT_DIR', raising=False)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('standalone writes must not spawn Git'))
    first = get_replica_id(repo)
    assert get_replica_id(repo) == first
    assert (repo / '.ai-memory/memory-replica-id').read_text(encoding='ascii').strip() == first
