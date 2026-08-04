"""Memory Hub brief worker process entry point."""

from __future__ import annotations

import logging

from memory_hub.config import load_settings
from memory_hub.logging import configure_logging
from memory_hub.db.session import create_session_factory
from memory_hub.worker.runner import run_once


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    if not settings.database_url:
        logger.warning("memory-hub worker has no database configured")
        return
    with create_session_factory(settings.database_url)() as session:
        processed = run_once(session)
    logger.info("memory-hub worker processed jobs=%s provider=%s", processed, settings.llm_provider)