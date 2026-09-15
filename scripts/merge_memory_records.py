"""Git merge driver: ancestor / current / other；只成功时替换 current。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from servers.memory_server.memory_git_merge import merge_packs


def main():
    if sys.argv[1:] == ['--self-test']:
        from servers.memory_server.memory_frontmatter import PACK_HEADER, render_record_markdown, render_record_pack_entry
        from servers.memory_server.memory_git_merge import read_pack
        def fixture(record_id):
            metadata = dict(schema_version='2.0', id=record_id, record_kind='note', author='fixture', scope='personal')
            return PACK_HEADER + '\n\n' + render_record_pack_entry(record_id, render_record_markdown(metadata, 'fixture'))
        try:
            merged = merge_packs('', fixture('left'), fixture('right'))
            if set(read_pack(merged)) != {'left', 'right'}:
                raise ValueError('merge self-test lost records')
        except ValueError as exc:
            print(f'Memory merge self-test failed: {exc}', file=sys.stderr)
            return 1
        print('Memory merge self-test passed')
        return 0
    if len(sys.argv) != 4:
        print('Memory merge expects ancestor/current/other files', file=sys.stderr)
        return 2
    ancestor, current, other = map(Path, sys.argv[1:])
    try:
        merged = merge_packs(ancestor.read_text(encoding='utf-8'), current.read_text(encoding='utf-8'), other.read_text(encoding='utf-8'))
        current.write_text(merged, encoding='utf-8', newline='\n')
    except (OSError, ValueError) as exc:
        print(f'Memory merge conflict: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
