"""Memory Hub brief worker process entry point."""

from __future__ import annotations

import logging
import os
import time

from memory_hub.config import load_settings
from memory_hub.logging import configure_logging
from memory_hub.db.session import create_session_factory
from memory_hub.worker.runner import run_once


def _provider(settings):
    if settings.llm_provider == "openai_compatible":
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise RuntimeError("OpenAI-compatible provider requires LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL")
        from memory_hub.llm.openai_compatible import OpenAICompatibleBriefProvider

        return OpenAICompatibleBriefProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_timeout_seconds), settings.llm_model
    from memory_hub.llm.fake import FakeBriefProvider

    return FakeBriefProvider(), "fake"


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)
    if not settings.database_url:
        logger.warning("memory-hub worker has no database configured")
        return
    provider, model_name = _provider(settings)
    factory = create_session_factory(settings.database_url)
    worker_id = os.getenv("MEMORY_HUB_WORKER_ID", f"worker-{os.getpid()}")
    while True:
        with factory() as session:
            processed = run_once(session, provider, worker_id=worker_id, model_name=model_name, rebase_interval_seconds=settings.brief_rebase_interval_seconds)
        if processed:
            logger.info("memory-hub worker processed jobs=%s provider=%s", processed, settings.llm_provider)
        time.sleep(1)