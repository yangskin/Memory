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


def load_settings() -> Settings:
    return Settings(
        environment=os.getenv("MEMORY_HUB_ENV", "development"),
        database_url=os.getenv("MEMORY_HUB_DATABASE_URL") or None,
        public_base_url=os.getenv("MEMORY_HUB_PUBLIC_BASE_URL") or None,
        log_level=os.getenv("MEMORY_HUB_LOG_LEVEL", "INFO"),
        disable_docs=os.getenv("MEMORY_HUB_DISABLE_DOCS", "false").strip().lower() == "true",
        llm_provider=os.getenv("LLM_PROVIDER", "fake"),
    )