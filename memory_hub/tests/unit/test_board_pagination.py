"""Board 游标分页与保守清理的路由级覆盖；用 SQLite 验证查询逻辑，PostgreSQL 由集成测试另行覆盖。"""
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import JSON, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from memory_hub.api.routes_board import router
from memory_hub.auth.rate_limit import TokenRateLimiter
from memory_hub.auth.tokens import create_token
from memory_hub.db.models import AccessToken, BoardPost
from memory_hub.db.repositories import (
    decode_board_cursor, encode_board_cursor, list_board_posts, purge_resolved_board_posts,
    purge_transient_board_notes,
)

PROJECT = "example-project"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def service():
    original = AccessToken.__table__.c.scopes.type
    original_refs = BoardPost.__table__.c.references_json.type
    AccessToken.__table__.c.scopes.type = JSON()
    # JSONB 只有 PostgreSQL 支持；SQLite 用例用 JSON 渲染同一列，避免建表即失败。
    BoardPost.__table__.c.references_json.type = JSON()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    try:
        AccessToken.__table__.create(engine)
        BoardPost.__table__.create(engine)
        app = FastAPI()
        factory = sessionmaker(engine, expire_on_commit=False)
        app.state.session_factory = factory
        app.state.rate_limiter = TokenRateLimiter()
        app.include_router(router)
        token_id, raw, hashed = create_token()
        with factory() as session:
            session.add(AccessToken(token_id=token_id, token_secret_hash=hashed, token_prefix="test",
                                    user_id="alice", project_id=PROJECT, scopes=["events:write", "context:read"]))
            session.commit()
        yield TestClient(app), factory, {"Authorization": "Bearer " + raw, "X-Memory-User-ID": "alice"}
    finally:
        engine.dispose()
        AccessToken.__table__.c.scopes.type = original
        BoardPost.__table__.c.references_json.type = original_refs


def add(factory, *, minutes=0, status="open", thread=None, expires=None, post_type="request", project=PROJECT,
        post_id=None, updated=None, author_agent=None):
    post_id = post_id or uuid4()
    created = BASE + timedelta(minutes=minutes)
    with factory() as session:
        session.add(BoardPost(post_id=post_id, project_id=project, author_user_id="alice", post_type=post_type,
                              content="c", thread_id=thread or post_id, status=status, created_at=created,
                              updated_at=updated or created, expires_at=expires, author_agent_id=author_agent))
        session.commit()
    return post_id


def query(client, headers, **body):
    response = client.post(f"/v1/projects/{PROJECT}/board/query", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client, headers, **body):
    pages, cursor = [], None
    for _ in range(50):
        page = query(client, headers, cursor=cursor, **body)
        pages.append(page)
        cursor = page["next_cursor"]
        if not cursor:
            return pages
    raise AssertionError("游标没有在合理页数内结束")


def test_cursor_walk_returns_every_post_exactly_once_beyond_the_old_50_limit(service):
    client, factory, headers = service
    expected = {str(add(factory, minutes=i)) for i in range(120)}
    pages = walk(client, headers, max_items=50)
    ids = [item["post_id"] for page in pages for item in page["items"]]
    assert len(ids) == len(set(ids)) == 120 and set(ids) == expected
    assert [len(p["items"]) for p in pages] == [50, 50, 20]
    assert [p["has_more"] for p in pages] == [True, True, False]
    assert pages[-1]["next_cursor"] is None


def test_order_is_stable_when_many_posts_share_the_same_timestamp(service):
    client, factory, headers = service
    expected = {str(add(factory, minutes=5)) for _ in range(75)}
    pages = walk(client, headers, max_items=20)
    ids = [item["post_id"] for page in pages for item in page["items"]]
    assert len(ids) == len(set(ids)) == 75 and set(ids) == expected


def test_a_post_created_mid_walk_does_not_duplicate_or_skip_existing_posts(service):
    client, factory, headers = service
    expected = {str(add(factory, minutes=i)) for i in range(30)}
    first = query(client, headers, max_items=10)
    add(factory, minutes=1000)
    seen = {item["post_id"] for item in first["items"]}
    cursor = first["next_cursor"]
    while cursor:
        page = query(client, headers, max_items=10, cursor=cursor)
        ids = {item["post_id"] for item in page["items"]}
        assert not ids & seen
        seen |= ids
        cursor = page["next_cursor"]
    assert seen == expected


def test_without_cursor_response_keeps_legacy_shape_and_total_semantics(service):
    client, factory, headers = service
    for i in range(60):
        add(factory, minutes=i)
    body = query(client, headers, max_items=50)
    assert body["total"] == 50 and len(body["items"]) == 50 and body["has_more"] is True
    # 旧客户端用 total>=50 / len==50 判断“可能被截断”，该语义必须保持。
    small = query(client, headers, max_items=20)
    assert small["total"] == 20 and small["has_more"] is True


def test_exact_page_boundary_reports_no_more(service):
    client, factory, headers = service
    for i in range(50):
        add(factory, minutes=i)
    body = query(client, headers, max_items=50)
    assert len(body["items"]) == 50 and body["has_more"] is False and body["next_cursor"] is None


def test_filters_apply_across_pages(service):
    client, factory, headers = service
    wanted = {str(add(factory, minutes=i, post_type="request")) for i in range(0, 70, 2)}
    for i in range(1, 70, 2):
        add(factory, minutes=i, post_type="note")
    pages = walk(client, headers, max_items=10, post_type="request", filter="unresolved")
    ids = {item["post_id"] for page in pages for item in page["items"]}
    assert ids == wanted


def test_invalid_cursor_is_rejected_without_touching_data(service):
    client, factory, headers = service
    add(factory)
    for bad in ("not-a-cursor", "e30", "aGVsbG8"):
        response = client.post(f"/v1/projects/{PROJECT}/board/query", headers=headers, json={"cursor": bad})
        assert response.status_code == 422, bad
    assert query(client, headers, cursor="")["total"] == 1


def test_cursor_roundtrip_and_requires_timezone():
    post = BoardPost(post_id=UUID(int=7), created_at=BASE)
    cursor = encode_board_cursor(post)
    assert "=" not in cursor and decode_board_cursor(cursor) == (BASE, UUID(int=7))
    # naive 时间（SQLite 读回值）按 UTC 解释，与 aware 的同一时刻编码结果一致。
    assert encode_board_cursor(BoardPost(post_id=UUID(int=7), created_at=BASE.replace(tzinfo=None))) == cursor
    offset = BASE.astimezone(timezone(timedelta(hours=8)))
    assert encode_board_cursor(BoardPost(post_id=UUID(int=7), created_at=offset)) == cursor
    # 手工构造的无时区游标必须被拒绝，不能被当作任意时刻比较。
    import base64, json
    forged = base64.urlsafe_b64encode(json.dumps({"c": "2026-01-01T00:00:00", "p": str(UUID(int=7))}).encode()).decode().rstrip("=")
    with pytest.raises(ValueError):
        decode_board_cursor(forged)


def test_expired_posts_are_hidden_from_query_but_kept_for_internal_lookups(service):
    client, factory, headers = service
    live = add(factory, minutes=1, expires=datetime.now(UTC) + timedelta(days=1))
    permanent = add(factory, minutes=2)
    expired = add(factory, minutes=3, expires=datetime.now(UTC) - timedelta(seconds=1))
    ids = {item["post_id"] for item in query(client, headers)["items"]}
    assert ids == {str(live), str(permanent)}
    with factory() as session:
        internal = list_board_posts(session, project_id=PROJECT, max_items=50)
        assert str(expired) in {str(p.post_id) for p in internal}


def test_purge_removes_only_whole_resolved_threads_past_retention(service):
    _, factory, _ = service
    now = BASE + timedelta(days=100)
    old = BASE + timedelta(days=1)
    recent = now - timedelta(days=1)
    stale_root = add(factory, status="resolved", updated=old, minutes=10)
    stale_reply = add(factory, status="resolved", updated=old, minutes=11, thread=stale_root, post_type="reply")
    fresh_resolved = add(factory, status="resolved", updated=recent, minutes=12)
    open_old = add(factory, status="open", updated=old, minutes=13)
    mixed_root = add(factory, status="resolved", updated=old, minutes=14)
    mixed_open_reply = add(factory, status="open", updated=old, minutes=15, thread=mixed_root, post_type="reply")
    recent_reply_root = add(factory, status="resolved", updated=old, minutes=16)
    recent_reply = add(factory, status="resolved", updated=recent, minutes=17, thread=recent_reply_root, post_type="reply")
    with factory() as session:
        removed = purge_resolved_board_posts(session, retention=timedelta(days=30), now=now)
    assert removed == 2
    with factory() as session:
        remaining = set(session.scalars(select(BoardPost.post_id)))
    assert remaining == {fresh_resolved, open_old, mixed_root, mixed_open_reply, recent_reply_root, recent_reply}
    assert stale_root not in remaining and stale_reply not in remaining


def test_purge_never_touches_open_posts_even_when_ancient_or_expired(service):
    _, factory, _ = service
    ancient = add(factory, status="open", updated=BASE, expires=BASE + timedelta(days=1))
    with factory() as session:
        assert purge_resolved_board_posts(session, retention=timedelta(days=1), now=BASE + timedelta(days=900)) == 0
    with factory() as session:
        assert session.get(BoardPost, ancient) is not None


def test_purge_is_batched_and_repeatable(service):
    _, factory, _ = service
    for i in range(25):
        add(factory, status="resolved", updated=BASE, minutes=i)
    now = BASE + timedelta(days=60)
    with factory() as session:
        assert purge_resolved_board_posts(session, retention=timedelta(days=30), now=now, batch=10) == 10
        assert purge_resolved_board_posts(session, retention=timedelta(days=30), now=now, batch=10) == 10
        assert purge_resolved_board_posts(session, retention=timedelta(days=30), now=now, batch=10) == 5
        assert purge_resolved_board_posts(session, retention=timedelta(days=30), now=now, batch=10) == 0


def test_purge_errs_on_the_side_of_keeping_when_thread_ids_collide_across_projects(service):
    _, factory, _ = service
    shared_thread = uuid4()
    gone = add(factory, status="resolved", updated=BASE, thread=shared_thread, project=PROJECT)
    kept = add(factory, status="open", updated=BASE, thread=shared_thread, project="other-project")
    with factory() as session:
        purge_resolved_board_posts(session, retention=timedelta(days=1), now=BASE + timedelta(days=90))
        remaining = set(session.scalars(select(BoardPost.post_id)))
    # thread_id 由客户端提供的 post_id 派生，理论上可跨项目撞车；此时 open 的一方让两者都保留，
    # 宁可少删也不误删。这是有意的保守，不是项目隔离保证。
    assert {gone, kept} <= remaining


def test_purge_schedule_runs_at_startup_then_waits_for_the_interval(service):
    from types import SimpleNamespace
    import logging
    from memory_hub.worker.main import purge_board_if_due

    _, factory, _ = service
    settings = SimpleNamespace(board_retention_days=30, board_note_retention_hours=8,
                               board_purge_interval_seconds=86400)
    logger = logging.getLogger("test-board-purge")
    stale = add(factory, status="resolved", updated=BASE, minutes=1)

    # monotonic 起点很小（刚开机）时，首次也必须立即执行，不能被推迟到运行满一个间隔之后。
    last = purge_board_if_due(factory, settings, None, 5.0, logger)
    assert last == 5.0
    with factory() as session:
        assert session.get(BoardPost, stale) is None

    again = add(factory, status="resolved", updated=BASE, minutes=2)
    # 间隔内不再扫描，返回值保持不变。
    assert purge_board_if_due(factory, settings, last, 5.0 + 86399, logger) == last
    with factory() as session:
        assert session.get(BoardPost, again) is not None

    # 到点后才再次清理。
    assert purge_board_if_due(factory, settings, last, 5.0 + 86400, logger) == 5.0 + 86400
    with factory() as session:
        assert session.get(BoardPost, again) is None


def test_purge_schedule_is_disabled_when_retention_is_zero(service):
    from types import SimpleNamespace
    import logging
    from memory_hub.worker.main import purge_board_if_due

    _, factory, _ = service
    settings = SimpleNamespace(board_retention_days=0, board_note_retention_hours=0,
                               board_purge_interval_seconds=60)
    stale = add(factory, status="resolved", updated=BASE)
    assert purge_board_if_due(factory, settings, None, 10.0, logging.getLogger("test-board-purge")) is None
    with factory() as session:
        assert session.get(BoardPost, stale) is not None


def test_transient_purge_removes_only_expired_resolved_notes_past_retention(service):
    _, factory, _ = service
    now = BASE + timedelta(hours=10)
    heartbeat_thread = uuid4()
    # 心跳线程里永远有一条 open 的当前心跳，所以旧的已处理心跳只能按单帖规则清理。
    gone = add(factory, status="resolved", post_type="note", expires=BASE + timedelta(minutes=5),
               thread=heartbeat_thread, updated=BASE, minutes=1)
    current = add(factory, status="open", post_type="note", expires=now + timedelta(minutes=5),
                  thread=heartbeat_thread, updated=BASE + timedelta(hours=9), minutes=2)
    still_valid = add(factory, status="resolved", post_type="note", expires=now + timedelta(hours=1),
                      updated=BASE, minutes=3)
    no_expiry = add(factory, status="resolved", post_type="note", updated=BASE, minutes=4)
    recent = add(factory, status="resolved", post_type="note", expires=BASE + timedelta(minutes=5),
                 updated=BASE + timedelta(hours=9), minutes=5)
    task_request = add(factory, status="resolved", post_type="request", expires=BASE + timedelta(minutes=5),
                       updated=BASE, minutes=6)
    root_with_reply = add(factory, status="resolved", post_type="note", expires=BASE + timedelta(minutes=5),
                          updated=BASE, minutes=7)
    reply = add(factory, status="resolved", post_type="reply", expires=BASE + timedelta(minutes=5),
                updated=BASE, thread=root_with_reply, minutes=8)
    open_expired = add(factory, status="open", post_type="note", expires=BASE + timedelta(minutes=5),
                       updated=BASE, minutes=9)
    with factory() as session:
        removed = purge_transient_board_notes(session, retention=timedelta(hours=8), now=now)
    assert removed == 1
    with factory() as session:
        remaining = set(session.scalars(select(BoardPost.post_id)))
    assert remaining == {current, still_valid, no_expiry, recent, task_request, root_with_reply, reply,
                         open_expired}
    assert gone not in remaining


def test_transient_purge_is_batched_and_repeatable(service):
    _, factory, _ = service
    for i in range(25):
        add(factory, status="resolved", post_type="note", expires=BASE + timedelta(minutes=1),
            updated=BASE, minutes=i)
    now = BASE + timedelta(hours=10)
    with factory() as session:
        assert purge_transient_board_notes(session, retention=timedelta(hours=8), now=now, batch=10) == 10
        assert purge_transient_board_notes(session, retention=timedelta(hours=8), now=now, batch=10) == 10
        assert purge_transient_board_notes(session, retention=timedelta(hours=8), now=now, batch=10) == 5
        assert purge_transient_board_notes(session, retention=timedelta(hours=8), now=now, batch=10) == 0


def test_purge_schedule_also_runs_the_transient_note_pass(service):
    from types import SimpleNamespace
    import logging
    from memory_hub.worker.main import purge_board_if_due

    _, factory, _ = service
    settings = SimpleNamespace(board_retention_days=30, board_note_retention_hours=8,
                               board_purge_interval_seconds=28800)
    heartbeat = add(factory, status="resolved", post_type="note",
                    expires=datetime.now(UTC) - timedelta(hours=9), updated=datetime.now(UTC) - timedelta(hours=9))
    purge_board_if_due(factory, settings, None, 1.0, logging.getLogger("test-board-purge"))
    with factory() as session:
        assert session.get(BoardPost, heartbeat) is None


def test_defaults_clean_transient_notes_every_eight_hours(monkeypatch):
    from memory_hub.config import load_settings

    for key in ("BOARD_PURGE_INTERVAL_SECONDS", "BOARD_NOTE_RETENTION_HOURS", "BOARD_RETENTION_DAYS"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings()
    assert settings.board_purge_interval_seconds == 8 * 3600
    assert settings.board_note_retention_hours == 8
    assert settings.board_retention_days == 30


def test_shared_board_keeps_machine_status_channels_out_of_messages(service):
    client, factory, headers = service
    question = add(factory, minutes=1, post_type="question")
    # 心跳按设备固定线程每分钟新建一条 note；它们不能占用人类留言的取数窗口。
    for i in range(60):
        add(factory, minutes=10 + i, post_type="note", author_agent="hub-health",
            expires=datetime.now(UTC) + timedelta(minutes=5))
    execution = add(factory, minutes=80, post_type="note", author_agent="codex-execution",
                    expires=datetime.now(UTC) + timedelta(minutes=5))
    expired = add(factory, minutes=81, post_type="note", author_agent="hub-health",
                  expires=datetime.now(UTC) - timedelta(seconds=1))

    response = client.post("/v1/shared-board", headers=headers, json={"max_items": 50})
    assert response.status_code == 200, response.text
    body = response.json()

    # 留言区只剩人类留言，计数不被心跳抬高。
    assert body["total"] == 1
    assert [item["post_id"] for item in body["items"]] == [str(question)]
    assert all(item["author_agent_id"] not in {"hub-health", "codex-execution"} for item in body["items"])

    # 机器状态走独立字段，且只保留仍在有效期内的快照。
    channels = {item["post_id"] for item in body["status_channels"]}
    assert str(execution) in channels
    assert str(expired) not in channels
    assert str(question) not in channels


def test_excluding_agents_keeps_posts_without_a_publisher_channel(service):
    _, factory, _ = service
    human = add(factory, minutes=2)
    channel = add(factory, minutes=1, author_agent="hub-health")
    with factory() as session:
        listed = list_board_posts(session, project_id=PROJECT, max_items=50,
                                  exclude_author_agents=["hub-health", "codex-execution"])
    ids = {post.post_id for post in listed}
    # author_agent_id 为 NULL 的普通留言不能被 NOT IN 误杀。
    assert human in ids and channel not in ids
