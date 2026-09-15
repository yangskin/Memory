"""部署入口；不调用 LLM、不改原始记忆。"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from servers.memory_server.memory_config import load_config
from servers.memory_server.memory_prepare import prepare_memory

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--no-git-integration', action='store_true')
    args = parser.parse_args()
    try:
        result = prepare_memory(load_config(Path(args.root)), git_integration=not args.no_git_integration)
    except Exception as exc:
        result = {'ok': False, 'error': 'prepare_failed', 'message': str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result.get('ok') else 1)
