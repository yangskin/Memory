from fastapi.testclient import TestClient

from memory_hub.api.main import create_app


def test_healthz_is_available_without_a_database(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "not_configured"}


def test_actual_request_body_limit_applies_without_content_length(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    client = TestClient(create_app())
    response = client.post("/missing", content=b"x" * (1_048_577))
    assert response.status_code == 413


def test_production_docs_and_cors_are_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    monkeypatch.setenv("MEMORY_HUB_DISABLE_DOCS", "true")
    client = TestClient(create_app())
    response = client.get("/docs", headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 404
    assert "access-control-allow-origin" not in response.headers