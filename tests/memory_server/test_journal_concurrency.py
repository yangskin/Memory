"""真实多进程追加及两个独立克隆的同步回归。"""
import json
import os
import subprocess
import sys
from pathlib import Path

from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record
from servers.memory_server.memory_prepare import install_merge_driver
from servers.memory_server.memory_record_index import memory_rebuild_index
from servers.memory_server.memory_frontmatter import parse_record_pack_entries


def test_same_clone_processes_share_journal_without_losing_records(repo):
    root = Path(__file__).resolve().parents[2]
    script = '''import json,sys
from pathlib import Path
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_records import memory_write_record
config=load_config(Path(sys.argv[1]))
results=[memory_write_record(config,content_markdown='concurrent '+sys.argv[2]+' '+str(i),record_kind='note',author='alice',task_id='task-'+sys.argv[2]) for i in range(6)]
assert all(r['ok'] for r in results), results
print(json.dumps([r['id'] for r in results]))
'''
    processes = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(repo), str(i)],
                 cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(4)]
    ids = []
    for process in processes:
        out, err = process.communicate(timeout=90)
        assert process.returncode == 0, err
        ids.extend(json.loads(out))
    paths = list((repo / 'memory-bank').glob('**/journal/**/*.md'))
    assert len(paths) == 1
    records = parse_record_pack_entries(paths[0].read_text(encoding='utf-8'))
    assert len(ids) == len(set(ids)) == len(records) == 24
    indexed = memory_rebuild_index(load_config(repo))
    assert indexed['ok'] and indexed['indexed_records'] == 24


def test_two_clones_write_same_user_same_week_and_merge_all_ids(tmp_path):
    def git(root, *args):
        result = subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
            '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=', *args],capture_output=True,text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()
    left = tmp_path/'left'; left.mkdir()
    git(left, 'init', '-q', '-b', 'main')
    assert install_merge_driver(left)['ok']
    git(left, 'add', '.gitattributes'); git(left, 'commit', '-qm', 'rules')
    right = tmp_path/'right'; git(left, 'clone', '-q', str(left), str(right))
    assert install_merge_driver(right)['ok']
    written=[]
    for root in (left,right):
        config=load_config(root)
        item=memory_write_record(config,content_markdown='offline fact '+root.name,record_kind='note',author='alice')
        assert item['ok'], item
        written.append(item)
        git(root,'add','memory-bank');git(root,'commit','-qm','offline record')
    assert written[0]['path'] != written[1]['path']
    git(left,'fetch',str(right),'main');git(left,'merge','--no-edit','FETCH_HEAD')
    assert not git(left,'diff','--name-only','--diff-filter=U')
    index=memory_rebuild_index(load_config(left))
    assert index['ok'] and index['indexed_records']==2
    assert all((left/item['path']).exists() for item in written)
