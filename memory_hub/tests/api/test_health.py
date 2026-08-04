from fastapi.testclient import TestClient

from memory_hub.api.main import create_app


def test_healthz_is_available_without_a_database() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "not_configured"}