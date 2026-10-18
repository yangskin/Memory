"""Memory Hub brief worker process entry point."""

from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

from memory_hub.config import load_settings
from memory_hub.logging import configure_logging
from memory_hub.db.repositories import purge_resolved_board_posts, purge_transient_board_notes
from memory_hub.db.session import create_session_factory
from memory_hub.worker.runner import run_once


def _provider(settings):
    if settings.llm_provider == "openai_compatible":
        if not (settings.llm_base_url and settings.llm_api_key and settings.llm_model):
            raise RuntimeError("OpenAI-compatible provider requires LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL")
        from memory_hub.llm.openai_compatible import OpenAICompatibleBriefProvider

        return OpenAICompatibleBriefProvider(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout_seconds,
            settings.brief_output_token_budget,
        ), settings.llm_model
    from memory_hub.llm.fake import FakeBriefProvider

    return FakeBriefProvider(), "fake"


def purge_board_if_due(factory, settings, last_run: float | None, now: float, logger) -> float | None:
    """按间隔清理两类过期帖子；任何失败只记日志，绝不影响 brief 处理循环。返回最新的上次运行时刻。

    ``last_run`` 为 ``None`` 表示本进程尚未清理过，启动后立即执行一次，之后按间隔重复。
    不能用 ``0.0`` 作初值：``time.monotonic()`` 在部分系统上从开机起算，开机时间短于间隔时首次清理会被推迟。
    """
    thread_retention = timedelta(days=settings.board_retention_days)
    note_retention = timedelta(hours=settings.board_note_retention_hours)
    if settings.board_retention_days <= 0 and settings.board_note_retention_hours <= 0:
        return last_run
    if last_run is not None and now - last_run < settings.board_purge_interval_seconds:
        return last_run
    with factory() as session:
        # 分批删除避免长事务；单轮设上限，剩余留给下一轮，防止一次清理占满 worker。
        if settings.board_retention_days > 0:
            try:
                total = 0
                for _ in range(20):
                    removed = purge_resolved_board_posts(session, retention=thread_retention)
                    total += removed
                    if removed == 0:
                        break
                if total:
                    logger.info("memory-hub board purge removed_threads=%s retention_days=%s",
                                total, settings.board_retention_days)
            except Exception:  # noqa: BLE001 - 清理是尽力而为，不能拖垮 worker
                session.rollback()
                logger.exception("memory-hub board thread purge failed")
        if settings.board_note_retention_hours > 0:
            try:
                notes = 0
                for _ in range(20):
                    removed = purge_transient_board_notes(session, retention=note_retention)
                    notes += removed
                    if removed == 0:
                        break
                if notes:
                    logger.info("memory-hub board purge removed_notes=%s retention_hours=%s",
                                notes, settings.board_note_retention_hours)
            except Exception:  # noqa: BLE001 - 清理是尽力而为，不能拖垮 worker
                session.rollback()
                logger.exception("memory-hub board note purge failed")
    return now


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
    lease_seconds = max(90, int(settings.llm_timeout_seconds) + 30)
    last_purge: float | None = None
    while True:
        with factory() as session:
            processed = run_once(
                session,
                provider,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                model_name=model_name,
                rebase_interval_seconds=settings.brief_rebase_interval_seconds,
                user_debounce_seconds=settings.brief_user_debounce_seconds,
                project_debounce_seconds=settings.brief_project_debounce_seconds,
                prompt_token_budget=settings.brief_prompt_token_budget,
                output_token_budget=settings.brief_output_token_budget,
                daily_token_budget=settings.brief_daily_token_budget,
                max_attempts=settings.brief_max_attempts,
            )
        if processed:
            logger.info("memory-hub worker processed jobs=%s provider=%s", processed, settings.llm_provider)
        last_purge = purge_board_if_due(factory, settings, last_purge, time.monotonic(), logger)
        time.sleep(1)