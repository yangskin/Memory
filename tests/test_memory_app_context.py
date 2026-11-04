import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from servers.memory_server.memory_app_context import refresh_app_context


class AppContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_app_and_generic_workspace_do_not_spawn(self):
        with tempfile.TemporaryDirectory() as temp, patch('asyncio.create_subprocess_exec') as spawn:
            with patch.dict(os.environ, {}, clear=True):
                await refresh_app_context(temp)
            with patch.dict(os.environ, {"CODEX_APP_TOOLS_PIPE_PATH": "PRIVATE", "CODEX_MCP_NODE_PATH": "PRIVATE"}):
                await refresh_app_context(temp)
            spawn.assert_not_called()

    async def test_startup_is_no_model_child_and_does_not_pollute_stdio(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / 'Tools/HubTaskRunner/app_access.py'
            script.parent.mkdir(parents=True); script.touch()
            process = AsyncMock(); process.returncode = 0
            with patch.dict(os.environ, {"CODEX_APP_TOOLS_PIPE_PATH": "PRIVATE", "CODEX_MCP_NODE_PATH": "PRIVATE"}), \
                    patch('asyncio.create_subprocess_exec', new_callable=AsyncMock, return_value=process) as spawn:
                await refresh_app_context(temp)
            self.assertEqual(spawn.call_args.args[-2:], (str(script.resolve()), '--startup'))
            import subprocess
            self.assertEqual(spawn.call_args.kwargs['stdout'], subprocess.DEVNULL)
            process.wait.assert_awaited_once()
