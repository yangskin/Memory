"""校验失败必须可诊断，迁移中断必须可恢复。"""
import json
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_prepare import prepare_memory
from servers.memory_server.memory_record_index import memory_rebuild_index, ensure_index_fresh
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_retrieval import memory_retrieve_context
from servers.memory_server.memory_pack_migration import plan_coalescence, apply_coalescence
from test_pack_migration import seed


def test_malformed_pack_refuses_prepare_and_recall(repo):
    path = repo / 'memory-bank/personal/packs/broken.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('<!-- memory-record-pack version=1 -->\n<!-- memory-record-pack-entry id=bad -->\n---\nid: bad\n', encoding='utf-8')
    config = load_config(repo)
    assert not prepare_memory(config, git_integration=False)['ok']
    assert not memory_retrieve_context(config, query='bad', user='alice')['ok']


def test_duplicate_conflict_cannot_return_plausible_recall(repo):
    config = load_config(repo)
    record = memory_write_record(config, content_markdown='# Valve\n\nValve AAAA', record_kind='incident', author='alice')
    assert record['ok'] and memory_rebuild_index(config)['ok']
    copy = repo / 'memory-bank/archive/record-packs/conflict.md'
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text((repo / record['path']).read_text(encoding='utf-8').replace('AAAA', 'BBBB'), encoding='utf-8')
    found = memory_retrieve_context(config, query='Valve', user='alice')
    assert not found['ok'] and found['error'] == 'duplicate_record_id'


def test_discovery_read_failure_does_not_delete_indexed_record(repo, monkeypatch):
    config = load_config(repo)
    written = memory_write_record(config, content_markdown='# Stable\n\nStable', record_kind='incident', author='alice')
    assert memory_rebuild_index(config)['ok']
    original = Path.read_bytes
    def reject(path):
        if path == repo / written['path']:
            raise PermissionError('injected read lock')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', reject)
    result = ensure_index_fresh(config)
    assert not result['ok'] and result['error'] == 'index_check_failed'


def test_migration_interrupted_after_first_removal_resumes_without_loss(repo, monkeypatch):
    paths = seed(repo)
    config = load_config(repo)
    originals = {path: path.read_bytes() for path in paths}
    plan = plan_coalescence(config)
    original = Path.unlink
    removed = []
    def interrupt(path, *args, **kwargs):
        if path in paths:
            if removed:
                raise OSError('injected crash after one source removal')
            removed.append(path)
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', interrupt)
        assert not apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)['ok']
    assert len(removed) == 1
    assert apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)['ok']
    result = memory_rebuild_index(config)
    assert result['ok'] and result['indexed_records'] == len(paths)
    assert apply_coalescence(config, plan['plan_id'], rollback=True)['ok']
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_cli_bad_migration_request_is_structured_failure(repo, monkeypatch, capsys):
    from servers.memory_server import cli
    code = cli.main(['--root', str(repo), 'coalesce-archives', '--rollback'])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and not result['ok']


def test_atomic_write_temp_name_does_not_expand_long_target(tmp_path, monkeypatch):
    import os
    from servers.memory_server.memory_record_io import _atomic_write_text
    target = tmp_path / ('a' * 140 + '.md')
    original = os.open
    observed = []
    def observe(path, flags, *args, **kwargs):
        if str(path).endswith('.tmp'):
            observed.append(str(path))
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', observe)
    _atomic_write_text(target, '正文', fsync_strict=True)
    assert target.read_text(encoding='utf-8') == '正文'
    assert observed and len(observed[0]) < len(str(target))
