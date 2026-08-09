"""
Generic Memory MCP Server (Phase 1) — powered by mcp SDK.

Exposes 5 default MCP tools:
    1. memory_read    — task context bootstrap, reads, search, recall
    2. memory_write   — structured memory records, observations, checkpoints
    3. memory_board_read  — dedicated Project Board queries
    4. memory_board_write — dedicated Project Board post/reply/resolve actions
    5. memory_task_sync — Graph Agent task lifecycle and Graph Bundle reads

Admin/sync/rebuild/diagnose/lineage/LLM-enhance flows are CLI/internal only.

Internal layout (P1-A split):
    - server_descriptions.py : SERVER_NAME / SERVER_VERSION / _BASE_DESCRIPTIONS
    - server_tools.py        : _build_file_roles / _build_facade_tools / _build_tools
    - server_dispatch.py     : _check_required / _dispatch_memory_* / _dispatch_tool
    - server.py (this file)  : create_server / _run / main + back-compat re-exports
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .memory_config import MemoryConfig, MemoryConfigError, ReloadableMemoryConfig, load_config
from .memory_response_budget import finalize_mcp_response
from .memory_worker import MemoryBackgroundWorker

# Back-compat re-exports for tests and external callers that still import
# from `servers.memory_server.server`. New code should import from the
# server_descriptions / server_tools / server_dispatch modules directly.
from .server_descriptions import SERVER_NAME, SERVER_VERSION, _BASE_DESCRIPTIONS  # noqa: F401
from .server_dispatch import (  # noqa: F401
    _check_required,
    _dispatch_memory_context,
    _dispatch_memory_read,
    _dispatch_memory_task_sync,
    _dispatch_memory_write,
    _dispatch_tool,
)
from .server_tools import (  # noqa: F401
    _build_facade_tools,
    _build_file_roles,
    _build_legacy_tools,
    _build_tools,
)

logger = logging.getLogger(__name__)


# ── Server setup ────────────────────────────────────────────────────────


def create_server(config: MemoryConfig | ReloadableMemoryConfig) -> Server:
    """Create and configure the MCP Server instance."""
    server = Server(SERVER_NAME)
    provider = config if isinstance(config, ReloadableMemoryConfig) else ReloadableMemoryConfig(config)
    tool_cache: tuple[str, list[Tool]] | None = None

    def current_tools() -> list[Tool]:
        nonlocal tool_cache
        current = provider.get()
        if tool_cache is None or tool_cache[0] != current.config_hash:
            tool_cache = (current.config_hash, _build_tools(current))
        return tool_cache[1]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return current_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        current = provider.get()
        result = _dispatch_tool(current, name, arguments or {})
        if isinstance(result, dict) and (arguments or {}).get("include_diagnostics"):
            result.setdefault("runtime_config", provider.diagnostics())
        result = finalize_mcp_response(result)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return [TextContent(type="text", text=text)]

    setattr(server, "memory_config_provider", provider)
    return server


# ── Entry point ─────────────────────────────────────────────────────────


async def _run(config: MemoryConfig | ReloadableMemoryConfig) -> None:
    provider = config if isinstance(config, ReloadableMemoryConfig) else ReloadableMemoryConfig(config)
    server = create_server(provider)
    worker = MemoryBackgroundWorker(provider.get)
    from .memory_sync_worker import MemorySyncWorker
    sync_worker = MemorySyncWorker(provider.get)
    worker.start()
    sync_worker.start()
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        worker.stop(timeout=1.0)
        sync_worker.stop(timeout=1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 1 memory MCP server")
    parser.add_argument("--root", default=os.getcwd(), help="Workspace root path")
    parser.add_argument("--config", default=None, help="Optional config path (default: .ai-memory/config.json)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    try:
        config = load_config(args.root, args.config)
    except (OSError, MemoryConfigError, TypeError, ValueError, OverflowError) as exc:
        logger.error("memory-mcp configuration is invalid: %s", exc)
        return 2
    provider = ReloadableMemoryConfig(config)
    # P0-3 (v0.6.0 OOTB): startup auto-maintenance. Best-effort, never
    # blocks the server boot. Disable via mcp.auto_maintenance.enabled=false.
    try:
        from .memory_auto_maintenance import run_if_due

        run_if_due(provider.get())
    except Exception as exc:  # pragma: no cover — must never block boot
        logger.warning("auto-maintenance skipped: %s", exc)
    try:
        asyncio.run(_run(provider))
    except KeyboardInterrupt:
        logger.info("memory-mcp stopped by user")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
