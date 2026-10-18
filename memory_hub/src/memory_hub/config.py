"""Environment-backed settings for the independent Hub service."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str | None
    public_base_url: str | None
    log_level: str
    disable_docs: bool
    llm_provider: str
    llm_base_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    llm_timeout_seconds: float
    brief_user_debounce_seconds: int
    brief_project_debounce_seconds: int
    brief_rebase_interval_seconds: int
    brief_prompt_token_budget: int
    brief_output_token_budget: int
    brief_daily_token_budget: int
    brief_max_attempts: int
    # 已结案线程的保留天数；0 表示关闭整线程清理。open 的帖子永远不会被清理。
    board_retention_days: int
    # 已处理且已过期的 note（心跳类重复帖）的保留小时数；0 表示关闭逐帖清理。
    board_note_retention_hours: int
    board_purge_interval_seconds: int


def load_settings() -> Settings:
    return Settings(
        environment=os.getenv("MEMORY_HUB_ENV", "development"),
        database_url=os.getenv("MEMORY_HUB_DATABASE_URL") or None,
        public_base_url=os.getenv("MEMORY_HUB_PUBLIC_BASE_URL") or None,
        log_level=os.getenv("MEMORY_HUB_LOG_LEVEL", "INFO"),
        disable_docs=os.getenv("MEMORY_HUB_DISABLE_DOCS", "false").strip().lower() == "true",
        llm_provider=os.getenv("LLM_PROVIDER", "fake"),
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_api_key=os.getenv("LLM_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL") or None,
        llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
        brief_user_debounce_seconds=max(1, int(os.getenv("BRIEF_USER_DEBOUNCE_SECONDS", "120"))),
        brief_project_debounce_seconds=max(1, int(os.getenv("BRIEF_PROJECT_DEBOUNCE_SECONDS", "300"))),
        brief_rebase_interval_seconds=max(60, int(os.getenv("BRIEF_REBASE_INTERVAL_SECONDS", "21600"))),
        brief_prompt_token_budget=max(1024, int(os.getenv("BRIEF_PROMPT_TOKEN_BUDGET", "6000"))),
        brief_output_token_budget=max(128, int(os.getenv("BRIEF_OUTPUT_TOKEN_BUDGET", "800"))),
        brief_daily_token_budget=max(1024, int(os.getenv("BRIEF_DAILY_TOKEN_BUDGET", "100000"))),
        brief_max_attempts=max(1, int(os.getenv("BRIEF_MAX_ATTEMPTS", "5"))),
        board_retention_days=max(0, int(os.getenv("BOARD_RETENTION_DAYS", "30"))),
        board_note_retention_hours=max(0, int(os.getenv("BOARD_NOTE_RETENTION_HOURS", "8"))),
        board_purge_interval_seconds=max(60, int(os.getenv("BOARD_PURGE_INTERVAL_SECONDS", "28800"))),
    )