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
        brief_user_debounce_seconds=int(os.getenv("BRIEF_USER_DEBOUNCE_SECONDS", "20")),
        brief_project_debounce_seconds=int(os.getenv("BRIEF_PROJECT_DEBOUNCE_SECONDS", "45")),
        brief_rebase_interval_seconds=int(os.getenv("BRIEF_REBASE_INTERVAL_SECONDS", "3600")),
    )