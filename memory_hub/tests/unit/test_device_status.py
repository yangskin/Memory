"""设备状态独立模块：覆盖更新、凭据隔离、通道白名单与共享视图；SQLite 验证路由逻辑。"""
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import JSON, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.api import routes_device_status
from memory_hub.api.routes_device_status import MAX_DEVICES_PER_CREDENTIAL, STALE_RETENTION, purge_stale_device_status, router
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, DeviceStatus

PROJECT = "example-project"
URL = f"/v1/projects/{PROJECT}/device-status"


@pytest.fixture
def service():
    original = AccessToken.__table__.c.scopes.type
    AccessToken.__table__.c.scopes.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        AccessToken.__table__.create(engine)
        DeviceStatus.__table__.create(engine)
        app = FastAPI()
        factory = sessionmaker(engine, expire_on_commit=False)
        app.state.session_factory = factory
        app.state.rate_limiter = TokenRateLimiter()
        app.include_router(router)

        def seed(user="alice", project=PROJECT, scopes=("events:write", "context:read")):
            token_id, raw, hashed = create_token()
            with factory() as session:
                session.add(AccessToken(token_id=token_id, token_secret_hash=hashed, token_prefix="test",
                                        user_id=user, project_id=project, scopes=list(scopes)))
                session.commit()
            return {"Authorization": "Bearer " + raw}

        yield TestClient(app), factory, seed
    finally:
        engine.dispose()
        AccessToken.__table__.c.scopes.type = original


def put(client, headers, channel="hub-health", device="PC-A", content='{"state":"ok"}', **extra):
    return client.post(URL + "/" + channel, headers=headers,
                       json={"device_name": device, "content": content, **extra})


def rows(factory):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(DeviceStatus))


def test_repeated_publication_overwrites_one_row_per_device_and_channel(service):
    client, factory, seed = service
    headers = seed()
    for index in range(30):
        response = put(client, headers, content='{"n":%d}' % index)
        assert response.status_code == 200, response.text
    put(client, headers, channel="codex-execution")
    # 30 次 hub-health 发布只占一行；另一通道独立一行。
    assert rows(factory) == 2
    items = client.get(URL, headers=headers, params={"channel": "hub-health", "device_name": "PC-A"}).json()["items"]
    assert len(items) == 1 and items[0]["content"] == '{"n":29}'
    assert items[0]["author_user_id"] == "alice" and len(items[0]["author_auth_id"]) == 64
    assert datetime.fromisoformat(items[0]["expires_at"]) > datetime.fromisoformat(items[0]["updated_at"])


def test_same_device_name_from_two_credentials_is_visible_as_conflict(service):
    client, factory, seed = service
    put(client, seed(user="alice"))
    put(client, seed(user="mallory"))
    items = client.get(URL, headers=seed(), params={"channel": "hub-health", "device_name": "PC-A"}).json()["items"]
    # 不同凭据各自一行，读取方据此识别同名冲突，不会被另一凭据覆盖。
    assert {item["author_user_id"] for item in items} == {"alice", "mallory"}
    assert len({item["author_auth_id"] for item in items}) == 2


def test_device_names_per_credential_are_bounded(service):
    from memory_hub.auth.rate_limit import RateLimitDecision

    client, factory, seed = service
    # 本用例只验证设备名上限；写入频率限制另有覆盖，这里放开以便一次写满上限。
    client.app.state.rate_limiter.check = lambda *args, **kwargs: RateLimitDecision(True)
    headers = seed()
    for index in range(MAX_DEVICES_PER_CREDENTIAL):
        assert put(client, headers, device=f"PC-{index}").status_code == 200
    assert put(client, headers, device="PC-EXTRA").status_code == 409
    # 已存在的设备仍可覆盖更新。
    assert put(client, headers, device="PC-0").status_code == 200
    assert rows(factory) == MAX_DEVICES_PER_CREDENTIAL
    # 长期过期的旧设备名（改名/停用遗留）不占名额，也会被清理。
    with factory() as session:
        stale = session.scalar(select(DeviceStatus).where(DeviceStatus.device_name == "PC-1"))
        stale.expires_at = datetime.now(UTC) - STALE_RETENTION - timedelta(minutes=1)
        session.commit()
    assert put(client, headers, device="PC-EXTRA").status_code == 200
    with factory() as session:
        assert purge_stale_device_status(session) == 1
    assert rows(factory) == MAX_DEVICES_PER_CREDENTIAL


def test_worker_purges_stale_rows_independently_of_board_retention(service):
    import logging
    from memory_hub.worker.main import purge_device_status_if_due

    client, factory, seed = service
    headers = seed()
    put(client, headers, device="PC-LIVE")
    put(client, headers, device="PC-GONE")
    with factory() as session:
        gone = session.scalar(select(DeviceStatus).where(DeviceStatus.device_name == "PC-GONE"))
        gone.expires_at = datetime.now(UTC) - STALE_RETENTION - timedelta(minutes=1)
        session.commit()
    logger = logging.getLogger("test-device-status-purge")
    last = purge_device_status_if_due(factory, 28800, None, 5.0, logger)
    assert last == 5.0
    with factory() as session:
        assert {row.device_name for row in session.scalars(select(DeviceStatus))} == {"PC-LIVE"}
    # 间隔内不重复扫描。
    assert purge_device_status_if_due(factory, 28800, last, 100.0, logger) == last


def test_status_writes_are_rate_limited_per_credential(service):
    client, factory, seed = service
    headers = seed()
    for _ in range(60):
        assert put(client, headers).status_code == 200
    assert put(client, headers).status_code == 429
    # 另一凭据不受影响；限流后仍只有覆盖更新的那一行。
    assert put(client, seed(user="bob")).status_code == 200
    assert rows(factory) == 2


def test_validation_scope_and_project_boundaries(service):
    client, factory, seed = service
    headers = seed()
    assert put(client, headers, channel="anything").status_code == 404
    assert put(client, headers, content="postgresql://u:p@h/d").status_code == 422
    assert put(client, headers, device="bad name").status_code == 422
    assert put(client, headers, ttl_seconds=5).status_code == 422
    assert client.post(URL + "/hub-health", headers=headers,
                       json={"device_name": "PC-A", "content": "{}", "extra": 1}).status_code == 422
    assert put(client, seed(scopes=("context:read",))).status_code == 403
    assert put(client, seed(project="other")).status_code == 403
    assert client.get(URL, headers=headers, params={"channel": "unknown"}).status_code == 404
    assert client.get(URL, headers=seed(scopes=("events:write",)), params={"channel": "hub-health"}).status_code == 403
    assert rows(factory) == 0


def test_read_keeps_expired_rows_but_shared_view_shows_only_live_ones(service):
    client, factory, seed = service
    headers = seed()
    put(client, headers, device="PC-LIVE")
    put(client, headers, device="PC-OLD")
    with factory() as session:
        old = session.scalar(select(DeviceStatus).where(DeviceStatus.device_name == "PC-OLD"))
        old.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    listed = client.get(URL, headers=headers, params={"channel": "hub-health"}).json()["items"]
    assert {item["device_name"] for item in listed} == {"PC-LIVE", "PC-OLD"}
    shared = client.get("/v1/shared-device-status", headers=headers).json()
    assert shared["project_id"] == PROJECT
    assert [item["device_name"] for item in shared["items"]] == ["PC-LIVE"]
    # 其它项目的凭据看不到本项目状态。
    assert client.get("/v1/shared-device-status", headers=seed(project="other")).json()["items"] == []


def test_device_status_module_is_independent_from_the_board():
    source = open(routes_device_status.__file__, encoding="utf-8").read()
    assert "BoardPost" not in source and "routes_board" not in source
