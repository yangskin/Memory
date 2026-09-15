"""副本目录解析不依赖 Git 可执行程序，损坏元数据不能改变身份。"""
import subprocess
from pathlib import Path

import pytest

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_journal import get_replica_id, replica_home
from servers.memory_server.memory_records import memory_write_record


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch):
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_CEILING_DIRECTORIES', 'GIT_DISCOVERY_ACROSS_FILESYSTEM'):
        monkeypatch.delenv(key, raising=False)


def init_git(root, *args):
    subprocess.run(['git', '-C', str(root), 'init', '-q', *args], check=True, capture_output=True, timeout=15)


def forbid_processes(monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('journal writes must not spawn subprocesses'))


def test_git_write_and_nested_project_without_git_process(repo, monkeypatch):
    init_git(repo)
    expected = 'a' * 32
    (repo / '.git/memory-replica-id').write_text(expected, encoding='ascii')
    nested = repo / 'nested/project'
    nested.mkdir(parents=True)
    forbid_processes(monkeypatch)
    result = memory_write_record(load_config(repo), content_markdown='无 Git 子进程写入', record_kind='note', author='alice')
    assert result['ok'], result
    assert Path(result['path']).name == expected + '-001.md'
    assert get_replica_id(nested) == expected
    assert not (repo / '.ai-memory/memory-replica-id').exists()


@pytest.mark.parametrize('relative', [False, True])
def test_separate_git_directory(repo, tmp_path, monkeypatch, relative):
    metadata = tmp_path / 'separate metadata'
    init_git(repo, '--separate-git-dir', str(metadata))
    if relative:
        # Windows 的 Git 会给 .git 指针设置隐藏属性；r+ 可原位修改此测试文件。
        with (repo / '.git').open('r+', encoding='utf-8') as stream:
            stream.write('gitdir: separate metadata\n')
            stream.truncate()
    forbid_processes(monkeypatch)
    assert replica_home(repo) == metadata.resolve()


def test_explicit_git_and_common_directory(repo, monkeypatch):
    init_git(repo)
    external = repo / 'external'
    external.mkdir()
    init_git(external)
    forbid_processes(monkeypatch)
    monkeypatch.setenv('GIT_DIR', 'external/.git')
    assert replica_home(repo) == external / '.git'
    (external / '.git/commondir').write_text('missing', encoding='utf-8')
    monkeypatch.setenv('GIT_COMMON_DIR', '.git')
    assert replica_home(repo) == repo / '.git'


@pytest.mark.parametrize('content', ['broken', 'gitdir: ', 'gitdir: missing', 'gitdir: missing\nextra', 'x' * 65537],
                         ids=['prefix', 'empty', 'missing', 'multiline', 'oversized'])
def test_invalid_gitfile_fails_without_alternate_identity(repo, monkeypatch, content):
    (repo / '.git').write_text(content, encoding='utf-8')
    forbid_processes(monkeypatch)
    result = memory_write_record(load_config(repo), content_markdown='不可丢失', record_kind='note', author='alice')
    assert not result['ok'] and result['error'] == 'replica_identity_failed', result
    assert not (repo / '.ai-memory/memory-replica-id').exists()
    assert not list((repo / 'memory-bank/archive/record-packs/journal').rglob('*.md'))


@pytest.mark.parametrize('content', ['', 'missing', 'missing\nextra'])
def test_invalid_commondir_fails_without_creating_target(repo, monkeypatch, content):
    init_git(repo)
    (repo / '.git/commondir').write_text(content, encoding='utf-8')
    forbid_processes(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        get_replica_id(repo)
    assert not (repo / '.git/missing').exists()
    assert not (repo / '.ai-memory/memory-replica-id').exists()


@pytest.mark.parametrize('key,value', [('GIT_DIR', ''), ('GIT_DIR', 'missing'), ('GIT_COMMON_DIR', 'missing')])
def test_invalid_explicit_directory_does_not_use_standalone_identity(repo, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    forbid_processes(monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        get_replica_id(repo)
    assert not (repo / '.ai-memory/memory-replica-id').exists()


def test_ceiling_stops_ancestor_discovery_but_not_current_directory(repo, monkeypatch):
    init_git(repo)
    child = repo / 'child'
    child.mkdir()
    monkeypatch.setenv('GIT_CEILING_DIRECTORIES', str(repo))
    forbid_processes(monkeypatch)
    assert replica_home(child) == child / '.ai-memory'
    assert replica_home(repo) == repo / '.git'
