"""完整投影的增量/权限/关系/严格外部编辑回归。"""
import os
import sqlite3
from dataclasses import replace

from servers.memory_server.memory_config import load_config
from servers.memory_server import memory_record_index as idx
from servers.memory_server.memory_frontmatter import canonical_record, parse_record_pack_entries
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_retrieval import memory_retrieve_context


def write(config, text, **kwargs):
    result = memory_write_record(config, content_markdown=text, record_kind='incident', author='alice', **kwargs)
    assert result['ok'], result
    return result


def test_healthy_retrieval_uses_sqlite_payloads_not_markdown_parser(repo, monkeypatch):
    config = load_config(repo)
    source = write(config, '# CrystalValve 故障\n\nCrystalValve 修复依赖压力阈值。',
                   tags=['validation'], supersedes=['old-id'], confidence=0.9)
    assert idx.memory_rebuild_index(config)['ok']
    import servers.memory_server.memory_retrieval as retrieval
    monkeypatch.setattr(retrieval, '_iter_records', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected Markdown reread')))
    result = memory_retrieve_context(config, query='CrystalValve 压力阈值', user='alice', max_chars=12000)
    assert result['ok'], result
    assert source['id'] in {item['id'] for item in result['context_items']}
    assert result['stats']['scanned_files'] == 0
    with idx._connect(config) as conn:
        import json
        metadata = json.loads(conn.execute('SELECT metadata_json FROM memory_record_payloads WHERE id=?', (source['id'],)).fetchone()[0])
    assert metadata['supersedes'] == ['old-id'] and metadata['confidence'] == 0.9


def test_external_equal_size_preserved_mtime_change_updates_only_changed_source(repo, monkeypatch):
    config = load_config(repo)
    first = write(config, '# AlphaValve\n\nAlphaValve setting AAAA')
    other_config = replace(config, record_packing_layout='legacy')
    write(other_config, '# BetaValve\n\nUnchanged BetaValve setting', task_id='another-task')
    assert idx.memory_rebuild_index(config)['ok']
    path = repo / first['path']
    before = path.stat()
    path.write_text(path.read_text(encoding='utf-8').replace('AAAA', 'BBBB'), encoding='utf-8', newline='\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    monkeypatch.setattr(idx, 'memory_rebuild_index', lambda *a, **k: (_ for _ in ()).throw(AssertionError('unexpected full rebuild')))
    result = idx.ensure_index_fresh(config)
    assert result['ok'] and result['changed_sources'] == 1, result
    with idx._connect(config) as conn:
        assert 'BBBB' in conn.execute('SELECT body FROM memory_records WHERE id=?', (first['id'],)).fetchone()[0]


def test_known_write_does_not_scan_corpus(repo, monkeypatch):
    config = load_config(repo)
    write(config, '# Base\n\nBase record')
    assert idx.memory_rebuild_index(config)['ok']
    monkeypatch.setattr(idx, '_record_corpus_snapshot', lambda *a: (_ for _ in ()).throw(AssertionError('full scan during write')))
    result = write(config, '# New\n\nNew record')
    with idx._connect(config) as conn:
        assert conn.execute('SELECT count(*) FROM memory_records WHERE id=?', (result['id'],)).fetchone()[0] == 1


def test_duplicate_source_survives_canonical_path_deletion(repo):
    config = load_config(repo)
    written = write(config, '# PermanentFact\n\nPermanentFact must remain')
    source = repo / written['path']
    duplicate = repo / 'memory-bank/archive/record-packs/copy.md'
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(source.read_bytes())
    assert idx.memory_rebuild_index(config)['ok']
    with idx._connect(config) as conn:
        selected = conn.execute('SELECT path FROM memory_records WHERE id=?', (written['id'],)).fetchone()[0]
    (repo / selected).unlink()
    refreshed = idx.ensure_index_fresh(config)
    assert refreshed['ok'], refreshed
    with idx._connect(config) as conn:
        remaining = conn.execute('SELECT path FROM memory_records WHERE id=?', (written['id'],)).fetchone()[0]
    assert remaining != selected and (repo / remaining).exists()


def test_incremental_duplicate_conflict_rolls_back_valid_payload_and_fts(repo):
    config = load_config(repo)
    written = write(config, '# StableValve\n\nStableValve safe pressure AAAA')
    assert idx.memory_rebuild_index(config)['ok']
    duplicate = repo / 'memory-bank/archive/record-packs/conflict.md'
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text((repo / written['path']).read_text(encoding='utf-8').replace('AAAA', 'BBBB'), encoding='utf-8')
    result = idx.ensure_index_fresh(config)
    assert not result['ok'] and result['error'] == 'duplicate_record_id'
    with idx._connect(config) as conn:
        assert 'AAAA' in conn.execute('SELECT body FROM memory_records WHERE id=?', (written['id'],)).fetchone()[0]
        assert conn.execute('SELECT count(*) FROM memory_record_payloads WHERE path=?', (duplicate.relative_to(repo).as_posix(),)).fetchone()[0] == 0


def test_new_write_repairs_projection_after_legacy_writer_updated_traditional_index(repo):
    config = replace(load_config(repo), record_packing_layout='legacy')
    source = write(config, '# LegacyValve\n\nLegacyValve pressure ORIGINAL', task_id='legacy-task')
    assert idx.memory_rebuild_index(config)['ok']
    path = repo / source['path']
    path.write_text(path.read_text(encoding='utf-8').replace('ORIGINAL', 'CHANGED'), encoding='utf-8')
    # 模拟旧端：只认识传统记录表、源签名与 corpus 水位，保留未知的完整投影表。
    metadata, body = parse_record_pack_entries(path.read_text(encoding='utf-8'))[0]
    idx._index_record_rows(config, [(source['path'], metadata, body)])
    snapshot = idx._record_corpus_snapshot(config)
    with idx._connect(config) as conn:
        conn.execute('UPDATE memory_index_sources SET signature=? WHERE path=?', (snapshot[source['path']], source['path']))
        idx._set_meta(conn, 'corpus_watermark', idx._snapshot_watermark(snapshot))
    write(config, '# NewValve\n\nNewValve pressure after legacy update', task_id='new-task')
    found = memory_retrieve_context(config, query='LegacyValve pressure', user='alice')
    assert found['ok'], found
    with idx._connect(config) as conn:
        body = conn.execute('SELECT body FROM memory_record_payloads WHERE id=?', (source['id'],)).fetchone()[0]
    assert 'CHANGED' in body and 'ORIGINAL' not in body


def test_read_transaction_rejects_legacy_change_after_freshness_check(repo, monkeypatch):
    config = load_config(repo)
    write(config, '# StableValve\n\nStableValve current body')
    assert idx.memory_rebuild_index(config)['ok']
    original = idx.ensure_index_fresh
    def legacy_change_after_check(config):
        result = original(config)
        with idx._connect(config) as conn:
            idx._set_meta(conn, 'corpus_watermark', 'legacy-writer-new-watermark')
        return result
    monkeypatch.setattr(idx, 'ensure_index_fresh', legacy_change_after_check)
    found = memory_retrieve_context(config, query='StableValve', user='alice')
    assert not found['ok'] and found['error'] == 'index_projection_incomplete'


def test_incremental_write_rechecks_legacy_watermark_after_parsing_sources(repo, monkeypatch):
    from pathlib import Path
    config = load_config(repo)
    written = write(config, '# RaceValve\n\nRaceValve pressure')
    assert idx.memory_rebuild_index(config)['ok']
    original_read = Path.read_bytes
    original_rebuild = idx.memory_rebuild_index
    injected, repairs = [], []
    def legacy_commit(path):
        raw = original_read(path)
        if path == repo / written['path'] and not injected:
            injected.append(True)
            with idx._connect(config) as conn:
                idx._set_meta(conn, 'corpus_watermark', 'legacy-commit-during-parse')
        return raw
    def repair(config):
        repairs.append(True)
        return original_rebuild(config)
    monkeypatch.setattr(Path, 'read_bytes', legacy_commit)
    monkeypatch.setattr(idx, 'memory_rebuild_index', repair)
    result = idx.memory_update_index(config, paths=[written['path']])
    assert result['ok'] and injected and repairs, result
