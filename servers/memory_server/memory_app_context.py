"""工作区可选集成：由 App 启动的既有 stdio MCP 发布自身当前连接。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import subprocess
import sys


async def refresh_app_context(repo_root):
    root = Path(repo_root).resolve()
    script = root / "Tools/HubTaskRunner/app_access.py"
    # 无宿主注入环境的 CLI/其它客户端不会拿旧缓存冒充新 App 入口。
    if not os.environ.get("CODEX_APP_TOOLS_PIPE_PATH") or not os.environ.get("CODEX_MCP_NODE_PATH"):
        return
    if not script.is_file() or script.is_symlink():
        return
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-X", "utf8", str(script), "--startup",
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        code = await asyncio.wait_for(process.wait(), 75)
        if code:
            logging.getLogger(__name__).warning('Local App connection refresh unavailable (exit %s); see local app-context-startup.json', code)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
