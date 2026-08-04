from memory_hub.config import load_settings


def test_default_settings_use_fake_provider(monkeypatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    settings = load_settings()
    assert settings.llm_provider == "fake"
    assert settings.database_url is None