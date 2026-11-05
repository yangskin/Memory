from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from memory_hub.api.main import create_app


def test_healthz_is_available_without_a_database(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "not_configured"}


def test_request_body_limit_is_enforced_once_by_the_front_proxy(monkeypatch) -> None:
    # 应用不再预读整个请求体重复检查；上限只由前置 Caddy 执行，且 api 不对外发布端口。
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    client = TestClient(create_app())
    assert client.post("/missing", content=b"x" * 1_048_577).status_code == 404
    hub_root = Path(__file__).resolve().parents[2]
    caddy = (hub_root / "Caddyfile").read_text(encoding="utf-8")
    assert "max_size 1MB" in caddy
    compose = (hub_root / "compose.yaml").read_text(encoding="utf-8")
    api_section = compose.split("\n  api:\n", 1)[1].split("\n  caddy:\n", 1)[0]
    assert "ports:" not in api_section


def test_client_disconnect_is_a_quiet_client_error(monkeypatch, caplog) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    app = create_app()

    @app.post("/disconnect")
    def disconnect() -> None:
        raise ClientDisconnect()

    with caplog.at_level("ERROR"):
        response = TestClient(app, raise_server_exceptions=False).post("/disconnect", content=b"{}")
    assert response.status_code == 400
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_production_docs_and_cors_are_disabled(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HUB_DATABASE_URL", raising=False)
    monkeypatch.setenv("MEMORY_HUB_DISABLE_DOCS", "true")
    client = TestClient(create_app())
    response = client.get("/docs", headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 404
    assert "access-control-allow-origin" not in response.headers