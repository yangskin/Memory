"""真实 HTTP 路由与 SQLite 事务覆盖；PostgreSQL 迁移另由集成测试验证。"""
from datetime import UTC, datetime, timedelta
import hashlib
import secrets
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, JSON
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.api.routes_enrollment import router
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, DeviceEnrollment


@pytest.fixture
def service():
    # 仅此测试环境为已有 PostgreSQL ARRAY 使用 SQLite JSON；生产类型保持不变。
    original = AccessToken.__table__.c.scopes.type
    AccessToken.__table__.c.scopes.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        AccessToken.__table__.create(engine)
        DeviceEnrollment.__table__.create(engine)
        app = FastAPI()
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        app.state.rate_limiter = TokenRateLimiter()
        app.include_router(router)
        client = TestClient(app)
        def seed(user="owner", project="example-project", scopes=None):
            token_id, raw, hashed = create_token()
            with app.state.session_factory() as session:
                session.add(AccessToken(token_id=token_id, token_secret_hash=hashed, token_prefix="test",
                    user_id=user, project_id=project, scopes=scopes or ["events:write", "context:read"]))
                session.commit()
            return {"Authorization": "Bearer " + raw}
        yield client, app, seed
    finally:
        engine.dispose()
        AccessToken.__table__.c.scopes.type = original


def pending(client):
    token_id, token, hashed = create_token()
    poll_secret = secrets.token_urlsafe(32)
    payload = dict(enrollment_id=uuid4().hex, device_name="QA-PC", token_id=token_id,
        token_secret_hash=hashed, poll_secret_hash=hashlib.sha256(poll_secret.encode()).hexdigest())
    response = client.post("/v1/projects/example-project/device-enrollments", json=payload)
    assert response.status_code == 200, response.text
    return payload, token, poll_secret, response.json()


def test_approve_then_authenticate_without_transmitting_plain_token(service):
    client, app, seed = service
    headers = seed(scopes=["devices:approve"])
    payload, token, secret, item = pending(client)
    path = "/v1/projects/example-project/device-enrollments/" + item["enrollment_id"]
    assert token not in str(payload) + str(item)
    with app.state.session_factory() as session:
        assert session.get(AccessToken, payload["token_id"]) is None
    assert client.get(path + "/status").status_code == 401
    assert client.get(path + "/status", headers={"X-Memory-Enrollment-Secret": "x" * 43}).status_code == 401
    decision = {"decision": "approve", "pairing_code": item["pairing_code"]}
    assert client.post(path + "/decision", json=decision, headers=headers).json()["status"] == "approved"
    assert client.post(path + "/decision", json=decision, headers=headers).status_code == 200
    assert client.post("/v1/projects/example-project/device-enrollments", json=payload).json()["status"] == "approved"
    assert client.get(path + "/status", headers={"X-Memory-Enrollment-Secret": secret}).json()["status"] == "approved"
    # 新设备拿到最小业务权限，不能给其它设备审批，也不能冒充其它 user。
    assert client.get("/v1/projects/example-project/device-enrollments", headers={"Authorization": "Bearer " + token}).status_code == 403
    with app.state.session_factory() as session:
        credential = session.get(AccessToken, payload["token_id"])
        assert credential.user_id == "owner"
        assert credential.scopes == ["events:write", "context:read"]
        credential.revoked_at = datetime.now(UTC)
        session.commit()
    assert client.get(path + "/status", headers={"X-Memory-Enrollment-Secret": secret}).json()["status"] == "revoked"


def test_approval_scope_project_pairing_and_rejection(service):
    client, app, seed = service
    ordinary = seed()
    admin = seed(scopes=["devices:approve"])
    outsider = seed(project="other", scopes=["devices:approve"])
    payload, token, secret, item = pending(client)
    path = "/v1/projects/example-project/device-enrollments/" + item["enrollment_id"] + "/decision"
    decision = {"decision": "approve", "pairing_code": item["pairing_code"]}
    assert client.post(path, headers=ordinary, json=decision).status_code == 403
    assert client.post(path, headers=outsider, json=decision).status_code == 403
    assert client.post(path, headers=admin, json={**decision, "pairing_code": "F" * 12}).status_code == 409
    assert client.post(path, headers=admin, json={**decision, "decision": "reject"}).json()["status"] == "rejected"
    assert client.post(path, headers=admin, json=decision).status_code == 409
    with app.state.session_factory() as session:
        assert session.get(AccessToken, payload["token_id"]) is None


def test_expiry_idempotency_collision_and_schema(service):
    client, app, seed = service
    admin = seed(scopes=["devices:approve"])
    payload, token, secret, item = pending(client)
    url = "/v1/projects/example-project/device-enrollments"
    assert client.post(url, json=payload).json() == item
    assert client.post(url, json={**payload, "device_name": "IMPOSTOR"}).status_code == 409
    assert client.post(url, json={**payload, "extra": "bad"}).status_code == 422
    with app.state.session_factory() as session:
        session.get(DeviceEnrollment, item["enrollment_id"]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert client.get(url, headers=admin).json()["items"] == []
    assert client.post(url + "/" + item["enrollment_id"] + "/decision", headers=admin,
        json={"decision": "approve", "pairing_code": item["pairing_code"]}).status_code == 409
    assert client.get(url + "/" + item["enrollment_id"] + "/status",
        headers={"X-Memory-Enrollment-Secret": secret}).json()["status"] == "expired"


def test_public_request_rate_limit(service):
    client, app, seed = service
    seed()
    for _ in range(10):
        pending(client)
    _, _, hashed = create_token()
    result = client.post("/v1/projects/example-project/device-enrollments", json=dict(enrollment_id=uuid4().hex,
        device_name="QA-PC", token_id="tok_" + "a" * 24, token_secret_hash=hashed, poll_secret_hash=hashed))
    assert result.status_code == 429
