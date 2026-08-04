"""Static shared-dashboard page is served without any database."""

from __future__ import annotations

from fastapi.testclient import TestClient

from memory_hub.api.main import create_app


def test_shared_page_is_served_without_database() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/shared")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "/v1/shared-feed" in body
    assert "sessionStorage" in body
    assert "Bearer " in body
