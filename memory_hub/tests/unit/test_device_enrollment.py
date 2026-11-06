"""开放注册限流与人工审批路由已移除的真实 HTTP 覆盖；PostgreSQL 迁移另由集成测试验证。"""
import ipaddress
from types import SimpleNamespace
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
from memory_hub.db.models import AccessToken, DeviceEnrollment, TokenDevice


@pytest.fixture
def service():
    # 仅此测试环境为已有 PostgreSQL ARRAY 使用 SQLite JSON；生产类型保持不变。
    original = AccessToken.__table__.c.scopes.type
    AccessToken.__table__.c.scopes.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        AccessToken.__table__.create(engine)
        TokenDevice.__table__.create(engine)
        DeviceEnrollment.__table__.create(engine)
        app = FastAPI()
        app.state.session_factory = sessionmaker(engine, expire_on_commit=False)
        app.state.rate_limiter = TokenRateLimiter()
        app.state.settings = SimpleNamespace(trusted_proxies=(), open_enrollment_projects=("example-project",))
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


def payload():
    token_id, _, hashed = create_token()
    return dict(enrollment_id=uuid4().hex, device_name="QA-PC", token_id=token_id,
                token_secret_hash=hashed, poll_secret_hash=hashed)


OPEN = "/v1/projects/example-project/device-enrollments/open"


def test_manual_approval_routes_are_removed(service):
    client, _, seed = service
    admin = seed(scopes=["devices:approve"])
    base = "/v1/projects/example-project/device-enrollments"
    enrollment_id = uuid4().hex
    # 匿名待审申请、状态轮询、待审列表与批准决定均不再存在；旧审批凭据也无法使用。
    assert client.post(base, json=payload()).status_code in {404, 405}
    assert client.get(base, headers=admin).status_code in {404, 405}
    assert client.get(base + "/" + enrollment_id + "/status",
                      headers={"X-Memory-Enrollment-Secret": "x" * 43}).status_code == 404
    assert client.post(base + "/" + enrollment_id + "/decision", headers=admin,
                       json={"decision": "approve", "pairing_code": "A" * 12}).status_code == 404
    with service[1].state.session_factory() as session:
        assert session.query(DeviceEnrollment).count() == 0


def test_public_request_rate_limit(service):
    client, app, seed = service
    seed()
    for _ in range(10):
        assert client.post(OPEN, json=payload()).status_code == 200
    assert client.post(OPEN, json=payload()).status_code == 429


def test_source_limit_counts_each_real_client_behind_the_trusted_proxy(service):
    _, app, seed = service
    seed()
    app.state.settings = SimpleNamespace(trusted_proxies=(ipaddress.ip_network("172.16.0.0/12"),),
                                         open_enrollment_projects=("example-project",))
    # 所有请求都从同一个代理地址进入，就像生产里经 Caddy 转发。
    proxied = TestClient(app, client=("172.19.0.5", 4321))

    def attempt(source):
        return proxied.post(OPEN, json=payload(), headers={"X-Forwarded-For": source}).status_code

    assert [attempt("203.0.113.1") for _ in range(10)] == [200] * 10
    assert attempt("203.0.113.1") == 429
    # 另一位真实访客不受前一位耗尽额度的影响。
    assert attempt("203.0.113.2") == 200
    # 不受信的直连方伪造转发头无法换来新额度。
    direct = TestClient(app, client=("198.51.100.9", 4321))
    for index in range(10):
        assert direct.post(OPEN, headers={"X-Forwarded-For": f"203.0.113.{100 + index}"},
                           json=payload()).status_code == 200
    assert direct.post(OPEN, headers={"X-Forwarded-For": "203.0.113.250"}, json=payload()).status_code == 429


def test_source_rejections_do_not_consume_the_global_limit(service):
    _, app, _ = service
    # 项目没有有效凭据：通过限流的请求返回 404 且不写库，避免项目每小时配额干扰限流计数。
    app.state.settings = SimpleNamespace(trusted_proxies=(ipaddress.ip_network("172.16.0.0/12"),),
                                         open_enrollment_projects=("empty-project",))
    proxied = TestClient(app, client=("172.19.0.5", 4321))
    url = "/v1/projects/empty-project/device-enrollments/open"

    def attempt(source):
        return proxied.post(url, json=payload(), headers={"X-Forwarded-For": source}).status_code

    # 单一来源刷满 60 次：只有前 10 次通过，其余被来源额度拒绝。
    flood = [attempt("203.0.113.66") for _ in range(60)]
    assert flood == [404] * 10 + [429] * 50
    # 被拒绝的 50 次不计入全局额度，其他来源仍可正常申请（已用 10/60）。
    assert attempt("198.51.100.7") == 404
    # 全局上限仍然有效：再有 4 个来源各用满 10 次 + 上面 1 次 = 51，再补 9 次达到 60。
    for index in range(4):
        assert [attempt(f"198.51.100.{20 + index}") for _ in range(10)] == [404] * 10
    assert [attempt("198.51.100.7") for _ in range(9)] == [404] * 9
    assert attempt("198.51.100.99") == 429
