from memory_hub.config import load_settings


def test_default_settings_use_fake_provider(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    settings = load_settings()
    assert settings.llm_provider == "fake"
    assert settings.database_url is None
    assert settings.brief_user_debounce_seconds == 20