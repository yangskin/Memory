"""记录级合并的完整性及真实 Git add/add 集成验收。"""
import subprocess
from pathlib import Path

import pytest

from servers.memory_server.memory_frontmatter import PACK_HEADER, render_record_markdown, render_record_pack_entry
from servers.memory_server.memory_git_merge import merge_packs, read_pack, render_pack
from servers.memory_server.memory_prepare import install_merge_driver, prepare_memory
from servers.memory_server.memory_config import load_config


def pack(*ids, sparse=False):
    return PACK_HEADER + '\n\n' + '\n'.join(render_record_pack_entry(
        record_id, render_record_markdown({'schema_version': '2.0', 'id': record_id,
                    'record_kind': 'note', 'author': 'alice', 'scope': 'personal', 'status': 'raw',
                    'created_at': '2026-09-15T00:00:00Z', 'model': None}, f'# {record_id}\n正文', sparse=sparse))
                    for record_id in ids)


def test_merge_is_commutative_idempotent_and_accepts_old_sparse_equivalence():
    base, a, b = pack('base'), pack('base', 'a'), pack('base', 'b', sparse=True)
    merged = merge_packs(base, a, b)
    assert merged == merge_packs(base, b, a)
    assert merged == merge_packs(base, merged, merged)
    assert set(read_pack(merged)) == {'base', 'a', 'b'}
    assert set(read_pack(merge_packs('', pack('a'), pack('b')))) == {'a', 'b'}


@pytest.mark.parametrize('bad', [
    pack('base').replace('正文', 'other'),
    pack('base').replace('id: base', 'id: mismatch'),
    pack('base').replace('/memory-record-pack-entry', '/broken'),
    pack('base') + 'unframed text',
    pack('base').replace('schema_version: "2.0"', 'schema_version: "9.0"'),
])
def test_conflict_or_corruption_is_never_silently_union_merged(bad):
    with pytest.raises(ValueError):
        merge_packs(pack('base'), pack('base', 'left'), bad)


def test_record_deletion_requires_explicit_resolution():
    with pytest.raises(ValueError, match='removal'):
        merge_packs(pack('base', 'removed'), pack('base'), pack('base', 'removed', 'new'))


def test_legacy_writer_audit_is_validated_and_preserved_by_render_and_merge():
    audit = '<!-- last overwritten by alice at 2026-10-11 05:01 UTC -->'
    original = pack('base') + '\n' + audit + '\n'
    parsed = read_pack(original.replace('\n', '\r\n'))
    assert set(parsed) == {'base'} and parsed.audit_comments == (audit,)
    assert audit in render_pack(parsed, compact=True)
    merged = merge_packs(original, original + pack('a').split('\n\n', 1)[1], pack('base', 'b'))
    assert set(read_pack(merged)) == {'base', 'a', 'b'}
    assert merged.count(audit) == 1
    assert merged == merge_packs(original, pack('base', 'b'), original + pack('a').split('\n\n', 1)[1])
    assert merged == merge_packs(original, merged, merged)
    repeated = original + audit + '\n'
    assert merge_packs(repeated, repeated, repeated).count(audit) == 2


@pytest.mark.parametrize('tail', [
    '<!-- unrelated comment -->',
    '<!-- last overwritten by alice at 2026-99-11 05:01 UTC -->',
    '<!-- last overwritten by alice at yesterday -->',
    '<!-- last overwritten by alice at 2026-10-11 05:01 UTC -->private content',
    '<!-- last overwritten by alice at 2026-10-11 05:01 UTC',
])
def test_unknown_or_malformed_audit_never_masks_corruption(tail):
    with pytest.raises(ValueError):
        read_pack(pack('base') + tail + '\n')


def test_audit_does_not_make_an_empty_or_truncated_pack_valid():
    audit = '<!-- last overwritten by alice at 2026-10-11 05:01 UTC -->\n'
    for text in (PACK_HEADER + '\n' + audit, pack('base').replace('/memory-record-pack-entry', '/broken') + audit):
        with pytest.raises(ValueError):
            read_pack(text)


def test_real_git_merges_same_path_add_add_after_local_install(tmp_path):
    def git(*args):
        result = subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
            '-c', 'core.hooksPath=', *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout
    git('init', '-q', '-b', 'main')
    installed = install_merge_driver(tmp_path)
    assert installed['ok'] and installed['installed'], installed
    git('add', '.gitattributes')
    git('commit', '-qm', 'merge rules')
    git('checkout', '-qb', 'left')
    target = tmp_path / 'memory-bank/people/alice/packs/journal/week.md'
    target.parent.mkdir(parents=True)
    target.write_text(pack('left'), encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'left record')
    git('checkout', '-qb', 'right', 'main')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pack('right'), encoding='utf-8')
    git('add', '.')
    git('commit', '-qm', 'right record')
    git('merge', '--no-edit', 'left')
    assert set(read_pack(target.read_text(encoding='utf-8'))) == {'left', 'right'}
    assert not git('diff', '--name-only', '--diff-filter=U').strip()
    attributes = (tmp_path / '.gitattributes').read_bytes()
    assert install_merge_driver(tmp_path)['ok']
    assert (tmp_path / '.gitattributes').read_bytes() == attributes


def test_prepare_rebuilds_database_and_is_repeatable_without_llm(repo):
    config = load_config(repo)
    first = prepare_memory(config, git_integration=False)
    second = prepare_memory(config, git_integration=False)
    assert first['ok'] and second['ok'] and first['ready'] and second['ready']
    assert first['indexed_records'] == second['indexed_records']
    assert (repo / '.ai-memory/search.db').is_file()
