from memory_hub.config import load_settings


def test_default_settings_use_fake_provider(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    settings = load_settings()
    assert settings.llm_provider == "fake"
    assert settings.database_url is None
    assert settings.brief_user_debounce_seconds == 120
    assert settings.brief_project_debounce_seconds == 300
    assert settings.brief_prompt_token_budget == 6000
    assert settings.brief_output_token_budget == 800
    assert settings.brief_daily_token_budget == 100000
    assert settings.brief_max_attempts == 5