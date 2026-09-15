"""冷归档归并必须保存正文/关系，旧引用可读且回滚不覆盖新写入。"""
import json
from dataclasses import replace
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_frontmatter import PACK_HEADER, render_record_markdown, render_record_pack_entry, canonical_record
from servers.memory_server.memory_git_merge import read_pack
from servers.memory_server.memory_pack_migration import plan_coalescence, apply_coalescence, _plan_path
from servers.memory_server.memory_reader import memory_get
from servers.memory_server.memory_record_index import memory_rebuild_index


def seed(repo, count=8):
    paths = []
    for index in range(count):
        metadata = {'schema_version': '2.0', 'id': f'mem-fixture-{index}', 'record_kind': 'incident',
                    'scope': 'personal', 'author': 'alice', 'status': 'raw',
                    'created_at': '2020-01-01T00:00:00Z', 'updated_at': '2020-01-01T00:00:00Z',
                    'tags': ['validation'], 'confidence': 0, 'immutable': False,
                    'supersedes': [f'prior-{index}'], 'source_refs': [], 'model': None,
                    'future_field': None, 'system_area': 'quoted "value": test'}
        text = PACK_HEADER + '\n\n' + render_record_pack_entry(metadata['id'], render_record_markdown(metadata, f'# Valve{index}\n\n阀门 {index} 的压力结论。'))
        path = repo / f'memory-bank/archive/record-packs/alice/202001/source-{index}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        paths.append(path)
    return paths


def test_coalesce_preserves_records_aliases_database_rebuild_and_exact_rollback(repo):
    config = load_config(repo)
    paths = seed(repo)
    originals = {path: path.read_bytes() for path in paths}
    plan = plan_coalescence(config)
    assert plan['ok'] and plan['source_files'] == 8 and plan['target_files'] == 1, plan
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    applied = apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)
    assert applied['ok'], applied
    assert all(not path.exists() for path in paths)
    assert apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)['ok']
    indexed = memory_rebuild_index(config)
    assert indexed['ok'] and indexed['indexed_records'] == 8, indexed
    for path, raw in originals.items():
        alias = memory_get(config, path.relative_to(repo).as_posix())
        assert alias['ok'] and alias['migrated'], alias
        original, restored = read_pack(raw.decode('utf-8')), read_pack(alias['content'])
        assert original.keys() == restored.keys()
        for key in original:
            assert canonical_record(original[key].metadata, original[key].body) == canonical_record(restored[key].metadata, restored[key].body)
    rollback = apply_coalescence(config, plan['plan_id'], rollback=True)
    assert rollback['ok'], rollback
    assert all(path.read_bytes() == raw for path, raw in originals.items())


def test_source_change_after_plan_is_not_deleted(repo):
    config = load_config(repo)
    paths = seed(repo)
    plan = plan_coalescence(config)
    paths[0].write_text(paths[0].read_text(encoding='utf-8') + '\nnew data\n', encoding='utf-8')
    result = apply_coalescence(config, plan['plan_id'], retire_legacy_paths=True)
    assert not result['ok'] and all(path.exists() for path in paths)
    assert 'new data' in paths[0].read_text(encoding='utf-8')


def test_tampered_plan_cannot_drop_source_record(repo):
    config = load_config(repo)
    seed(repo)
    planned = plan_coalescence(config)
    path = _plan_path(config, planned['plan_id'])
    plan = json.loads(path.read_text(encoding='utf-8'))
    first = next(iter(plan['sources'].values()))
    first['ids'] = []
    path.write_text(json.dumps(plan), encoding='utf-8')
    assert not apply_coalescence(config, planned['plan_id'], retire_legacy_paths=True)['ok']


def test_migration_stops_on_conflicting_same_id(repo):
    config = load_config(repo)
    paths = seed(repo, 1)
    other = paths[0].with_name('conflict.md')
    other.write_text(paths[0].read_text(encoding='utf-8').replace('压力结论', '另一结论'), encoding='utf-8')
    assert not plan_coalescence(config)['ok']
    assert paths[0].exists() and other.exists()


def test_rollback_rejects_changed_target_and_keeps_it(repo):
    config = load_config(repo)
    seed(repo)
    planned = plan_coalescence(config)
    assert apply_coalescence(config, planned['plan_id'], retire_legacy_paths=True)['ok']
    plan = json.loads(_plan_path(config, planned['plan_id']).read_text(encoding='utf-8'))
    target = repo / next(iter(plan['targets']))
    changed = target.read_text(encoding='utf-8') + '\nnew data\n'
    target.write_text(changed, encoding='utf-8')
    assert not apply_coalescence(config, planned['plan_id'], rollback=True)['ok']
    assert target.read_text(encoding='utf-8') == changed


def test_migration_default_preserves_every_legacy_path(repo):
    config = load_config(repo)
    paths = seed(repo)
    originals = {path: path.read_bytes() for path in paths}
    plan = plan_coalescence(config)
    blocked = apply_coalescence(config, plan['plan_id'])
    assert not blocked['ok'] and blocked['error'] == 'legacy_readers_not_confirmed'
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert not list((repo / 'memory-bank/archive/record-packs').glob('coalesced/**/*.md'))
